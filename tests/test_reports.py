import datetime as dt
import json
import unittest

from helpers import fixture_json, make_root

import daily_summary
import journal_stats as js
import reports_lib as rl
import store_report
from zumi_common import parse_utc

NOW = parse_utc("2026-11-26T11:00:00Z")
FAKE_TOKEN = "ghp_" + "Z" * 36  # built at runtime: no token-like literal in the repo


class SanitizeTest(unittest.TestCase):
    def setUp(self):
        self.rcfg = {"max_str_len": 400, "max_trades_per_part": 500, "max_log_lines": 400}

    def test_privacy_rules(self):
        p = fixture_json("payload_basic.json")
        p["data"]["log"].append({"t": 1795600000, "m": f"token {FAKE_TOKEN} leaked"})
        rep = rl.sanitize(p, self.rcfg, NOW)
        self.assertEqual(rep["acct"], "a1b2c3d4e5f60718")
        self.assertNotIn("balance", rep["data"]["eq"])
        self.assertNotIn("pl", rep["data"]["trades"][0])
        self.assertIn("pl_pct", rep["data"]["trades"][0])
        text = json.dumps(rep)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("token *** leaked", text)

    def test_clear_login_is_hashed_and_dropped(self):
        p = {"kind": "ea_report", "login": 12345678, "ts": 1795600000, "data": {"trades": []}}
        rep = rl.sanitize(p, self.rcfg, NOW)
        self.assertEqual(rep["acct"], rl.account_hash("12345678"))
        self.assertNotIn("12345678", json.dumps(rep))

    def test_rejects_wrong_kind(self):
        with self.assertRaises(rl.PayloadError):
            rl.sanitize({"kind": "other"}, self.rcfg, NOW)

    def test_workflow_dispatch_event(self):
        ev = {"inputs": {"payload": json.dumps(fixture_json("payload_partial.json"))}}
        self.assertEqual(rl.payload_from_event(ev)["part"], 1)
        with self.assertRaises(rl.PayloadError):
            rl.payload_from_event({"inputs": {"payload": "{not json"}})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def test_path_and_idempotency(self):
        res = store_report.store(self.root, fixture_json("payload_basic.json"), NOW)
        self.assertEqual(res["path"].relative_to(self.root).as_posix(),
                         "reports/2026/11/25/a1b2c3d4e5f60718_20261125T152000Z.json")
        res2 = store_report.store(self.root, fixture_json("payload_basic.json"), NOW)
        self.assertEqual(res["path"], res2["path"])
        self.assertEqual(len(list((self.root / "reports/2026/11/25").glob("*.json"))), 1)
        deals = rl.read_deals(self.root, "a1b2c3d4e5f60718")
        self.assertEqual(sorted(deals), [1001, 1002, 1003, 1004])
        self.assertEqual([d.isoformat() for d in res["dates"]], ["2026-11-25"])

    def test_partial_close_reconstruction(self):
        r1 = store_report.store(self.root, fixture_json("payload_partial.json"), NOW)
        self.assertTrue(r1["path"].name.endswith("_p1of2.json"))
        pos = rl.build_positions(rl.read_deals(self.root, "a1b2c3d4e5f60718"))
        self.assertFalse(pos[0]["closed"])  # only half closed so far
        store_report.store(self.root, fixture_json("payload_partial2.json"), NOW)
        store_report.store(self.root, fixture_json("payload_partial2.json"), NOW)  # resend: no duplicate
        pos = rl.build_positions(rl.read_deals(self.root, "a1b2c3d4e5f60718"))
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertTrue(p["closed"])
        self.assertEqual(p["n_exits"], 2)
        self.assertAlmostEqual(p["exit"], 4115.0)
        self.assertAlmostEqual(p["r"], 1.5)
        self.assertAlmostEqual(p["pl_pct"], 1.5)
        self.assertEqual(p["mfe_r"], 2.1)


class StatsTest(unittest.TestCase):
    @staticmethod
    def P(pos, pl, r, t):
        return {"pos": pos, "pl_pct": pl, "r": r, "t_out": t, "t_in": t - 600, "sl": 1.0, "risk_pct": 0.5,
                "method": "m", "phase": "p", "magic": 1}

    def test_metrics(self):
        ps = [self.P(1, 1.0, 2.0, 100), self.P(2, -0.5, -1.0, 200), self.P(3, -0.5, -1.0, 300), self.P(4, 0.0, 0.0, 400)]
        m = js.metrics(ps)
        self.assertEqual((m["wins"], m["losses"], m["be"]), (1, 2, 1))
        self.assertAlmostEqual(m["win_pct"], 33.3)
        self.assertAlmostEqual(m["pf"], 1.0)
        self.assertAlmostEqual(m["exp_r"], 0.0)
        self.assertEqual(m["max_loss_streak"], 2)
        self.assertEqual(m["end_loss_streak"], 0)  # BE breaks the streak

    def test_drawdown_episodes(self):
        ps = [self.P(1, 2.0, 2, 100), self.P(2, -1.5, -1, 200), self.P(3, -0.5, -1, 300), self.P(4, 3.0, 3, 400),
              self.P(5, -1.2, -1, 500)]
        eps = js.drawdown_episodes(ps, 1.0)
        self.assertEqual(len(eps), 2)
        self.assertEqual(eps[0]["depth_pct"], 2.0)
        self.assertEqual(eps[0]["end"], 400)
        self.assertIsNone(eps[1]["end"])


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        store_report.store(self.root, fixture_json("payload_basic.json"), NOW)
        store_report.store(self.root, fixture_json("payload_partial.json"), NOW)
        store_report.store(self.root, fixture_json("payload_partial2.json"), NOW)

    def test_pages(self):
        written = daily_summary.summarize(self.root, [dt.date(2026, 11, 25), dt.date(2026, 11, 26)])
        names = {p.relative_to(self.root).as_posix() for p in written}
        self.assertIn("reports/daily/2026-11-25.md", names)
        self.assertIn("reports/weekly/2026-W48.md", names)
        self.assertIn("reports/monthly/2026-11.md", names)
        self.assertIn("docs/dashboard/data.json", names)
        day = (self.root / "reports/daily/2026-11-25.md").read_text()
        self.assertIn("| 4 | 0 / 4 / 0 | 0.0 |", day)
        for rule in ("no_sl", "risk_cap", "news", "daily_cap"):
            self.assertIn(f"**{rule}**", day)
        self.assertIn("PHASE RANGE -> TREND", day)
        self.assertIn("spread_wide", day)
        self.assertIn("By method", day)
        self.assertIn("M3 breakout", day)
        self.assertIn("Losing streak across the journal at the end of this period: **4**", day)
        week = (self.root / "reports/weekly/2026-W48.md").read_text()
        self.assertIn("Weekday x hour", week)
        self.assertIn("| 5 | 1 / 4 / 0 | 20.0 |", week)
        # deterministic: a second run writes nothing
        self.assertEqual(daily_summary.summarize(self.root, [dt.date(2026, 11, 25), dt.date(2026, 11, 26)]), [])
        dash = json.loads((self.root / "docs/dashboard/data.json").read_text())
        self.assertEqual(dash["accounts"]["a1b2c3d4e5f60718"]["metrics"]["n"], 5)
        self.assertTrue((self.root / "reports/journal/a1b2c3d4e5f60718/positions.csv").exists())


if __name__ == "__main__":
    unittest.main()
