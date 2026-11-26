"""Aggregate (v2) reports: validator, merging, pages, dashboard and alerts. Public-mode repo."""
import copy
import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stderr

from helpers import FakeDataSession, fixture_json, make_root

import agg
import alerts
import build_latest
import daily_summary
import reports_lib as rl
import store_report
from zumi_common import load_config, parse_utc

NOW = parse_utc("2026-11-25T16:00:00Z")
ACCT = "a1b2c3d4e5f60718"


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()   # public mode (the repository is public)

    def test_clean_payload_roundtrip(self):
        res = store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        self.assertEqual(res["stripped"], [])
        self.assertEqual(res["path"].relative_to(self.root).as_posix(),
                         f"reports/2026/11/25/{ACCT}_20261125T150000Z.json")
        rep = json.loads(res["path"].read_text())
        self.assertEqual((rep["v"], rep["kind"]), (2, "ea_agg"))
        self.assertEqual(rep["data"]["periods"]["day"]["n"], 4)
        self.assertEqual(rep["data"]["periods"]["mtd"]["grid"]["Mon"]["08"], {"n": 5, "wins": 3, "win_pct": 60.0})
        self.assertEqual(rep["data"]["periods"]["mtd"]["grid"]["Mon"]["12"], {"n": 2})  # small cell: count only
        self.assertEqual([d.isoformat() for d in res["dates"]], ["2026-11-25"])
        self.assertFalse((self.root / "reports/journal").exists())  # no per-trade journal in public mode

    def test_strips_per_trade_fields_and_raw_method_names(self):
        res = store_report.store(self.root, fixture_json("payload_agg_dirty.json"), NOW)
        stripped = set(res["stripped"])
        for key in ("login", "data.trades", "data.log", "data.eq.balance", "data.mkt.bid",
                    "data.periods.day.trades", "data.periods.quarter", "data.periods.mtd.grid.Mon.09"):
            self.assertIn(key, stripped)
        self.assertFalse(any("London" in k or "breakout" in k for k in stripped))  # the log never echoes names
        self.assertIn("data.periods.day.by_method.<renamed>", stripped)
        text = res["path"].read_text()
        for leaked in ("London", "breakout", "4100", "1795570000", "12345678", "10000", "4190", "ticket", "t_in",
                       "price", "\"trades\"", "\"log\""):
            self.assertNotIn(leaked, text)
        rep = json.loads(text)
        methods = rep["data"]["periods"]["day"]["by_method"]
        self.assertTrue(all(agg.METHOD_RE.match(k) for k in methods))
        self.assertEqual(methods["M000"]["n"], 4)  # 1 original + 3 folded from the raw name
        self.assertEqual(rep["data"]["periods"]["mtd"]["grid"]["Tue"]["04"], {"n": 2})

    def test_per_trade_v1_payload_rejected_in_public_mode(self):
        with self.assertRaises(rl.PayloadError):
            store_report.store(self.root, fixture_json("payload_basic.json"), NOW)
        payload = fixture_json("payload_agg.json")
        payload["kind"] = "ea_report"   # a v1 report pretending to be anything else is rejected too
        with self.assertRaises(rl.PayloadError):
            store_report.store(self.root, payload, NOW)
        err = io.StringIO()
        from helpers import FIXTURES
        with redirect_stderr(err):
            code = store_report.main(["--root", str(self.root), "--payload-file", str(FIXTURES / "payload_basic.json")])
        self.assertEqual(code, 1)
        self.assertIn("public", err.getvalue())
        self.assertNotIn("4100", err.getvalue())  # the payload is never echoed
        self.assertFalse(list((self.root / "reports").rglob("*.json")) if (self.root / "reports").exists() else [])


class MergeTest(unittest.TestCase):
    def test_streaks_and_sums_chain_across_parts(self):
        a = {"n": 3, "wins": 1, "losses": 2, "be": 0, "net_r": -1.0, "gross_win_r": 1.0, "gross_loss_r": 2.0,
             "net_pct": -0.5, "max_ls": 2, "cur_ls": 2, "lead_ls": 0, "dd_pct": 1.0,
             "by_method": {"M001": {"n": 3, "wins": 1, "net_r": -1.0}}, "rule": {"no_sl": 1},
             "grid": {"Mon": {"08": {"n": 3, "wins": 1}}}}
        b = {"n": 2, "wins": 0, "losses": 2, "be": 0, "net_r": -2.0, "gross_win_r": 0.0, "gross_loss_r": 2.0,
             "net_pct": -1.0, "max_ls": 2, "cur_ls": 2, "lead_ls": 2, "dd_pct": 1.0,
             "by_method": {"M001": {"n": 2, "wins": 0, "net_r": -2.0}}, "rule": {"no_sl": 2},
             "grid": {"Mon": {"08": {"n": 2}}}}
        m = agg.merge_aggs([a, b])
        self.assertEqual((m["n"], m["wins"], m["losses"]), (5, 1, 4))
        self.assertEqual((m["max_ls"], m["cur_ls"]), (4, 4))
        self.assertAlmostEqual(m["net_r"], -3.0)
        self.assertAlmostEqual(m["pf"], 0.25)
        self.assertAlmostEqual(m["dd_pct"], 1.5)  # path of end-of-part P/L: 0 -> -0.5 -> -1.5
        self.assertEqual(m["rule"], {"no_sl": 3})
        self.assertEqual(m["by_method"]["M001"]["n"], 5)
        self.assertEqual(m["grid"]["Mon"]["08"], {"n": 5})  # one part hid its wins -> merged win% unknown
        c = {"n": 1, "wins": 1, "losses": 0, "be": 0, "net_r": 2.0, "gross_win_r": 2.0, "gross_loss_r": 0.0,
             "net_pct": 1.0, "max_ls": 0, "cur_ls": 0, "lead_ls": 0, "dd_pct": 0.0}
        m2 = agg.merge_aggs([a, b, c])
        self.assertEqual((m2["max_ls"], m2["cur_ls"]), (4, 0))
        all_losses = agg.merge_aggs([b, b])
        self.assertEqual(all_losses["lead_ls"], 4)


class AggPagesTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        store_report.store(self.root, fixture_json("payload_agg.json"), NOW)
        later = fixture_json("payload_agg.json")
        later["ts"] += 86400
        later["data"]["day"] = "2026-11-26"
        later["data"]["periods"]["day"] = {"n": 1, "wins": 1, "losses": 0, "be": 0, "net_r": 2.0, "net_pct": 1.0,
                                           "gross_win_r": 2.0, "gross_loss_r": 0.0, "max_ls": 0, "cur_ls": 0,
                                           "lead_ls": 0, "dd_pct": 0.0}
        store_report.store(self.root, later, NOW + dt.timedelta(days=1))

    def test_daily_weekly_monthly_and_dashboard(self):
        written = daily_summary.summarize(self.root, [dt.date(2026, 11, 25), dt.date(2026, 11, 26)])
        names = {p.relative_to(self.root).as_posix() for p in written}
        for name in ("reports/daily/2026-11-25.md", "reports/daily/2026-11-26.md", "reports/weekly/2026-W48.md",
                     "reports/monthly/2026-11.md", "docs/dashboard/data.json"):
            self.assertIn(name, names)
        day = (self.root / "reports/daily/2026-11-25.md").read_text()
        self.assertIn("| 4 | 0 / 4 / 0 | 0.0 |", day)
        self.assertIn("**no_sl** 1", day)
        self.assertIn("| M001 | 2 | 0.0 | -2.00 |", day)
        self.assertIn("spread_wide", day)
        self.assertIn("-0.80 R", day)
        week = (self.root / "reports/weekly/2026-W48.md").read_text()
        self.assertIn("rolling 7 days ending 2026-11-26", week)
        month = (self.root / "reports/monthly/2026-11.md").read_text()
        self.assertIn("| Mon |  |  | 5 / 60 | 2 |  |  |", month)
        dash = json.loads((self.root / "docs/dashboard/data.json").read_text())
        a = dash["aggregates"][ACCT]
        self.assertEqual(sorted(a["daily"]), ["2026-11-25", "2026-11-26"])
        self.assertEqual(a["mtd"]["n"], 30)
        self.assertEqual(a["all"]["n"], 30)
        self.assertEqual(dash["accounts"], {})
        self.assertEqual(daily_summary.summarize(self.root, [dt.date(2026, 11, 25), dt.date(2026, 11, 26)]), [])


class AggAlertsTest(unittest.TestCase):
    def test_alerts_from_aggregates(self):
        root = make_root()
        now = parse_utc("2026-11-25T16:00:00Z")
        build_latest.build(root, now, session=FakeDataSession())
        store_report.store(root, fixture_json("payload_agg.json"), now)
        keys = {a["key"] for a in alerts.evaluate(root, load_config(root), now)}
        for k in (f"dayloss:{ACCT}:2026-11-25", f"streak:{ACCT}:2026-11-25:4", f"dd:{ACCT}:2026-11-25",
                  f"rule:{ACCT}:2026-11-25:no_sl:1", f"rule:{ACCT}:2026-11-25:news:1", f"gold:{ACCT}:2026-11-25",
                  f"eaflag:{ACCT}:spread_wide:2026-11-25"):
            self.assertIn(k, keys)

    def test_old_aggregate_does_not_alert(self):
        root = make_root()
        now = parse_utc("2026-11-26T09:00:00Z")   # 18 h after the report > report_max_age_h
        build_latest.build(root, now, session=FakeDataSession())
        payload = copy.deepcopy(fixture_json("payload_agg.json"))
        store_report.store(root, payload, now)
        keys = {a["key"] for a in alerts.evaluate(root, load_config(root), now)}
        self.assertFalse(any(k.startswith(("dayloss", "streak", "dd:")) for k in keys))


if __name__ == "__main__":
    unittest.main()
