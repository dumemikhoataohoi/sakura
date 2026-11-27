"""Vault (ZV2, scripts/vault.py): crypto, fail-closed reads, config gating, every writer with the vault on and off,
alerts redaction, migrate guards and doctor. Dummy keys only; offline."""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeDataSession, fixture_json, make_root

import alerts
import build_latest
import calendar_events
import control
import coordinator_report
import daily_summary
import ff_calendar
import reports_lib as rl
import store_report
import vault
from zumi_common import iso_z, parse_utc

KEY_HEX = "11" * 32                       # dummy test key
KEY = bytes.fromhex(KEY_HEX)
OTHER_KEY_HEX = "22" * 32
NOW = parse_utc("2026-11-25T16:00:00Z")
PHASE1 = ["reports/**", "docs/**", "control/CHANGELOG.md", "reports/coord/**", "reports/agents/**",
          "data/calendar/**", "data/quality.json"]
PHASE2 = ["control/control.json", "data/latest.json"]


def set_vault(root: Path, enabled=True, phase=1, bridge="2.3", **extra) -> None:
    path = root / "config" / "settings.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["vault"] = {"enabled": enabled, "phase": phase, "ea_bridge_min": bridge, "paths": PHASE1,
                    "phase2_paths": PHASE2, "plain_exceptions": ["docs/dashboard/index.html"],
                    "generic_commit_messages": True, **extra}
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def encrypt_calendar(root: Path) -> None:
    """What `vault.py migrate --phase 1` does to the hand-kept calendar (a vault-set file)."""
    cal = root / "data/calendar/events_us.csv"
    vault.write_any(cal, cal.read_bytes(), True)


def control_raw(now=NOW, seq=2) -> dict:
    return {"v": 1, "author": "claude", "seq": seq, "updated_utc": iso_z(now),
            "global": {"risk_mult": 1.0, "pause_all": False}, "methods": {}, "news_blocks": [], "notes_vi": "ok"}


class KeyEnv(unittest.TestCase):
    key_hex = KEY_HEX

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ZUMI_KEY", "GITHUB_ACTIONS")}
        if self.key_hex:
            env["ZUMI_KEY"] = self.key_hex
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)


# ---------------------------------------------------------------- crypto
class CryptoTest(KeyEnv):
    def test_round_trip_sizes_and_utf8(self):
        for n in (0, 1, 31, 32, 33, 64, 65, 1000):
            data = bytes(range(256)) * 4
            data = data[:n]
            z = vault.encrypt("reports/x.json", data)
            self.assertTrue(z.startswith("ZV2:"))
            self.assertNotIn("\n", z)
            self.assertNotIn("=", z)                                   # base64url without padding
            self.assertEqual(vault.decrypt("reports/x.json", z), data)
        text = "Cảnh báo VIX ↑ 30".encode("utf-8")
        self.assertEqual(vault.decrypt("docs/a.md", vault.encrypt("docs/a.md", text)), text)

    def test_tag_tamper_fails(self):
        z = vault.encrypt("control/CHANGELOG.md", b"- seq 3: global risk_mult 1.00->0.50 (R05)\n")
        body = z[4:]
        for pos in (2, 30, 70, len(body) - 2):                        # nonce, tag, ciphertext
            c = "A" if body[pos] != "A" else "B"
            bad = "ZV2:" + body[:pos] + c + body[pos + 1:]
            with self.assertRaises(vault.VaultError):
                vault.decrypt("control/CHANGELOG.md", bad)
        for bad in (z[:40], "ZV2:", "ZV2:not*base64", "plain text", z + "!!"):
            with self.assertRaises(vault.VaultError):
                vault.decrypt("control/CHANGELOG.md", bad)

    def test_path_swap_fails(self):
        z = vault.encrypt("reports/daily/2026-11-25.md", b"page")
        for other in ("reports/daily/2026-11-26.md", "reports/daily/2026-11-25.mdx", "docs/x.md"):
            with self.assertRaises(vault.VaultError):
                vault.decrypt(other, z)
        self.assertEqual(vault.decrypt("./reports/daily/2026-11-25.md.zv", z), b"page")   # same logical path

    def test_wrong_missing_and_malformed_key_fail(self):
        z = vault.encrypt("docs/x.md", b"secret page")
        with mock.patch.dict(os.environ, {"ZUMI_KEY": OTHER_KEY_HEX}):
            with self.assertRaises(vault.VaultError):
                vault.decrypt("docs/x.md", z)
        for bad in ("", "abc", "zz" * 32, "11" * 31):
            with mock.patch.dict(os.environ, {"ZUMI_KEY": bad}):
                with self.assertRaises(vault.VaultError) as cm:
                    vault.decrypt("docs/x.md", z)
                if bad:
                    self.assertNotIn(bad, str(cm.exception))           # the key never appears in an error
        os.environ.pop("ZUMI_KEY")
        with self.assertRaises(vault.VaultError):
            vault.encrypt("docs/x.md", b"x")
        with self.assertRaises(vault.VaultError):
            vault.decrypt("docs/x.md", z)

    def test_determinism(self):
        a = vault.encrypt("reports/a.json", b"same")
        self.assertEqual(a, vault.encrypt("reports/a.json", b"same"))  # quiet diffs
        self.assertNotEqual(a, vault.encrypt("reports/b.json", b"same"))
        self.assertNotEqual(a[4:26], vault.encrypt("reports/a.json", b"other")[4:26])   # nonce depends on content
        with mock.patch.dict(os.environ, {"ZUMI_KEY": OTHER_KEY_HEX}):
            self.assertNotEqual(a, vault.encrypt("reports/a.json", b"same"))

    def test_known_answer_vectors_and_self_test(self):
        v = vault.kat_vectors()
        key = bytes.fromhex(vault.KAT_KEY_HEX)
        self.assertEqual(v["fp"], vault.fingerprint(key))
        ke, km = vault.subkeys(key)
        self.assertEqual((v["ke"], v["km"]), (ke.hex(), km.hex()))
        for case in v["cases"]:
            self.assertEqual(vault.decrypt(case["path"], case["zv"], key).hex(), case["pt_hex"])
        # frozen values (a change here breaks every EA on bridge 2.8 and every .zv file in the repo)
        self.assertEqual(v["fp"], "38906176")
        self.assertEqual(v["cases"][0]["zv"],
                         "ZV2:IQuhLFGHcQNuIwnp8RMwuV5ORCveA_WBATIJ_EgDw7Sq3NV6qB33kmpnVSUbznYmd3LhzBstkA")
        self.assertEqual(vault.self_test(KEY), [])
        self.assertEqual(len(vault.fingerprint()), 8)
        self.assertNotIn(KEY_HEX[:8], vault.fingerprint())

    def test_alert_id_is_opaque_and_stable(self):
        a = vault.alert_id("streak:a1b2c3d4e5f60718:2026-11-25:4")
        self.assertRegex(a, r"^z[0-9a-f]{12}$")
        self.assertEqual(a, vault.alert_id("streak:a1b2c3d4e5f60718:2026-11-25:4"))
        self.assertNotEqual(a, vault.alert_id("streak:a1b2c3d4e5f60718:2026-11-25:5"))


# ---------------------------------------------------------------- config and files
class ConfigAndFilesTest(KeyEnv):
    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_should_encrypt_phases(self):
        v = vault.settings({"vault": {"enabled": True, "paths": PHASE1, "phase2_paths": PHASE2,
                                      "plain_exceptions": ["docs/dashboard/index.html"]}})
        yes = ["reports/2026/11/25/a.json", "reports/daily/2026-11-25.md", "reports/coord/2026-11-25.json",
               "reports/agents/2026-11-25.md", "docs/workflows.md", "docs/dashboard/data.json", "control/CHANGELOG.md",
               "data/calendar/events_us.csv", "data/calendar/ff_week.json", "data/quality.json"]
        no = ["AGENTS.md", "config/settings.json", "scripts/vault.py", "tests/test_vault.py", "mql5/ZumiGitHubBridge.mqh",
              ".github/workflows/data.yml", "docs/dashboard/index.html", "data/history/DGS10.csv",
              "control/control.json", "data/latest.json", "control/reason_codes.md", "README.md"]
        for rel in yes:
            self.assertTrue(vault.should_encrypt(rel, v), rel)
        for rel in no:
            self.assertFalse(vault.should_encrypt(rel, v), rel)
        # phase 2 needs BOTH phase >= 2 and ea_bridge_min >= 2.8 (bridge 2.3 reads these two in plaintext)
        for phase, bridge, expect in ((2, "2.3", False), (1, "2.8", False), (2, "2.8", True), (3, "2.10", True)):
            vv = dict(v, phase=phase, ea_bridge_min=bridge)
            self.assertEqual(vault.should_encrypt("control/control.json", vv), expect, (phase, bridge))
            self.assertEqual(vault.should_encrypt("data/latest.json", vv), expect, (phase, bridge))
        self.assertFalse(vault.should_encrypt("reports/x.md", dict(v, enabled=False)))
        self.assertFalse(vault.should_encrypt("control/control.json", dict(v, paths=["**"])))   # never via a glob

    def test_read_any_fallback(self):
        p = self.root / "reports" / "daily" / "2026-11-25.md"
        with self.assertRaises(FileNotFoundError):
            vault.read_any(p)
        self.assertFalse(vault.exists(p))
        p.parent.mkdir(parents=True)
        p.write_text("plain page\n", encoding="utf-8")
        self.assertEqual(vault.read_text(p), "plain page\n")           # no .zv: plaintext, no key needed
        self.assertTrue(vault.write_any(p, b"secret page\n", True))
        self.assertFalse(p.exists())                                   # plaintext removed
        z = vault.zv_path(p)
        self.assertTrue(z.read_text().startswith("ZV2:"))
        self.assertNotIn("secret", z.read_text())
        self.assertEqual(vault.read_text(p), "secret page\n")          # .zv preferred
        self.assertEqual(vault.read_text(z), "secret page\n")          # either name works
        self.assertFalse(vault.write_any(p, b"secret page\n", True))   # deterministic: unchanged
        self.assertEqual(vault.glob(p.parent, "*.md"), [p])
        os.environ.pop("ZUMI_KEY")
        with self.assertRaises(vault.VaultError):                      # fail clearly, never "" / empty
            vault.read_text(p)
        os.environ["ZUMI_KEY"] = KEY_HEX
        with self.assertRaises(vault.VaultError):                      # never plaintext over a .zv ...
            vault.write_any(p, b"plain again\n", False)
        self.assertTrue(z.is_file())
        self.assertFalse(p.exists())
        self.assertTrue(vault.write_any(p, b"plain again\n", False, allow_decrypt=True))   # ... except `vault.py dec`
        self.assertFalse(z.exists())
        self.assertEqual(p.read_text(), "plain again\n")

    def test_check_detects_problems(self):
        set_vault(self.root)
        p = self.root / "docs" / "a.md"
        vault.write_text(p, "doc\n")
        self.assertEqual(vault.check(self.root), [])
        p.write_text("stale plaintext\n")
        self.assertTrue(any("both" in x for x in vault.check(self.root)))
        p.unlink()
        z = vault.zv_path(p)
        t = z.read_text().strip()
        z.write_text(t[:10] + ("A" if t[10] != "A" else "B") + t[11:] + "\n")   # flip one nonce character
        self.assertTrue(any("authentication failed" in x for x in vault.check(self.root)))

    def test_commit_message_and_public_log(self):
        self.assertEqual(vault.commit_message(self.root, "reports: x n=3 net R +1.2", "reports"), "reports: x n=3 net R +1.2")
        set_vault(self.root)
        self.assertEqual(vault.commit_message(self.root, "reports: x n=3 net R +1.2", "reports"), "reports: update")
        self.assertEqual(vault.public_log(self.root, "detail", "generic"), "detail")   # local session: details
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(vault.public_log(self.root, "detail", "generic"), "generic")


# ---------------------------------------------------------------- writers, vault on and off
class WritersTest(KeyEnv):
    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def assert_form(self, rel: str, encrypted: bool):
        plain, z = self.root / rel, vault.zv_path(self.root / rel)
        self.assertEqual(z.is_file(), encrypted, rel)
        self.assertEqual(plain.is_file(), not encrypted, rel)
        if encrypted:
            self.assertTrue(z.read_text().startswith("ZV2:"), rel)

    def run_writers(self, phase2=False) -> dict:
        payload = fixture_json("payload_agg.json")
        res = store_report.store(self.root, payload, NOW)
        day = dt.date.fromisoformat(payload["data"]["day"])
        raw_rel = res["path"].relative_to(self.root).as_posix()
        daily_summary.summarize(self.root, res["dates"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(coordinator_report.main(["--root", str(self.root), "--now", iso_z(NOW)]), 0)
        (self.root / "control").mkdir(exist_ok=True)
        vault.write_text(self.root / "control" / "control.json", json.dumps(control_raw()))
        with contextlib.redirect_stdout(out):
            self.assertEqual(control.cmd_normalize(self.root, NOW, ""), 0)
        ff_calendar.write_if_changed(self.root / "data" / "calendar" / "ff_week.json",
                                     {"src": "t", "fetched_day": "2026-11-25", "events": [{"t": 1, "title": "x"}]})
        build_latest.build(self.root, NOW, session=FakeDataSession())
        return {"raw": raw_rel, "day": day}

    def test_writers_vault_off(self):
        r = self.run_writers()
        for rel in (r["raw"], f"reports/daily/{r['day']}.md", "reports/weekly/2026-W48.md", "reports/monthly/2026-11.md",
                    "docs/dashboard/data.json", f"reports/coord/{NOW.date()}.md", f"reports/coord/{NOW.date()}.json",
                    "control/control.json", "control/CHANGELOG.md", "data/calendar/ff_week.json",
                    "data/calendar/events_us.csv", "data/quality.json", "data/latest.json"):
            self.assert_form(rel, False)
        self.assertEqual(list(self.root.rglob("*.zv")), [])

    def test_writers_vault_on_phase1(self):
        set_vault(self.root)
        vault.write_any(self.root / "data/calendar/events_us.csv",
                        (self.root / "data/calendar/events_us.csv").read_bytes(), True)   # migrated calendar
        r = self.run_writers()
        for rel in (r["raw"], f"reports/daily/{r['day']}.md", "reports/weekly/2026-W48.md", "reports/monthly/2026-11.md",
                    "docs/dashboard/data.json", f"reports/coord/{NOW.date()}.md", f"reports/coord/{NOW.date()}.json",
                    "control/CHANGELOG.md", "data/calendar/ff_week.json", "data/calendar/events_us.csv",
                    "data/quality.json"):
            self.assert_form(rel, True)
        # the live EA runs bridge 2.3: control.json and latest.json stay plaintext in phase 1
        self.assert_form("control/control.json", False)
        self.assert_form("data/latest.json", False)
        self.assertEqual(json.loads((self.root / "data/latest.json").read_text())["schema"], "zumi.latest")
        # readable through the vault, identical to what the readers expect
        self.assertIn("Zumi daily report", vault.read_text(self.root / f"reports/daily/{r['day']}.md"))
        self.assertIn("seq 2", vault.read_text(self.root / "control/CHANGELOG.md"))
        self.assertEqual(vault.check(self.root), [])
        self.assertTrue(calendar_events.load_events(self.root / "data/calendar/events_us.csv"))
        self.assertEqual(len(rl.load_reports(self.root, r["day"], r["day"])), 1)
        # idempotent: a second identical run changes nothing on disk
        snap = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        daily_summary.summarize(self.root, [r["day"]])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, snap)

    def test_writers_vault_on_phase2(self):
        set_vault(self.root, phase=2, bridge="2.8")
        encrypt_calendar(self.root)
        self.run_writers()
        self.assert_form("control/control.json", True)
        self.assert_form("data/latest.json", True)
        self.assertEqual(control.load_json(self.root / "control/control.json")["seq"], 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(control.main(["effective", "--root", str(self.root), "--now", iso_z(NOW)]), 0)
        self.assertTrue(json.loads(out.getvalue())["neutral"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(control.cmd_check(self.root, NOW, "", False), 0)

    def test_journal_writer_private_mode(self):
        root = make_root(public_mode=False)
        self.addCleanup(shutil.rmtree, root, True)
        set_vault(root)
        res = store_report.store(root, fixture_json("payload_basic.json"), NOW)
        acct = res["report"]["acct"]
        self.assertTrue(vault.zv_path(root / "reports" / "journal" / acct / "deals.jsonl").is_file())
        self.assertTrue(rl.read_deals(root, acct))
        pos = rl.build_positions(rl.read_deals(root, acct))
        self.assertTrue(rl.write_positions_csv(root, acct, pos))
        self.assertTrue(vault.zv_path(root / "reports" / "journal" / acct / "positions.csv").is_file())
        self.assertFalse(rl.write_positions_csv(root, acct, pos))

    def test_commit_messages_generic_when_on(self):
        set_vault(self.root)
        encrypt_calendar(self.root)
        msg = self.root / "msg.txt"
        pf = self.root / "payload.json"
        pf.write_text(json.dumps(fixture_json("payload_agg.json")))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(store_report.main(["--root", str(self.root), "--payload-file", str(pf), "--msg-out", str(msg)]), 0)
        self.assertEqual(msg.read_text(), "reports: update\n")
        with contextlib.redirect_stdout(io.StringIO()):
            build_latest.main(["--root", str(self.root), "--offline", "--now", iso_z(NOW), "--msg-file", str(msg)])
        self.assertEqual(msg.read_text(), "data: update\n")


class ReadersFailClosedTest(KeyEnv):
    """With an encrypted file present and no key, every reader stops with an error (never 'empty')."""

    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)
        set_vault(self.root)
        cal = self.root / "data/calendar/events_us.csv"
        vault.write_any(cal, cal.read_bytes(), True)
        store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        vault.write_text(self.root / "data/quality.json", json.dumps({"ok": True, "issues": []}))
        os.environ.pop("ZUMI_KEY")

    def test_library_readers_raise(self):
        with self.assertRaises(vault.VaultError):
            calendar_events.load_events(self.root / "data/calendar/events_us.csv")
        with self.assertRaises(vault.VaultError):
            rl.load_reports(self.root, dt.date(2026, 11, 24), dt.date(2026, 11, 26))
        with self.assertRaises(vault.VaultError):
            alerts.evaluate(self.root, json.loads((self.root / "config/settings.json").read_text()), NOW)
        with self.assertRaises(vault.VaultError):
            daily_summary.summarize(self.root, [dt.date(2026, 11, 25)])

    def test_cli_readers_exit_non_zero(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(coordinator_report.main(["--root", str(self.root), "--now", iso_z(NOW)]), 1)
            self.assertEqual(alerts.main(["--root", str(self.root), "--dry-run", "--now", iso_z(NOW)]), 1)
            self.assertEqual(build_latest.main(["--root", str(self.root), "--offline", "--now", iso_z(NOW)]), 1)
        self.assertIn("ZUMI_KEY is not set", err.getvalue())
        self.assertFalse((self.root / "reports" / "coord").exists())   # nothing written in plaintext


# ---------------------------------------------------------------- alerts redaction
class FakeGh:
    def __init__(self, bodies=None):
        self.bodies = list(bodies or [])

    def find_open_pr_with_label(self, label):
        return 7

    def comment_bodies(self, number):
        return list(self.bodies)

    def comment(self, number, body):
        self.bodies.append(body)


SAMPLE_ALERTS = [alerts.alert("streak:a1b2c3d4e5f60718:2026-11-25:5", "high", "EA thua 5 lệnh liên tiếp",
                              "EA losing streak of 5 trades"),
                 alerts.alert("vix_jump:2026-11-24", "high", "VIX tăng vọt 18.2 -> 24.9 (+36.8%)",
                              "VIX jump 18.2 -> 24.9 (+36.8%)"),
                 alerts.alert("event:CPI:2026-12-10T13:30:00Z", "info", "Sự kiện CPI", "Upcoming CPI")]


class AlertsRedactionTest(KeyEnv):
    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def cfg(self):
        return json.loads((self.root / "config/settings.json").read_text())

    def test_plain_comment_when_off(self):
        gh = FakeGh()
        self.assertEqual(alerts.post_alerts(gh, self.cfg(), SAMPLE_ALERTS, NOW, "main"), (7, 3))
        self.assertIn("VIX jump 18.2", gh.bodies[0])
        self.assertIn("zumi-alert-key: vix_jump:2026-11-24", gh.bodies[0])

    def test_redacted_comment_has_no_readable_data(self):
        set_vault(self.root)
        gh = FakeGh()
        self.assertEqual(alerts.post_alerts(gh, self.cfg(), SAMPLE_ALERTS, NOW, "main"), (7, 3))
        body = gh.bodies[0]
        for leak in ("VIX", "18.2", "streak", "a1b2c3d4e5f60718", "CPI", "2026-11-24", "2026-11-25:5", "thua", "EN:"):
            self.assertNotIn(leak, body)
        lines = [ln for ln in body.splitlines() if ln.startswith("- **[")]
        self.assertEqual(len(lines), 3)
        for ln in lines:
            self.assertRegex(ln, r"^- \*\*\[(HIGH|MEDIUM|INFO)\]\*\* `z[0-9a-f]{12}` ZV2:[A-Za-z0-9_-]+$")
        # Claude decrypts the comment with the key
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vault.cmd_alerts(body), 0)
        self.assertIn("VIX jump 18.2 -> 24.9", out.getvalue())
        self.assertIn("streak:a1b2c3d4e5f60718:2026-11-25:5", out.getvalue())
        # de-duplication on the opaque ids: nothing is posted twice
        self.assertEqual(alerts.post_alerts(gh, self.cfg(), SAMPLE_ALERTS, NOW, "main"), (7, 0))
        self.assertEqual(len(gh.bodies), 1)
        new = SAMPLE_ALERTS + [alerts.alert("dd:a1b2c3d4e5f60718:2026-11-25", "high", "dd 5.2%", "dd 5.2%")]
        self.assertEqual(alerts.post_alerts(gh, self.cfg(), new, NOW, "main"), (7, 1))
        self.assertNotIn("5.2", gh.bodies[1])

    def test_no_key_posts_only_a_data_free_notice_and_fails(self):
        set_vault(self.root)
        os.environ.pop("ZUMI_KEY")
        gh = FakeGh()
        with self.assertRaises(vault.VaultError):
            alerts.post_alerts(gh, self.cfg(), SAMPLE_ALERTS, NOW, "main")
        self.assertEqual(len(gh.bodies), 1)
        self.assertIn("vault_nokey", gh.bodies[0])
        for leak in ("VIX", "streak", "CPI"):
            self.assertNotIn(leak, gh.bodies[0])

    def test_actions_logs_redacted(self):
        set_vault(self.root)
        encrypt_calendar(self.root)
        store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(out):
            alerts.main(["--root", str(self.root), "--dry-run", "--now", "2026-11-25T08:00:00Z"])
        text = out.getvalue()
        self.assertRegex(text, r"\b(?:[1-9]\d*) alert\(s\) detected\n")    # count only, no keys
        self.assertIn("Zumi alert (encrypted)", text)                       # the dry-run comment is redacted too
        self.assertNotIn("a1b2c3d4e5f60718", text)
        self.assertNotIn("EN:", text)


# ---------------------------------------------------------------- migrate guard and doctor
class MigrateDoctorTest(KeyEnv):
    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel, text in (("docs/workflows.md", "# Workflows\n"), ("reports/daily/2026-11-25.md", "page\n"),
                          ("control/CHANGELOG.md", "# Control changelog\n"), ("data/quality.json", "{}\n"),
                          ("control/control.json", json.dumps(control_raw())), ("data/latest.json", "{}\n"),
                          ("docs/dashboard/index.html", "<html></html>\n"), ("AGENTS.md", "# agents\n")):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8")

    def migrate(self, *args) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = vault.main(["migrate", "--root", str(self.root), *args])
        return rc, out.getvalue() + err.getvalue()

    def test_refuses_without_key(self):
        set_vault(self.root)
        os.environ.pop("ZUMI_KEY")
        rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 2)
        self.assertIn("ZUMI_KEY is not set", text)
        self.assertEqual(list(self.root.rglob("*.zv")), [])

    def test_refuses_when_disabled_or_self_test_fails(self):
        set_vault(self.root, enabled=False)
        rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 2)
        self.assertIn("vault.enabled is false", text)
        set_vault(self.root)
        with mock.patch.object(vault, "self_test", return_value=["round trip with the configured key failed"]):
            rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 2)
        self.assertIn("round trip", text)
        self.assertEqual(list(self.root.rglob("*.zv")), [])

    def test_refuses_phase2_without_bridge_28(self):
        set_vault(self.root, phase=2, bridge="2.3")
        rc, text = self.migrate("--phase", "2")
        self.assertEqual(rc, 2)
        self.assertIn('ea_bridge_min "2.8"', text)
        set_vault(self.root, phase=1, bridge="2.8")
        rc, text = self.migrate("--phase", "2")
        self.assertEqual(rc, 2)
        self.assertIn("vault.phase 2", text)
        self.assertTrue((self.root / "control/control.json").is_file())

    def test_phase1_migration(self):
        set_vault(self.root)
        rc, text = self.migrate("--phase", "1", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would encrypt docs/workflows.md", text)
        self.assertEqual(list(self.root.rglob("*.zv")), [])
        rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 0, text)
        for rel in ("docs/workflows.md", "reports/daily/2026-11-25.md", "control/CHANGELOG.md", "data/quality.json",
                    "data/calendar/events_us.csv"):
            self.assertFalse((self.root / rel).exists(), rel)
            self.assertTrue(vault.zv_path(self.root / rel).is_file(), rel)
        for rel in ("control/control.json", "data/latest.json", "docs/dashboard/index.html", "AGENTS.md",
                    "config/settings.json"):
            self.assertTrue((self.root / rel).is_file(), rel)
            self.assertFalse(vault.zv_path(self.root / rel).exists(), rel)
        self.assertEqual(vault.read_text(self.root / "docs/workflows.md"), "# Workflows\n")
        self.assertNotIn(KEY_HEX, text)
        rc, _ = self.migrate("--phase", "1")                         # idempotent
        self.assertEqual(rc, 0)
        set_vault(self.root, phase=2, bridge="2.8")
        rc, text = self.migrate("--phase", "2")
        self.assertEqual(rc, 0, text)
        self.assertTrue(vault.zv_path(self.root / "control/control.json").is_file())
        self.assertEqual(control.load_json(self.root / "control/control.json")["seq"], 2)

    def doctor(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = vault.main(["doctor", "--root", str(self.root)])
        return rc, out.getvalue()

    def test_doctor_reports_without_printing_the_key(self):
        set_vault(self.root)
        rc, text = self.doctor()                                     # enabled, not migrated yet: readers refuse
        self.assertEqual(rc, 1, text)
        self.assertIn("reader calendar_events.load_events: FAIL", text)
        self.assertIn("plaintext refused", text)
        self.assertIn("docs/workflows.md", text)                     # would be encrypted
        self.assertEqual(self.migrate("--phase", "1")[0], 0)
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("key present: yes", text)
        self.assertIn(f"key fingerprint: {vault.fingerprint(KEY)}", text)
        self.assertIn("match", text)
        self.assertIn("reader calendar_events.load_events: OK", text)
        for secret in (KEY_HEX, KEY_HEX[:16]):
            self.assertNotIn(secret, text)
        os.environ.pop("ZUMI_KEY")
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("key present: no", text)

    def test_key_fp_recorded_and_compared(self):
        set_vault(self.root)
        rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 0, text)
        fp = vault.settings(self.root)["key_fp"]
        self.assertEqual(fp, vault.fingerprint(KEY))
        cfg_text = (self.root / "config/settings.json").read_text()
        self.assertNotIn(KEY_HEX, cfg_text)
        self.assertEqual(json.loads(cfg_text)["vault"]["key_fp"], fp)
        os.environ["ZUMI_KEY"] = OTHER_KEY_HEX                       # typo / wrong key: clear message, no key printed
        for args in (["check"], ["migrate", "--phase", "1"], ["doctor"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = vault.main([args[0], "--root", str(self.root), *args[1:]])
            text = out.getvalue() + err.getvalue()
            self.assertNotEqual(rc, 0, args)
            self.assertIn(vault.fingerprint(bytes.fromhex(OTHER_KEY_HEX)), text)
            self.assertIn(fp, text)
            self.assertNotIn(OTHER_KEY_HEX, text)

    def test_migrate_verifies_the_file_on_disk_before_unlinking(self):
        set_vault(self.root)
        real = vault._write_bytes_atomic

        def corrupt(path, data):                                     # simulate a bad write of the .zv
            real(path, data.replace(b"ZV2:", b"ZV2:A", 1) if path.name.endswith(".zv") else data)
        with mock.patch.object(vault, "_write_bytes_atomic", corrupt):
            rc, text = self.migrate("--phase", "1")
        self.assertEqual(rc, 1)
        self.assertIn("plaintext kept", text)
        self.assertEqual(list(self.root.rglob("*.zv")), [])          # nothing half-migrated
        self.assertTrue((self.root / "docs/workflows.md").is_file())


class SafetyRulesTest(KeyEnv):
    """Review fixes: a .zv is never replaced by plaintext; downgrade protection; generic public text."""

    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_zv_stays_encrypted_when_the_vault_is_disabled(self):
        set_vault(self.root)
        page = self.root / "reports/daily/2026-11-25.md"
        vault.write_text(page, "v1\n")
        set_vault(self.root, enabled=False)                          # config flipped off: still encrypted
        self.assertTrue(vault.write_text(page, "v2 secret\n"))
        self.assertFalse(page.exists())
        self.assertNotIn("secret", vault.zv_path(page).read_text())
        self.assertEqual(vault.read_text(page), "v2 secret\n")
        os.environ.pop("ZUMI_KEY")                                   # no key: fail, never write plaintext
        with self.assertRaises(vault.VaultError):
            vault.write_text(page, "v3 secret\n")
        self.assertFalse(page.exists())
        os.environ["ZUMI_KEY"] = KEY_HEX
        self.assertEqual(vault.read_text(page), "v2 secret\n")
        # every writer goes through write_text: e.g. daily_summary with the vault off keeps the .zv
        store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        daily_summary.summarize(self.root, [dt.date(2026, 11, 25)])
        self.assertTrue(vault.zv_path(page).is_file())
        self.assertFalse(page.exists())

    def test_only_dec_decrypts_and_it_must_be_confirmed(self):
        set_vault(self.root)
        doc = self.root / "docs/a.md"
        vault.write_text(doc, "doc\n")
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), contextlib.redirect_stderr(err):
            self.assertEqual(vault.main(["dec", str(doc)]), 2)        # not interactive, no --yes-plaintext
        self.assertIn("refused", err.getvalue())
        self.assertTrue(vault.zv_path(doc).is_file())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vault.main(["dec", "--yes-plaintext", str(doc)]), 0)
        self.assertEqual(doc.read_text(), "doc\n")
        self.assertFalse(vault.zv_path(doc).exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vault.main(["enc", str(doc)]), 0)
        self.assertTrue(vault.zv_path(doc).is_file())

    def test_downgrade_plaintext_refused(self):
        set_vault(self.root)
        q = self.root / "data/quality.json"
        q.write_text('{"ok": true, "issues": []}\n')                # .zv missing, plaintext planted
        with self.assertRaises(vault.VaultError) as cm:
            vault.read_text(q)
        self.assertIn("plaintext refused", str(cm.exception))
        with self.assertRaises(vault.VaultError):
            alerts._load_json(q)
        # latch: once migrated (key_fp recorded) the vault stays active even if `enabled` is flipped off
        set_vault(self.root, enabled=False, key_fp=vault.fingerprint(KEY))
        self.assertTrue(vault.enabled(self.root))
        with self.assertRaises(vault.VaultError):
            vault.read_text(q)
        # vault fully off (no key_fp): plaintext is the normal case
        set_vault(self.root, enabled=False)
        self.assertEqual(json.loads(vault.read_text(q))["ok"], True)
        # out-of-scope files stay plaintext even with the vault on
        set_vault(self.root)
        (self.root / "data/latest.json").write_text("{}\n")
        self.assertEqual(vault.read_text(self.root / "data/latest.json"), "{}\n")

    def test_public_text_generic_while_any_zv_exists(self):
        set_vault(self.root)
        vault.write_text(self.root / "docs/a.md", "x\n")
        set_vault(self.root, enabled=False)                          # off, but a .zv is still in the repo
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(vault.public_log(self.root, "detail", "generic"), "generic")
        self.assertEqual(vault.public_log(self.root, "detail", "generic"), "detail")   # local console
        self.assertEqual(vault.commit_message(self.root, "data: DGS10 4.92->4.91", "data"), "data: update")

    def test_check_gate_for_workflows(self):
        out, err = io.StringIO(), io.StringIO()
        os.environ.pop("ZUMI_KEY")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(vault.main(["check", "--root", str(self.root)]), 0)   # vault off, no .zv: no key needed
            set_vault(self.root)
            self.assertEqual(vault.main(["check", "--root", str(self.root)]), 1)   # on without key: fail fast
        self.assertIn("ZUMI_KEY is not set", err.getvalue())
        os.environ["ZUMI_KEY"] = KEY_HEX
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(vault.main(["check", "--root", str(self.root)]), 1)   # plaintext left in the vault set
        self.assertIn("should be encrypted", err.getvalue())

    def test_dashboard_page_shows_the_encrypted_notice(self):
        from helpers import REPO
        html = (REPO / "docs/dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("data.json.zv", html)
        self.assertIn('startsWith("ZV2:")', html)
        self.assertIn("dữ liệu đã mã hóa – xem bằng vault.py", html)
        v = vault.settings(json.loads((REPO / "config/settings.json").read_text()))
        self.assertTrue(vault.in_scope("docs/dashboard/data.json", v))           # the data stays encrypted
        self.assertFalse(vault.in_scope("docs/dashboard/index.html", v))
        self.assertFalse(v["enabled"])                                            # shipped OFF
        self.assertEqual(v["key_fp"], "")


class AlertsSplitTest(KeyEnv):
    def setUp(self):
        super().setUp()
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def many(self, n=40):
        return [alerts.alert(f"rule:a1b2c3d4e5f60718:2026-11-25:r{i}:1", "medium", "x" * 300, "y" * 300)
                for i in range(n)]

    def test_split_into_parts_without_cutting(self):
        for redact in (False, True):
            bodies = alerts.render_comments(self.many(), NOW, redact=redact, limit=4000)
            self.assertGreater(len(bodies), 1)
            n = len(bodies)
            for i, b in enumerate(bodies, 1):
                self.assertLessEqual(len(b), 4000)
                self.assertIn(f"(part {i}/{n})", b.splitlines()[0])
            markers = [m for b in bodies for m in alerts.KEY_RE.findall(b)]
            self.assertEqual(len(markers), 40)
            self.assertEqual(len(set(markers)), 40)                  # every alert exactly once, with its marker
            if redact:
                self.assertEqual(sum(len(vault.ALERT_RE.findall(b)) for b in bodies), 40)

    def test_post_counts_and_never_reposts(self):
        set_vault(self.root)
        cfg = json.loads((self.root / "config/settings.json").read_text())
        gh = FakeGh()
        orig = alerts.render_comments
        with mock.patch.object(alerts, "render_comments",
                               lambda a, now, redact=False, key=None: orig(a, now, redact, key, limit=4000)):
            self.assertEqual(alerts.post_alerts(gh, cfg, self.many(), NOW, "main"), (7, 40))
            parts = len(gh.bodies)
            self.assertGreater(parts, 1)
            self.assertEqual(alerts.post_alerts(gh, cfg, self.many(), NOW, "main"), (7, 0))
            self.assertEqual(len(gh.bodies), parts)                  # nothing re-posted on the next run

if __name__ == "__main__":
    unittest.main()
