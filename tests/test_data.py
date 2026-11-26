import datetime as dt
import json
import unittest

from helpers import FakeDataSession, fixture_text, make_root, synthetic_fred

import build_latest
import calendar_events
import fred
import gold
import quality
from zumi_common import parse_utc

NOW = parse_utc("2026-11-26T11:17:00Z")


class FredParseTest(unittest.TestCase):
    def test_skips_blank_and_dot(self):
        rows = fred.parse_fred_csv(fixture_text("fred_DGS10.csv"), "DGS10")
        dates = [d for d, _ in rows]
        self.assertNotIn("2026-11-23", dates)
        self.assertNotIn("2026-11-25", dates)
        self.assertEqual(rows[-1], ("2026-11-26", 4.88))

    def test_old_header_format(self):
        rows = fred.parse_fred_csv(fixture_text("fred_old_format.csv"), "VIXCLS")
        self.assertEqual(rows, [("2026-11-19", 20.10), ("2026-11-20", 35.89)])

    def test_summary_is_causal_d_minus_1(self):
        rows = fred.parse_fred_csv(fixture_text("fred_DGS10.csv"), "DGS10")
        s = fred.summarize(rows, dt.date(2026, 11, 26))
        self.assertEqual(s["d"], "2026-11-24")  # 11-26 value is NOT used on 11-26
        self.assertEqual(s["v"], 4.91)
        self.assertEqual(s["p"], 4.92)
        self.assertAlmostEqual(s["c1"], -0.01)
        self.assertAlmostEqual(s["c5"], round(4.91 - 5.12, 4))  # 5 observations back = 11-16
        self.assertEqual(s["age"], 2)
        self.assertIsNone(fred.summarize(rows, dt.date(2026, 11, 13)))

    def test_merge_new_wins(self):
        merged = fred.merge_rows([("2026-01-01", 1.0), ("2026-01-02", 2.0)], [("2026-01-02", 2.5), ("2026-01-03", 3.0)])
        self.assertEqual(merged, [("2026-01-01", 1.0), ("2026-01-02", 2.5), ("2026-01-03", 3.0)])


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.events = calendar_events.load_events(self.root / "data/calendar/events_us.csv")

    def test_dst_conversion(self):
        by_date = {e["date"]: e for e in self.events}
        self.assertEqual(by_date["2026-03-06"]["t_utc"].hour, 13)  # EST: 08:30 -> 13:30 UTC
        self.assertEqual(by_date["2026-03-18"]["t_utc"].hour, 18)  # EDT: 14:00 -> 18:00 UTC
        self.assertEqual(by_date["2026-11-27"]["unc"], 1)

    def test_block_next_and_recent(self):
        block = calendar_events.calendar_block(self.events, NOW, next_n=6, recent_h=24)
        self.assertEqual([e["ev"] for e in block["next"]], ["NFP", "FOMC"])
        self.assertEqual(block["next"][0]["utc"], "2026-11-27T13:30:00Z")
        self.assertEqual([e["ev"] for e in block["recent"]], ["CPI"])
        self.assertEqual(block["covered_until"], "2026-12-09")

    def test_real_calendar_file_parses(self):
        from helpers import REPO
        events = calendar_events.load_events(REPO / "data/calendar/events_us.csv")
        self.assertGreater(len(events), 250)
        self.assertEqual({e["ev"] for e in events}, {"NFP", "CPI", "FOMC"})


class GoldTest(unittest.TestCase):
    def test_summary_uses_prior_atr(self):
        bars = gold.parse_ohlc_csv(fixture_text("stooq_xauusd.csv"))
        s = gold.summarize(bars, dt.date(2026, 11, 26))
        self.assertEqual(s["d"], "2026-11-25")
        self.assertEqual(s["c1"], -30.0)
        expected_atr = gold.atr([b for b in bars if b[0] < "2026-11-25"])
        self.assertAlmostEqual(s["atr14"], round(expected_atr, 2))
        s24 = gold.summarize(bars, dt.date(2026, 11, 25))
        self.assertGreater(s24["move_atr"], 2.0)  # the +120 day


class BuildLatestTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def latest(self):
        return json.loads((self.root / "data/latest.json").read_text())

    def test_build_writes_schema_and_d_minus_1(self):
        res = build_latest.build(self.root, NOW, session=FakeDataSession())
        self.assertTrue(res["ok"], res["issues"])
        self.assertTrue(res["written"])
        lat = self.latest()
        self.assertEqual(lat["schema"], "zumi.latest")
        self.assertEqual(lat["day"], "2026-11-26")
        self.assertEqual(len(lat["series"]), 8)
        for s in lat["series"].values():
            self.assertLess(s["d"], lat["day"])
        self.assertEqual(quality.check_schema(lat), [])
        self.assertNotIn("gold", lat)  # stooq unreachable -> optional block omitted
        self.assertTrue((self.root / "data/history/DGS10.csv").exists())
        text = (self.root / "data/latest.json").read_text()
        self.assertTrue(text.startswith("{\n  \"asof\""))  # pretty + sorted keys

    def test_unchanged_run_does_not_rewrite_but_new_day_does(self):
        build_latest.build(self.root, NOW, session=FakeDataSession())
        res2 = build_latest.build(self.root, NOW + dt.timedelta(hours=1), session=FakeDataSession())
        self.assertFalse(res2["written"])
        self.assertEqual(res2["message"], "data: no change")
        res3 = build_latest.build(self.root, NOW + dt.timedelta(days=1), session=FakeDataSession())
        self.assertTrue(res3["written"])

    def test_refetch_trailing_window_once_history_exists(self):
        build_latest.build(self.root, NOW, session=FakeDataSession())
        sess = FakeDataSession()
        build_latest.build(self.root, NOW, session=sess)
        cosd = {p["id"]: p["cosd"] for u, p in sess.calls if "id" in p}
        self.assertEqual(cosd["DGS10"], (NOW.date() - dt.timedelta(days=60)).isoformat())

    def test_spike_guard_keeps_previous_latest(self):
        build_latest.build(self.root, NOW, session=FakeDataSession())
        before = (self.root / "data/latest.json").read_text()
        hist_before = (self.root / "data/history/VIXCLS.csv").read_text()
        bad = synthetic_fred("VIXCLS", 18.0, last_date="2026-11-26").rstrip("\n").split("\n")
        bad[-1] = bad[-1].split(",")[0] + ",9999"
        sess = FakeDataSession(overrides={"VIXCLS": "\n".join(bad) + "\n"})
        res = build_latest.build(self.root, NOW + dt.timedelta(days=1), session=sess)
        self.assertFalse(res["ok"])
        self.assertFalse(res["written"])
        self.assertEqual((self.root / "data/latest.json").read_text(), before)
        self.assertEqual((self.root / "data/history/VIXCLS.csv").read_text(), hist_before)
        q = json.loads((self.root / "data/quality.json").read_text())
        self.assertFalse(q["ok"])
        self.assertIn("range", {i["kind"] for i in q["issues"]})

    def test_fetch_failure_falls_back_to_history(self):
        build_latest.build(self.root, NOW, session=FakeDataSession())
        res = build_latest.build(self.root, NOW, session=FakeDataSession(fail={"DGS2"}))
        self.assertTrue(res["ok"])
        self.assertIn("DGS2", self.latest()["series"])
        self.assertTrue(any(i["kind"] == "fetch" and i["sid"] == "DGS2" for i in res["issues"]))
        self.assertEqual(res["message"], "data: quality issues changed: DGS2:fetch")

    def test_gold_block_when_available(self):
        path = self.root / "config/settings.json"
        cfg = json.loads(path.read_text())
        cfg["gold"]["enabled"] = True  # disabled by default (stooq blocks GitHub runners)
        path.write_text(json.dumps(cfg))
        res = build_latest.build(self.root, NOW, session=FakeDataSession(gold_text=fixture_text("stooq_xauusd.csv")))
        self.assertTrue(res["ok"], res["issues"])
        self.assertEqual(self.latest()["gold"]["d"], "2026-11-25")

    def test_change_summary_lists_moves(self):
        old = {"series": {"DGS10": {"d": "2026-11-23", "v": 4.92}}, "events": {}, "day": "2026-11-25"}
        new = {"series": {"DGS10": {"d": "2026-11-24", "v": 4.91}}, "events": {}, "day": "2026-11-26"}
        self.assertEqual(build_latest.change_summary(old, new), "data: DGS10 4.92->4.91 (2026-11-24)")


class QualityTest(unittest.TestCase):
    def test_stale_and_spike(self):
        q = {"max_age_days": {"default": 7}, "range": {"X": [0, 10]}, "max_abs_c1": {"X": 1.0}}
        issues = quality.check_series("X", {"d": "2026-11-01", "v": 5, "c1": 2.0, "age": 25}, q)
        kinds = {(i["kind"], i["level"]) for i in issues}
        self.assertIn(("stale", "warn"), kinds)
        self.assertIn(("spike", "error"), kinds)

    def test_schema_detects_missing(self):
        issues = quality.check_schema({"schema": "zumi.latest"})
        self.assertTrue(quality.has_errors(issues))


if __name__ == "__main__":
    unittest.main()
