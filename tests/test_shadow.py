"""Blocked-signal shadow statistics (bridge 2.5 / EA161 v1.32 data.shadow): whitelist and rendering. Public-mode repo."""
import copy
import datetime as dt
import json
import unittest

from helpers import fixture_json, make_root

import agg
import daily_summary
import store_report
from zumi_common import parse_utc

NOW = parse_utc("2026-11-25T16:00:00Z")

SHADOW = {
    "v": 1, "open": 7,
    "periods": {
        "day": {"CELL": {"n": 6, "wins": 2, "losses": 4, "to": 1, "net_r": -2.0},
                "CTRL": {"n": 4, "wins": 3, "losses": 1, "to": 0, "net_r": 2.0}},
        "week": {"CELL": {"n": 30, "wins": 12, "losses": 17, "to": 2, "net_r": -5.4},
                 "CTRL": {"n": 9, "wins": 6, "losses": 3, "to": 0, "net_r": 3.0},
                 "NEWS": {"n": 2, "wins": 0, "losses": 2, "to": 0, "net_r": -2.0}},
        "mtd": {"CELL": {"n": 80, "wins": 33, "losses": 46, "to": 4, "net_r": -13.25},
                "CAP": {"n": 5, "wins": 4, "losses": 1, "to": 0, "net_r": 3.0}},
        "r30": {"CELL": {"n": 90, "wins": 37, "losses": 52, "to": 4, "net_r": -15.0}},
    },
    "by_method": {"M701:CTRL": {"n": 9, "wins": 6, "net_r": 3.0}, "M132:CELL": {"n": 40, "wins": 15, "net_r": -9.5}},
    "by_phase": {"SDPBN:CELL": {"n": 50, "wins": 20, "net_r": -9.0}, "none:CAP": {"n": 2, "wins": 1, "net_r": 0.0}},
}


def payload(shadow=None, ts_shift=0):
    p = fixture_json("payload_agg.json")
    p["ts"] += ts_shift
    if shadow is not None:
        p["data"]["shadow"] = copy.deepcopy(shadow)
    return p


class ShadowValidatorTest(unittest.TestCase):
    def test_valid_shadow_passes_unchanged(self):
        stripped = set()
        self.assertEqual(agg.sanitize_shadow(copy.deepcopy(SHADOW), stripped), SHADOW)
        self.assertEqual(stripped, set())
        root = make_root()
        res = store_report.store(root, payload(SHADOW), NOW)
        self.assertEqual(res["stripped"], [])
        self.assertEqual(json.loads(res["path"].read_text())["data"]["shadow"], SHADOW)

    def test_vocabulary_codes_and_ranges(self):
        bad = copy.deepcopy(SHADOW)
        bad["price"] = 4123.5                                               # unknown top-level key
        bad["periods"]["day"]["CLUSTER"] = {"n": 1, "wins": 1, "losses": 0, "to": 0, "net_r": 1.0}   # removed in v1.32
        bad["periods"]["day"]["CTRL"]["sl"] = 4100.25                       # unknown key in a row
        bad["periods"]["week"]["CELL"]["net_r"] = 4100.0                    # |R| > 2.5 x n: a price, not R
        bad["periods"]["week"]["NEWS"]["wins"] = 3                          # wins > n
        bad["periods"]["mtd"]["CAP"]["losses"] = 2                          # wins + losses > n
        bad["periods"]["h1"] = {}                                           # unknown period
        bad["by_method"]["London breakout:CTRL"] = {"n": 3, "wins": 1, "net_r": 0.0}   # method name instead of a code
        bad["by_method"]["M7012:CTRL"] = {"n": 3, "wins": 1, "net_r": 0.0}
        bad["by_method"]["M701:PAUSE"] = {"n": 3, "wins": 1, "net_r": 0.0}  # reason not in the vocabulary
        bad["by_phase"]["SD|PB|N:CELL"] = {"n": 3, "wins": 1, "net_r": 0.0}
        bad["by_phase"]["SDPBN:CELL"]["t"] = 1795600000                     # a time inside a cell
        bad["open"] = -1
        stripped = set()
        out = agg.sanitize_shadow(bad, stripped)
        for key in ("data.shadow.price", "data.shadow.periods.day.CLUSTER", "data.shadow.periods.day.CTRL.sl",
                    "data.shadow.periods.week.CELL.net_r", "data.shadow.periods.week.NEWS", "data.shadow.periods.mtd.CAP",
                    "data.shadow.periods.h1", "data.shadow.by_method.<invalid>", "data.shadow.by_phase.<invalid>",
                    "data.shadow.by_phase.<cell>.t", "data.shadow.open"):
            self.assertIn(key, stripped)
        self.assertEqual(set(out["periods"]["day"]), {"CELL", "CTRL"})
        self.assertEqual(out["periods"]["day"]["CTRL"], SHADOW["periods"]["day"]["CTRL"])
        self.assertEqual(set(out["periods"]["week"]), {"CTRL"})
        self.assertEqual(set(out["by_method"]), {"M701:CTRL", "M132:CELL"})
        self.assertEqual(out["by_phase"]["SDPBN:CELL"], SHADOW["by_phase"]["SDPBN:CELL"])
        self.assertNotIn("open", out)
        self.assertFalse(any("London" in k for k in stripped))             # stripped values / names never reach the log
        root = make_root()
        text = store_report.store(root, payload(bad), NOW)["path"].read_text()
        for leaked in ("4100", "4123", "London", "1795600000", "CLUSTER", "PAUSE", "SD|PB"):
            self.assertNotIn(leaked, text)

    def test_list_caps(self):
        many = copy.deepcopy(SHADOW)
        many["by_method"] = {f"M{i:03d}:CELL": {"n": i, "wins": 0, "net_r": -float(i)} for i in range(1, 51)}
        many["by_phase"] = {f"P{chr(65 + i // 26)}{chr(65 + i % 26)}:CAP": {"n": 1, "wins": 1, "net_r": 1.0} for i in range(60)}
        stripped = set()
        out = agg.sanitize_shadow(many, stripped)
        self.assertEqual(len(out["by_method"]), agg.SHADOW_METHOD_MAX)
        self.assertEqual(min(c["n"] for c in out["by_method"].values()), 21)   # the top 30 by n are kept
        self.assertEqual(len(out["by_phase"]), agg.SHADOW_PHASE_MAX)
        self.assertIn("data.shadow.by_method.<truncated>", stripped)
        self.assertIn("data.shadow.by_phase.<truncated>", stripped)

    def test_bad_types(self):
        stripped = set()
        self.assertIsNone(agg.sanitize_shadow("CELL 4100", stripped))
        self.assertIn("data.shadow", stripped)
        self.assertIsNone(agg.sanitize_shadow({"v": 1, "by_method": {"M001:CELL": {"n": 1, "wins": 1, "net_r": 1.0}}}, set()))
        out = agg.sanitize_shadow({"periods": {"day": {"CELL": {"n": True, "wins": 0, "net_r": 0}}}}, set())
        self.assertEqual(out, {"periods": {"day": {}}})
        root = make_root()
        res = store_report.store(root, payload([1, 2]), NOW)
        self.assertNotIn("shadow", res["report"]["data"])
        self.assertIn("data.shadow", res["stripped"])


class ShadowRenderTest(unittest.TestCase):
    def test_daily_weekly_monthly_sections(self):
        root = make_root()
        store_report.store(root, payload(SHADOW), NOW)
        daily_summary.summarize(root, [dt.date(2026, 11, 25)])
        day = (root / "reports/daily/2026-11-25.md").read_text()
        self.assertIn("#### Blocked signals (shadow), today", day)
        # real trades of the fixture day: 0 W / 4 L -> real win% 0.0
        self.assertIn("| CELL | 6 | 2 / 4 | 1 | 33.3 | -2.00 | -0.333 | +33.3 |", day)
        self.assertIn("| CTRL | 4 | 3 / 1 | 0 | 75.0 | +2.00 | +0.500 | +75.0 |", day)
        self.assertIn("| all blocked | 10 | 5 / 5 | 1 | 50.0 | +0.00 | +0.000 | +50.0 |", day)
        self.assertIn("| real trades | 4 | 0 / 4 | - | 0.0 | -3.00 | -0.750 | - |", day)
        self.assertIn("Virtual trades still open: 7.", day)
        self.assertIn("| M132:CELL | 40 | 37.5 | -9.50 |", day)
        self.assertIn("| SDPBN:CELL | 50 | 40.0 | -9.00 |", day)
        self.assertLess(day.index("| M132:CELL"), day.index("| M701:CTRL"))   # sorted by n
        week = (root / "reports/weekly/2026-W48.md").read_text()
        self.assertIn("#### Blocked signals (shadow), rolling 7 days", week)
        self.assertIn("| NEWS | 2 | 0 / 2 | 0 | 0.0 | -2.00 | -1.000 |", week)
        month = (root / "reports/monthly/2026-11.md").read_text()
        self.assertIn("#### Blocked signals (shadow), month to date", month)
        self.assertIn("| CAP | 5 | 4 / 1 | 0 | 80.0 | +3.00 | +0.600 |", month)

    def test_no_shadow_no_section_and_empty_period(self):
        root = make_root()
        store_report.store(root, payload(), NOW)
        daily_summary.summarize(root, [dt.date(2026, 11, 25)])
        self.assertNotIn("Blocked signals", (root / "reports/daily/2026-11-25.md").read_text())
        root = make_root()
        empty = copy.deepcopy(SHADOW)
        empty["periods"]["day"] = {}
        store_report.store(root, payload(empty), NOW)
        daily_summary.summarize(root, [dt.date(2026, 11, 25)])
        self.assertIn("No blocked signal resolved in this period (7 virtual trades still open).",
                      (root / "reports/daily/2026-11-25.md").read_text())


if __name__ == "__main__":
    unittest.main()
