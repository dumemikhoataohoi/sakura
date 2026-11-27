"""Bridge 2.7 payload on the server side: by_side / by_session aggregates and shadow "SESS:REASON" cells pass the
whitelist (anything else is dropped without logging raw keys), merge exactly, reach coordinator rule R09 through the
stored daily blocks, and render on the summary pages. Offline and deterministic."""
import datetime as dt
import json
import shutil
import unittest

from helpers import make_root

import agg
import coordinator_report as cr
import daily_summary
import reports_lib as rl
from zumi_common import load_config, parse_utc

NOW = parse_utc("2026-11-27T15:00:00Z")


def grp(n, w, r=0.0):
    """One group exactly as ZGB_GroupJson writes it."""
    return {"n": n, "wins": w, "win_pct": round(100.0 * w / n, 1) if n else None, "net_r": r, "cur_ls": 0,
            "l20_n": min(n, 20), "l20_wins": min(w, 20)}


def block(day, buy=(0, 0), sell=(0, 0), sessions=None):
    sides = {k: grp(n, w, w - (n - w)) for k, (n, w) in (("buy", buy), ("sell", sell)) if n}
    n, wins = buy[0] + sell[0], buy[1] + sell[1]
    return {"from": day, "to": day, "n": n, "wins": wins, "losses": n - wins, "be": 0, "net_r": float(2 * wins - n),
            "net_pct": 0.1 * (2 * wins - n), "max_ls": 0, "cur_ls": 0, "lead_ls": 0, "dd_pct": 0.2, "rule": {},
            "by_method": {"M001": grp(n, wins)}, "by_phase": {}, "by_method_phase": {}, "by_side": sides,
            "by_session": sessions if sessions is not None else {"LON": grp(n, wins)}}


def payload(day, ts, blk, shadow=None):
    data = {"ea": "Zumi", "ver": "2.7", "reason": "periodic", "day": day, "phase": "SDPBN",
            "periods": {"day": blk, "week": blk, "mtd": blk, "r30": blk}, "flags": []}
    if shadow:
        data["shadow"] = shadow
    return {"v": 2, "kind": "ea_agg", "acct": "0123456789abcdef", "rid": f"x-{ts}", "part": 1, "parts": 1, "ts": ts, "data": data}


class SessionTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(agg.AGG_SESSIONS, ("ASIA", "LON", "NY", "OFF"))
        got = [agg.session_of(h) for h in range(24)]
        self.assertEqual(got, ["ASIA"] * 8 + ["LON"] * 4 + ["NY"] * 8 + ["OFF"] * 4)
        # privacy: every band starts on a 4-hour grid bucket, so session x grid never isolates a single entry hour
        for first, _ in agg.SESSION_BANDS_UTC:
            self.assertIn(f"{first:02d}", agg.BUCKETS)

    def test_by_session_whitelist_and_merge(self):
        stripped = set()
        blk = agg.sanitize_agg_block({"n": 6, "by_session": {"lon": grp(2, 1), "NY": grp(3, 2), "OFF": grp(1, 0),
                                                              "EUROPE": grp(1, 1), "M003": grp(1, 1)}},
                                     "data.periods.day", stripped)
        self.assertEqual(sorted(blk["by_session"]), ["LON", "NY", "OFF"])
        self.assertIn("data.periods.day.by_session.<invalid>", stripped)
        self.assertFalse(any("EUROPE" in s or "M003" in s for s in stripped))   # raw keys never logged
        merged = agg.merge_aggs([dict(blk, wins=3, losses=3), dict(blk, wins=3, losses=3)])
        self.assertEqual(merged["by_session"]["NY"], {"n": 6, "wins": 4, "net_r": 0.0, "win_pct": 66.7})
        self.assertIn("by_session", agg.empty_agg())

    def test_bridge_payload_passes_clean(self):
        cfg = load_config(make_root())
        rep, stripped = agg.sanitize_v2(payload("2026-11-27", int(NOW.timestamp()), block("2026-11-27", (3, 2), (2, 0))),
                                        cfg["reports"], NOW, "0123456789abcdef")
        self.assertEqual(stripped, [])
        day = rep["data"]["periods"]["day"]
        self.assertEqual(sorted(day["by_side"]), ["buy", "sell"])
        self.assertEqual(day["by_side"]["sell"]["wins"], 0)
        self.assertEqual(list(day["by_session"]), ["LON"])

    def test_shadow_by_session_cells(self):
        stripped = set()
        sh = agg.sanitize_shadow({"v": 1, "periods": {"day": {"CELL": {"n": 2, "wins": 1, "losses": 1, "to": 0, "net_r": 0.0}}},
                                  "by_session": {"ASIA:CELL": {"n": 4, "wins": 2, "net_r": 0.5},
                                                 "NY:SIDE": {"n": 2, "wins": 0, "net_r": -2.0},
                                                 "LONDON:CELL": {"n": 1, "wins": 1, "net_r": 1.0},
                                                 "NY:BOGUS": {"n": 1, "wins": 1, "net_r": 1.0},
                                                 "OFF:CAP": {"n": 1, "wins": 2, "net_r": 1.0}}}, stripped)
        self.assertEqual(sorted(sh["by_session"]), ["ASIA:CELL", "NY:SIDE"])
        self.assertIn("data.shadow.by_session.<invalid>", stripped)
        self.assertEqual(agg.SHADOW_SESSION_MAX, len(agg.AGG_SESSIONS) * len(agg.SHADOW_REASONS))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.cfg = load_config(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def store(self, day, ts, blk):
        rep, _ = agg.sanitize_v2(payload(day, ts, blk), self.cfg["reports"], NOW, "0123456789abcdef")
        rl.store_raw(self.root, rep)
        return rep

    def test_by_side_reaches_r09_through_stored_day_blocks(self):
        t = int(NOW.timestamp())
        for i in range(3):   # 3 days x 12 sells with 2 wins: n 36, Wilson 90 % upper bound ~ 0.28 < 0.45
            day = (NOW.date() - dt.timedelta(days=i)).isoformat()
            self.store(day, t - i * 86400, block(day, buy=(10, 6), sell=(12, 2)))
        hist = cr.build_history(self.root, NOW, 14)["0123456789abcdef"]
        self.assertEqual(hist["by_side"]["sell"], {"n": 36, "wins": 6, "net_r": -24.0})
        res = cr.evaluate(self.root, NOW, self.cfg)
        self.assertEqual(res["proposal"]["global"].get("side"), "auto")
        self.assertTrue(any(h["rule"] == "R09" and h["phase"] == "sell" for h in res["hits"]))

    def test_summary_pages_render_side_and_session(self):
        rep = self.store("2026-11-27", int(NOW.timestamp()), block("2026-11-27", (3, 2), (2, 0),
                                                                    {"ASIA": grp(1, 1), "NY": grp(4, 1)}))
        text = "\n".join(daily_summary.agg_section("0123456789abcdef", "day", rep["data"]["periods"]["day"], rep))
        self.assertIn("#### By side", text)
        self.assertIn("| sell | 2 |", text)
        self.assertIn("#### By session (UTC entry", text)
        self.assertIn("| NY | 4 |", text)
        old = dict(rep["data"]["periods"]["day"])
        old.pop("by_side")
        old.pop("by_session")
        text = "\n".join(daily_summary.agg_section("0123456789abcdef", "day", old, rep))
        self.assertNotIn("By side", text)                    # pages of bridge <= 2.6 reports are unchanged
        self.assertNotIn("By session", text)
        self.assertEqual(json.loads(json.dumps(rep))["data"]["ver"], "2.7")


if __name__ == "__main__":
    unittest.main()
