import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ff_calendar  # noqa: E402

NOW = dt.datetime(2026, 11, 27, 8, 0, tzinfo=dt.timezone.utc)
FEED = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-12-04T08:30:00-05:00",
     "impact": "High", "forecast": "150K", "previous": "120K"},
    {"title": "ISM Manufacturing PMI", "country": "USD", "date": "2026-12-01T10:00:00-05:00",
     "impact": "High", "forecast": "", "previous": "48.7"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-11-26T00:00:00-05:00", "impact": "Holiday"},
    {"title": "German Ifo", "country": "EUR", "date": "2026-11-30T04:00:00-05:00", "impact": "High"},
    {"title": "bad date", "country": "USD", "date": "x", "impact": "High"},
    "junk",
]


class FFCalendarTest(unittest.TestCase):
    def test_parse_filters_and_converts_to_utc(self):
        obj = ff_calendar.parse(FEED, NOW)
        self.assertEqual([e["title"] for e in obj["events"]], ["ISM Manufacturing PMI", "Non-Farm Employment Change"])
        self.assertEqual(obj["events"][1]["utc"], "2026-12-04T13:30:00Z")

    def test_write_only_on_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ff.json"
            obj = ff_calendar.parse(FEED, NOW)
            self.assertTrue(ff_calendar.write_if_changed(p, obj))
            later = ff_calendar.parse(FEED, NOW + dt.timedelta(days=1))   # only fetched_day differs
            self.assertFalse(ff_calendar.write_if_changed(p, later))
            self.assertTrue(json.loads(p.read_text())["events"])


if __name__ == "__main__":
    unittest.main()


class GoldContextTest(unittest.TestCase):
    def test_candle_codes_and_context(self):
        import gold
        self.assertEqual(gold.candle_code(("d", 10, 12, 9, 11), ("d", 11, 13, 10.6, 11.1)), "SWEEP_HI")
        self.assertEqual(gold.candle_code(("d", 11, 12, 10, 10.2), ("d", 10.1, 12.5, 9.9, 12.4)), "OUTSIDE")
        bars, p = [], 100.0
        start = dt.date(2026, 6, 1)
        for i in range(150):
            o, c = p, p + 0.5
            bars.append(((start + dt.timedelta(days=i)).isoformat(), o, c + 0.3, o - 0.3, c))
            p = c
        ctx = gold.context(bars, start + dt.timedelta(days=150))
        self.assertEqual(ctx["d1"]["dir"], 1)
        self.assertGreater(ctx["d1"]["rsi"], 70)
        self.assertIn("w1", ctx)
