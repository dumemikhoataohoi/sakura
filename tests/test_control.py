"""Control channel: validator bounds, expiry, NEUTRAL fallback, seq, changelog, coordinator rules."""
import copy
import datetime as dt
import json
import re
import shutil
import subprocess
import unittest

from helpers import REPO, fixture_json, make_root

import control
import coordinator_report
import store_report
from zumi_common import iso_z, parse_utc

NOW = parse_utc("2026-11-25T16:00:00Z")


def base(**over):
    obj = {"v": 1, "author": "claude", "seq": 2, "updated_utc": iso_z(NOW),
           "global": {"risk_mult": 1.0, "pause_all": False}, "methods": {}, "news_blocks": [], "notes_vi": "ok"}
    obj.update(over)
    return obj


class ValidatorTest(unittest.TestCase):
    def ok(self, obj):
        norm, errors, warnings = control.normalize(obj, NOW)
        self.assertEqual(errors, [], errors)
        return norm, warnings

    def bad(self, obj, fragment):
        norm, errors, _ = control.normalize(obj, NOW)
        self.assertIsNone(norm)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_defaults_and_expiry(self):
        norm, _ = self.ok(base(methods={"M003": {"mode": "pause", "reason_code": "R01"}}))
        self.assertEqual(norm["global"]["expires_utc"], "2026-11-27T16:00:00Z")          # default 48 h
        self.assertEqual(norm["methods"]["M003"]["risk_mult"], 0.0)
        norm, warnings = self.ok(base(methods={"M003": {"mode": "reduce", "risk_mult": 0.5, "reason_code": "R05",
                                                        "expires_utc": "2026-12-30T00:00:00Z"}}))
        self.assertEqual(norm["methods"]["M003"]["expires_utc"], "2026-12-02T16:00:00Z")   # clamped to 7 days
        self.assertTrue(warnings)
        self.bad(base(methods={"M003": {"mode": "pause", "reason_code": "R01", "expires_utc": "2026-11-25T10:00:00Z"}}),
                 "must be after updated_utc")

    def test_multiplier_bounds_and_modes(self):
        norm, warnings = self.ok(base(**{"global": {"risk_mult": 3.0, "reason_code": "R07"}}))
        self.assertEqual(norm["global"]["risk_mult"], control.MAX_MULT)
        self.assertTrue(any("clamped" in w for w in warnings))
        norm, _ = self.ok(base(**{"global": {"risk_mult": -1, "reason_code": "R05"}}))
        self.assertEqual(norm["global"]["risk_mult"], 0.0)
        norm, _ = self.ok(base(methods={"M010": {"mode": "boost", "risk_mult": 1.5, "phases": ["TREND"], "reason_code": "R07"},
                                        "M011": {"mode": "on", "risk_mult": 1.7, "reason_code": "R08"}}))
        self.assertEqual(norm["methods"]["M010"]["risk_mult"], 1.5)
        self.assertEqual(norm["methods"]["M011"]["risk_mult"], 1.0)   # "on" never scales risk
        self.bad(base(methods={"M010": {"mode": "boost", "risk_mult": 0.8, "reason_code": "R07"}}), "boost needs")
        self.bad(base(methods={"M010": {"mode": "reduce", "risk_mult": 1.2, "reason_code": "R05"}}), "reduce needs")
        self.bad(base(methods={"M010": {"mode": "open", "reason_code": "R07"}}), "mode must be")
        self.bad(base(**{"global": {"risk_mult": 0.5}}), "reason code")   # non-neutral global needs a reason

    def test_codes_only_and_public_text(self):
        self.bad(base(methods={"London breakout": {"mode": "pause", "reason_code": "R01"}}), "method codes")
        self.bad(base(methods={"M003": {"mode": "pause", "reason_code": "R99"}}), "unknown reason code")
        self.bad(base(notes_vi="Vàng tại 4100.5"), "prices")
        self.bad(base(notes_vi="mua ở 4100"), "prices")
        self.ok(base(notes_vi="Tạm dừng M003 tới 2026-11-27 13:30 UTC"))
        self.bad(base(author="someone"), "author")
        self.bad(base(extra=1), "unknown top-level key")
        self.bad(base(updated_utc="2026-11-26T16:00:00Z"), "future")

    def test_news_windows(self):
        norm, _ = self.ok(base(news_blocks=[
            {"kind": "allow", "start_utc": "2026-11-27T13:00:00Z", "end_utc": "2026-11-27T15:00:00Z", "reason_code": "R07"},
            {"start_utc": "2026-11-26T13:00:00Z", "end_utc": "2026-11-26T14:00:00Z", "reason_code": "R03"}]))
        self.assertEqual([w["kind"] for w in norm["news_blocks"]], ["block", "allow"])
        self.bad(base(news_blocks=[{"kind": "allow", "start_utc": "2026-11-27T10:00:00Z", "end_utc": "2026-11-27T15:00:00Z",
                                    "reason_code": "R03"}]), "at most 4 h")
        self.bad(base(news_blocks=[{"kind": "allow", "start_utc": "2026-11-27T13:00:00Z", "end_utc": "2026-11-27T14:00:00Z",
                                    "reason_code": "R01"}]), "R03 or R07")
        self.bad(base(news_blocks=[{"start_utc": "2026-11-27T13:00:00Z", "end_utc": "2026-11-28T14:00:00Z",
                                    "reason_code": "R03"}]), "at most 24 h")
        self.bad(base(news_blocks=[{"start_utc": "2026-11-27T13:00:00Z", "end_utc": "2026-11-27T12:00:00Z",
                                    "reason_code": "R03"}]), "after start")

    def test_reason_codes_doc_matches(self):
        text = (REPO / "control" / "reason_codes.md").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"^\| (R\d\d) \|", text, re.M)), set(control.REASON_CODES))

    def test_repo_control_file_is_valid_and_normalized(self):
        raw = json.loads((REPO / "control" / "control.json").read_text(encoding="utf-8"))
        norm, errors, _ = control.normalize(raw)
        self.assertEqual(errors, [])
        self.assertEqual(norm, raw)


class EffectiveTest(unittest.TestCase):
    def setUp(self):
        self.norm, errors, _ = control.normalize(base(
            **{"global": {"risk_mult": 0.5, "reason_code": "R05", "expires_utc": "2026-11-26T16:00:00Z"}},
            methods={"M003": {"mode": "pause", "phases": ["TREND"], "reason_code": "R01"},
                     "M010": {"mode": "boost", "risk_mult": 1.5, "reason_code": "R07", "expires_utc": "2026-11-26T00:00:00Z"}}), NOW)
        self.assertEqual(errors, [])

    def test_neutral_fallbacks(self):
        self.assertTrue(control.effective(None, NOW)["neutral"])                                    # missing / invalid
        self.assertTrue(control.effective(self.norm, NOW + dt.timedelta(hours=73))["neutral"])      # stale
        eff = control.effective(self.norm, NOW + dt.timedelta(hours=20))                            # partly expired
        self.assertEqual(eff["global"]["risk_mult"], 0.5)
        self.assertNotIn("M010", eff["methods"])
        self.assertIn("M003", eff["methods"])
        eff = control.effective(self.norm, NOW + dt.timedelta(hours=49))
        self.assertTrue(eff["neutral"])                                                             # all expired

    def test_risk_mult_cap_and_phases(self):
        eff = control.effective(self.norm, NOW)
        self.assertEqual(control.risk_mult(eff, "M003", "TREND"), 0.0)       # paused in TREND
        self.assertEqual(control.risk_mult(eff, "M003", "RANGE"), 0.5)       # other phase: global only
        self.assertEqual(control.risk_mult(eff, "M010", "TREND", cap=1.0), 0.75)
        eff["global"]["risk_mult"] = 1.0
        self.assertEqual(control.risk_mult(eff, "M010", "TREND", cap=1.0), 1.0)   # EA cap 1.0 blocks any increase
        self.assertEqual(control.risk_mult(eff, "M010", "TREND", cap=2.0), 1.5)
        eff["global"]["pause_all"] = True
        self.assertEqual(control.risk_mult(eff, "M999", "X", cap=2.0), 0.0)


class SeqAndChangelogTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        (self.root / "control").mkdir()
        # Seed with a fixed neutral file, not the live control.json (that one changes with every directive).
        seed, _, _ = control.normalize(base(seq=1))
        (self.root / "control" / "control.json").write_text(json.dumps(seed, indent=2, sort_keys=True) + "\n")
        if not shutil.which("git"):
            self.skipTest("git not available")
        for args in (["init", "-q", "-b", "main"], ["add", "."],
                     ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "seed"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.prev = json.loads((self.root / "control" / "control.json").read_text())

    def write(self, obj):
        (self.root / "control" / "control.json").write_text(json.dumps(obj))

    def test_seq_checks(self):
        prev, _, _ = control.normalize(self.prev)
        same = copy.deepcopy(prev)
        self.assertEqual(control.check_seq(prev, same), [])
        changed = copy.deepcopy(prev)
        changed["global"]["risk_mult"] = 0.5
        self.assertTrue(control.check_seq(prev, changed))                     # changed without a new seq
        back = copy.deepcopy(prev)
        back["seq"] = 0
        self.assertTrue(control.check_seq(prev, back))

    def test_normalize_writes_changelog_and_check_passes(self):
        now = parse_utc(self.prev["updated_utc"]) + dt.timedelta(hours=1)
        new = base(seq=self.prev["seq"] + 1, updated_utc=iso_z(now),
                   methods={"M003": {"mode": "pause", "phases": ["TREND"], "reason_code": "R01"}})
        self.write(new)
        self.assertEqual(control.cmd_normalize(self.root, now, "HEAD"), 0)
        log = (self.root / "control" / "CHANGELOG.md").read_text()
        self.assertRegex(log, r"seq \d+: M003 pause x0\.00 \[TREND\] until .*\(R01\)")
        self.assertEqual(control.cmd_check(self.root, now, "HEAD", alert=False), 0)
        stale_seq = dict(new, seq=self.prev["seq"])                                # change without seq bump
        self.write(stale_seq)
        self.assertEqual(control.cmd_normalize(self.root, now, "HEAD"), 1)
        self.write(new)                                                           # valid but not normalized
        self.assertEqual(control.cmd_check(self.root, now, "HEAD", alert=False), 1)


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        cfg_path = self.root / "config" / "settings.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["coordinator"]["enable_codes"] = {"M120": ["TREND"]}
        cfg_path.write_text(json.dumps(cfg))
        payload = fixture_json("payload_agg.json")
        g = lambda n, w, r, cur: {"n": n, "wins": w, "net_r": r, "cur_ls": cur, "l20_n": min(n, 20), "l20_wins": w}
        payload["data"]["periods"]["r30"] = {"n": 60, "wins": 30, "losses": 30, "be": 0, "by_method_phase": {
            "M001:TREND": g(8, 3, -2.0, 5),      # R01 streak
            "M002:RANGE": g(12, 3, -4.0, 1),     # R02 weak win%
            "M003:TREND": g(18, 13, 9.0, 0),     # R07 boost
            "M005:TREND": g(10, 6, 2.0, 0),      # recovered -> R08
            "M000:none": g(5, 0, -5.0, 5)}}      # unknown code: never touched
        store_report.store(self.root, payload, NOW)
        (self.root / "control").mkdir()
        cur = base(seq=7, updated_utc=iso_z(NOW - dt.timedelta(hours=60)), methods={
            "M004": {"mode": "pause", "reason_code": "R01", "expires_utc": iso_z(NOW - dt.timedelta(hours=1))},
            "M005": {"mode": "pause", "phases": ["TREND"], "reason_code": "R02", "expires_utc": iso_z(NOW + dt.timedelta(hours=5))}})
        norm, errors, _ = control.normalize(cur, NOW)
        self.assertEqual(errors, [])
        (self.root / "control" / "control.json").write_text(json.dumps(norm))

    def test_rules(self):
        res = coordinator_report.evaluate(self.root, NOW)
        rules = {(h["rule"], h.get("code")) for h in res["hits"]}
        for item in (("R01", "M001"), ("R02", "M002"), ("R07", "M003"), ("R08", "M005"), ("R05", "*"), ("R07", "M120")):
            self.assertIn(item, rules)
        p = res["proposal"]
        self.assertIsNotNone(p, res["errors"])
        self.assertEqual(p["seq"], 8)
        self.assertEqual(p["methods"]["M001"]["mode"], "pause")
        self.assertEqual(p["methods"]["M001"]["reason_code"], "R01")
        self.assertEqual(p["methods"]["M002"]["phases"], ["RANGE"])
        self.assertEqual((p["methods"]["M003"]["mode"], p["methods"]["M003"]["risk_mult"]), ("boost", 1.25))
        self.assertEqual((p["methods"]["M005"]["mode"], p["methods"]["M005"]["reason_code"]), ("on", "R08"))
        self.assertEqual(p["methods"]["M120"]["mode"], "on")
        self.assertNotIn("M004", p["methods"])            # expired directive dropped = method restored
        self.assertIn("M004", res["restored"])
        self.assertNotIn("M000", p["methods"])
        self.assertEqual((p["global"]["risk_mult"], p["global"]["reason_code"]), (0.5, "R05"))
        self.assertTrue(any(w["kind"] == "block" and w["reason_code"] == "R03" and w["start_utc"] == "2026-11-27T13:00:00Z"
                            for w in p["news_blocks"]))

    def test_report_files_and_apply(self):
        self.assertEqual(coordinator_report.main(["--root", str(self.root), "--now", iso_z(NOW), "--apply"]), 0)
        md = (self.root / "reports/coord/2026-11-25.md").read_text()
        self.assertIn("| R01 | pause | M001 | TREND |", md)
        self.assertIn("```json", md)
        applied = json.loads((self.root / "control/control.json").read_text())
        self.assertEqual(applied["seq"], 8)
        self.assertEqual(control.normalize(applied)[0], applied)
        self.assertIn("seq 8:", (self.root / "control/CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
