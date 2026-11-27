"""End-to-end smoke test of the vault on a temporary COPY of the repository (offline, dummy key).

The same sequence runs twice, vault off and vault on (phase 1, after `vault.py migrate --phase 1`):
store an EA v2 report -> daily_summary -> coordinator_report (dry run) -> control.py normalize / effective ->
agents_status -> alerts (evaluate + render). Every output must be identical once decrypted, and control.json /
latest.json must stay plaintext in phase 1 (the live EA runs bridge 2.3).
"""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, fixture_json

import alerts
import control
import coordinator_report
import daily_summary
import store_report
import vault
from zumi_common import iso_z, parse_utc

KEY_HEX = "5a" * 32                       # dummy test key
NOW = parse_utc("2026-11-27T14:00:00Z")   # after control.json's updated_utc, before its expiry
OUTPUT_DIRS = ("reports", "data", "control", "docs")


def agents_status_vault_aware() -> bool:
    src = (REPO / "scripts" / "agents_status.py").read_text(encoding="utf-8")
    return bool(re.search(r"^import vault\b", src, re.M))


class VaultEndToEndTest(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ZUMI_KEY", "GITHUB_ACTIONS")}
        env["ZUMI_KEY"] = KEY_HEX
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy_repo(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="sakura_e2e_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "repo"
        shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.zv"))
        return root

    def enable_vault(self, root: Path) -> None:
        path = root / "config" / "settings.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["vault"] = {"enabled": True, "phase": 1, "ea_bridge_min": "2.3",
                        "paths": ["reports/**", "docs/**", "control/CHANGELOG.md", "reports/coord/**",
                                  "reports/agents/**", "data/calendar/**", "data/quality.json"],
                        "phase2_paths": ["control/control.json", "data/latest.json"],
                        "plain_exceptions": ["docs/dashboard/index.html"], "generic_commit_messages": True}
        path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = vault.main(["migrate", "--phase", "1", "--root", str(root)])
        self.assertEqual(rc, 0, out.getvalue())

    def run_sequence(self, root: Path) -> dict:
        """The production order of the workflows, all offline. Returns the console results to compare."""
        res = {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            payload = fixture_json("payload_agg.json")
            payload["ts"] = int(NOW.timestamp()) - 3600            # fresh report: the aggregate alert rules fire
            stored = store_report.store(root, payload, NOW)
            res["dates"] = [d.isoformat() for d in stored["dates"]]
            daily_summary.summarize(root, stored["dates"])
            self.assertEqual(coordinator_report.main(["--root", str(root), "--now", iso_z(NOW)]), 0, out.getvalue())
            self.assertEqual(control.main(["normalize", "--root", str(root), "--now", iso_z(NOW), "--prev-ref", ""]), 0,
                             out.getvalue())
        eff = io.StringIO()
        with contextlib.redirect_stdout(eff):
            self.assertEqual(control.main(["effective", "--root", str(root), "--now", iso_z(NOW)]), 0)
        res["effective"] = json.loads(eff.getvalue())
        import agents_status
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(agents_status.main(["--root", str(root), "--now", iso_z(NOW)]), 0)
        cfg = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8"))
        res["alerts"] = alerts.evaluate(root, cfg, NOW)
        res["comment"] = alerts.render_comment(res["alerts"], NOW, redact=vault.enabled(cfg))
        return res

    def snapshot(self, root: Path) -> dict:
        """Decrypted content of every output file, by logical path."""
        out = {}
        for d in OUTPUT_DIRS:
            for p in sorted((root / d).rglob("*")):
                if p.is_file():
                    rel = vault.logical(p.relative_to(root).as_posix())
                    out[rel] = vault.read_any(p, root)
        return out

    def test_agents_status_goes_through_the_vault(self):
        self.assertTrue(agents_status_vault_aware())

    def test_outputs_identical_with_vault_on_and_off(self):
        off_root, on_root = self.copy_repo(), self.copy_repo()
        self.enable_vault(on_root)
        off, on = self.run_sequence(off_root), self.run_sequence(on_root)

        # 1) every file the steps read or wrote is identical once decrypted
        snap_off, snap_on = self.snapshot(off_root), self.snapshot(on_root)
        self.assertEqual(sorted(snap_off), sorted(snap_on))
        for rel in snap_off:
            self.assertEqual(snap_off[rel], snap_on[rel], rel)
        for rel in (f"reports/daily/{off['dates'][-1]}.md", f"reports/coord/{NOW.date()}.md", "control/CHANGELOG.md"):
            self.assertIn(rel, snap_on)

        # 2) console results identical
        self.assertEqual(off["dates"], on["dates"])
        self.assertEqual(off["effective"], on["effective"])
        self.assertEqual(off["alerts"], on["alerts"])
        self.assertTrue(off["alerts"])

        # 3) the alerts comment: plaintext when off; ids + severity + ZV2 only when on, decrypting to the same alerts
        self.assertIn(off["alerts"][0]["en"], off["comment"])
        for al in on["alerts"]:
            self.assertNotIn(al["key"], on["comment"])
            self.assertNotIn(al["en"], on["comment"])
        blobs = re.findall(r"`(z[0-9a-f]{12})` (ZV2:[A-Za-z0-9_-]+)", on["comment"])
        opened = sorted((json.loads(vault.decrypt(f"alerts/{aid}", z)) for aid, z in blobs), key=lambda a: a["key"])
        self.assertEqual(opened, sorted(({k: a[k] for k in ("key", "level", "vi", "en")} for a in off["alerts"]),
                                        key=lambda a: a["key"]))

        # 4) phase 1: what the live EA (bridge 2.3) reads stays plaintext
        for rel in ("control/control.json", "data/latest.json"):
            self.assertTrue((on_root / rel).is_file(), rel)
            self.assertFalse(vault.zv_path(on_root / rel).exists(), rel)
            json.loads((on_root / rel).read_text(encoding="utf-8"))
        self.assertEqual(json.loads((on_root / "data/latest.json").read_text())["schema"], "zumi.latest")

        # 5) vault on: nothing that phase 1 selects is left in plaintext, and every .zv decrypts
        v = vault.settings(on_root)
        self.assertEqual(vault.pending_files(on_root, v, 1), [])
        self.assertTrue(vault.zv_path(on_root / f"reports/agents/{NOW.date()}.md").is_file())
        self.assertEqual(vault.check(on_root), [])
        self.assertTrue(vault.zv_path(on_root / "docs/workflows.md").is_file())
        self.assertTrue((on_root / "AGENTS.md").is_file())
        self.assertEqual(list(off_root.rglob("*.zv")), [])

if __name__ == "__main__":
    unittest.main()
