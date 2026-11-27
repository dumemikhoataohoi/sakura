"""Coordinator v2 rules (Wilson health switch, risk allocation, drawdown ladder, open-risk cap, side R09),
the by_side whitelist and the offline replay evaluator. Offline and deterministic."""
import contextlib
import copy
import csv
import io
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_root

import agg
import control
import coord_replay
import coordinator_report as cr
from zumi_common import iso_z, load_config, parse_utc

NOW = parse_utc("2026-11-25T16:00:00Z")


def g(n, w, r=0.0, cur=0):
    return {"n": n, "wins": w, "net_r": r, "cur_ls": cur, "l20_n": min(n, 20), "l20_wins": min(w, 20)}


def report(cells=None, by_method=None, week_dd=0.0, mtd_dd=0.0, cr_state=None, shadow=None):
    cells = cells or {}
    data = {"day": "2026-11-25", "periods": {
        "r30": {"n": sum(x["n"] for x in cells.values()), "wins": sum(x["wins"] for x in cells.values()),
                "by_method_phase": cells, "by_method": by_method or {}},
        "week": {"n": 0, "dd_pct": week_dd}, "mtd": {"n": 0, "dd_pct": mtd_dd}}, "flags": []}
    if cr_state:
        data["mkt"] = {"ctx": {"cr": cr_state}}
    if shadow:
        data["shadow"] = {"by_method": shadow, "periods": {}}
    return {"a": {"v": 2, "kind": "ea_agg", "acct": "a", "ts": int(NOW.timestamp()), "data": data}}


class V2Base(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.cfg = load_config(self.root)
        self.c = cr.coord_cfg(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_(self, reps, hist=None, current=None, **over):
        c = dict(self.c, **over)
        res = cr.decide(reps, hist, current, NOW, c)
        self.assertIsNotNone(res["proposal"], res["errors"])
        return res


class WilsonTest(V2Base):
    def test_wilson_bounds(self):
        self.assertEqual(cr.wilson(0, 0), (0.0, 1.0))
        lb, ub = cr.wilson(10, 20)
        self.assertLess(lb, 0.5)
        self.assertGreater(ub, 0.5)
        self.assertAlmostEqual(lb + ub, 1.0, places=9)                    # symmetric at p = 0.5
        self.assertLess(cr.wilson(6, 24)[1], 0.5)                          # 25 % over 24: clearly below 50 %
        self.assertGreater(cr.wilson(20, 24)[0], 0.5)
        self.assertAlmostEqual(cr.shrunk(0, 0, 0.55, 10), 0.55)
        self.assertAlmostEqual(cr.shrunk(10, 10, 0.5, 10), 0.75)

    def test_pause_needs_min_n_and_upper_bound(self):
        res = self.run_(report({"M010:SUPBH": g(24, 6, -12.0, 1), "M011:SUPBH": g(15, 2, -11.0, 1),
                                "M012:RPBN": g(30, 14, 0.0, 0), "M000:none": g(40, 0, -40.0, 1)}))
        m = res["proposal"]["methods"]
        self.assertEqual((m["M010"]["mode"], m["M010"]["phases"], m["M010"]["reason_code"]), ("pause", ["SUPBH"], "R02"))
        self.assertNotIn("M011", m)        # n < 20
        self.assertNotIn("M012", m)        # 47 % over 30: upper bound above 50 %
        self.assertNotIn("M000", m)        # never touched
        hit = [h for h in res["hits"] if h["rule"] == "R02"][0]
        self.assertEqual((hit["n"], hit["win_pct"]), (24, 25.0))
        self.assertLess(hit["ub_pct"], 50.0)

    def test_history_window_beats_r30_and_band_mode(self):
        hist = {"a": {"health": {"days": 90, "n": 60, "wins": 30, "by_method_phase": {
            "M020:SDPBL": {"n": 12, "wins": 3, "net_r": -6.0}, "M020:SDIMPN": {"n": 12, "wins": 2, "net_r": -8.0}}}}}
        res = self.run_(report({"M020:SDPBL": g(3, 1)}), hist)
        self.assertNotIn("M020", res["proposal"]["methods"])               # per full phase: n 12 < 20
        res = self.run_(report({"M020:SDPBL": g(3, 1)}), hist, health_cell="band")
        d = res["proposal"]["methods"]["M020"]
        self.assertEqual(d["mode"], "pause")
        self.assertEqual(len(d["phases"]), 9)
        self.assertIn("SDCTRH", d["phases"])
        res = self.run_(report({"M020:SDPBL": g(3, 1)}), hist, health_cell="code")
        self.assertEqual(res["proposal"]["methods"]["M020"]["phases"], [])   # whole code

    def test_restore_by_cell_or_by_blocked_signals(self):
        cur, _, _ = control.normalize({"v": 1, "author": "claude", "seq": 3, "updated_utc": iso_z(NOW - dt.timedelta(hours=8)),
                                       "global": {"risk_mult": 1.0, "pause_all": False}, "news_blocks": [], "notes_vi": "ok",
                                       "methods": {"M030": {"mode": "pause", "phases": ["RPBN"], "reason_code": "R02"},
                                                   "M031": {"mode": "pause", "phases": ["RPBL"], "reason_code": "R02"}}}, NOW)
        reps = report({"M030:RPBN": g(30, 25, 15.0, 1), "M031:RPBL": g(24, 6, -12.0, 1)},
                      shadow={"M031:CTRL": {"n": 25, "wins": 21, "net_r": 15.0}})
        res = self.run_(reps, None, cur)
        m = res["proposal"]["methods"]
        self.assertEqual((m["M030"]["mode"], m["M030"]["reason_code"]), ("on", "R08"))     # cell lower bound >= 50 %
        self.assertEqual((m["M031"]["mode"], m["M031"]["reason_code"]), ("on", "R08"))     # blocked signals recovered
        self.assertTrue(any(h["rule"] == "R08" and h["code"] == "M031" for h in res["hits"]))

    def test_last20_mode_is_v1(self):
        res = self.run_(report({"M040:RPBN": g(12, 3, -4.0, 1)}), r02_mode="last20")
        self.assertEqual(res["proposal"]["methods"]["M040"]["reason_code"], "R02")
        res = self.run_(report({"M040:RPBN": g(12, 3, -4.0, 1)}), r02_mode="off")
        self.assertNotIn("M040", res["proposal"]["methods"])


class AllocationTest(V2Base):
    def test_reduce_from_shrunk_expectancy(self):
        bm = {"M050": {"n": 20, "wins": 5, "net_r": -10.0}, "M051": {"n": 20, "wins": 15, "net_r": 8.0},
              "M052": {"n": 5, "wins": 0, "net_r": -5.0}, "M053": {"n": 40, "wins": 0, "net_r": -40.0}}
        res = self.run_(report(by_method=bm), alloc_enable=True)
        m = res["proposal"]["methods"]
        self.assertEqual((m["M050"]["mode"], m["M050"]["risk_mult"], m["M050"]["reason_code"]), ("reduce", 0.5, "R02"))
        self.assertNotIn("M051", m)                                        # positive expectancy: never above 1.0
        self.assertNotIn("M052", m)                                        # n < alloc_min_n
        self.assertEqual(m["M053"]["risk_mult"], 0.25)                     # floor
        for d in m.values():
            self.assertLessEqual(d["risk_mult"], 1.0)
        self.assertEqual(self.run_(report(by_method=bm))["proposal"]["methods"], {})   # off by default

    def test_pause_wins_and_kept_pause_not_lifted(self):
        cur, _, _ = control.normalize({"v": 1, "author": "claude", "seq": 3, "updated_utc": iso_z(NOW - dt.timedelta(hours=2)),
                                       "global": {"risk_mult": 1.0}, "news_blocks": [], "notes_vi": "ok",
                                       "methods": {"M061": {"mode": "pause", "reason_code": "R01"}}}, NOW)
        bm = {"M060": {"n": 24, "wins": 6, "net_r": -12.0}, "M061": {"n": 24, "wins": 6, "net_r": -12.0}}
        res = self.run_(report({"M060:RPBN": g(24, 6, -12.0, 1)}, by_method=bm), None, cur, alloc_enable=True)
        m = res["proposal"]["methods"]
        self.assertEqual(m["M060"]["mode"], "pause")
        self.assertEqual((m["M061"]["mode"], m["M061"]["reason_code"]), ("pause", "R01"))


class LadderSideTest(V2Base):
    def test_drawdown_ladder_and_open_risk(self):
        g0 = self.run_(report())["proposal"]["global"]
        self.assertEqual((g0["risk_mult"], g0["reason_code"]), (1.0, "R00"))
        self.assertNotIn("open_risk_pct", g0)
        g1 = self.run_(report(week_dd=3.4))["proposal"]["global"]
        self.assertEqual((g1["risk_mult"], g1["open_risk_pct"], g1["reason_code"], g1["pause_all"]), (0.75, 9.0, "R05", False))
        g2 = self.run_(report(mtd_dd=5.2))["proposal"]["global"]
        self.assertEqual((g2["risk_mult"], g2["open_risk_pct"]), (0.5, 6.0))
        g3 = self.run_(report(mtd_dd=5.2), open_risk_local_pct=4.0)["proposal"]["global"]
        self.assertEqual(g3["open_risk_pct"], 4.0)                         # never above the user's local value
        legacy = self.run_(report(week_dd=4.0), dd_ladder=None, dd_open_risk_ladder=[])["proposal"]["global"]
        self.assertEqual(legacy["risk_mult"], 1.0)                         # v1: single 5 % step
        self.assertEqual(self.run_(report(week_dd=6.0), dd_ladder=None)["proposal"]["global"]["risk_mult"], 0.5)

    def test_pause_all_only_with_ea_crisis_at_top_step(self):
        self.assertFalse(self.run_(report(mtd_dd=6.0))["proposal"]["global"]["pause_all"])
        self.assertFalse(self.run_(report(week_dd=3.5, cr_state="BLOCK:VOL"))["proposal"]["global"]["pause_all"])
        self.assertFalse(self.run_(report(mtd_dd=6.0, cr_state="OK"))["proposal"]["global"]["pause_all"])
        self.assertTrue(self.run_(report(mtd_dd=6.0, cr_state="BLOCK:VOL"))["proposal"]["global"]["pause_all"])
        self.assertFalse(self.run_(report(mtd_dd=6.0, cr_state="BLOCK:VOL"), dd_crisis_pause_all=False)
                         ["proposal"]["global"]["pause_all"])

    def test_side_auto_when_one_side_is_weak(self):
        hist = lambda n, w: {"a": {"side": {"by_side": {"sell": {"n": n, "wins": w, "net_r": 0.0},   # noqa: E731
                                                        "buy": {"n": 50, "wins": 30, "net_r": 10.0}}}}}
        gl = self.run_(report(), hist(40, 10))["proposal"]["global"]
        self.assertEqual((gl.get("side"), gl["reason_code"]), ("auto", "R09"))
        self.assertNotIn("side", self.run_(report(), hist(20, 4))["proposal"]["global"])    # n < 30
        self.assertNotIn("side", self.run_(report(), hist(40, 18))["proposal"]["global"])   # not clearly weak
        self.assertNotIn("side", self.run_(report(), hist(40, 10), side_enable=False)["proposal"]["global"])
        both = self.run_(report(week_dd=3.5), hist(40, 10))["proposal"]["global"]
        self.assertEqual((both["side"], both["reason_code"], both["risk_mult"]), ("auto", "R05", 0.75))

    def test_notes_and_codes_stay_public(self):
        res = self.run_(report({"M010:SUPBH": g(24, 6, -12.0, 1)}, week_dd=5.5), alloc_enable=True)
        p = res["proposal"]
        self.assertFalse(control.PRICE_RE.search(p["notes_vi"]))
        self.assertTrue(all(control.METHOD_RE.match(k) for k in p["methods"]))
        self.assertEqual(json.dumps(p, sort_keys=True), json.dumps(cr.decide(report({"M010:SUPBH": g(24, 6, -12.0, 1)}, week_dd=5.5),
                                                                             None, None, NOW, dict(self.c, alloc_enable=True))["proposal"], sort_keys=True))


class HistoryAndWhitelistTest(V2Base):
    def write(self, day, ts, block):
        folder = self.root / "reports" / day.replace("-", "/")
        folder.mkdir(parents=True, exist_ok=True)
        rep = {"v": 2, "kind": "ea_agg", "acct": "a", "ts": ts, "data": {"day": day, "periods": {"day": block}}}
        (folder / f"a_{ts}.json").write_text(json.dumps(rep))

    def test_build_history_sums_newest_daily_blocks(self):
        t = int(NOW.timestamp())
        self.write("2026-11-23", t - 2 * 86400 - 3600, {"n": 1, "wins": 0, "by_method_phase": {"M001:RPBN": {"n": 1, "wins": 0, "net_r": -1.0}}})
        self.write("2026-11-23", t - 2 * 86400, {"n": 3, "wins": 1, "net_r": -1.0, "by_method_phase": {"M001:RPBN": {"n": 3, "wins": 1, "net_r": -1.0}},
                                                 "by_side": {"buy": {"n": 3, "wins": 1, "net_r": -1.0}}})
        self.write("2026-11-24", t - 86400, {"n": 2, "wins": 2, "net_r": 2.0, "by_method_phase": {"M001:RPBN": {"n": 2, "wins": 2, "net_r": 2.0}},
                                             "by_side": {"sell": {"n": 2, "wins": 2, "net_r": 2.0}}})
        self.write("2026-08-01", t - 116 * 86400, {"n": 9, "wins": 0, "by_method_phase": {"M001:RPBN": {"n": 9, "wins": 0}}})
        h = cr.build_history(self.root, NOW, 90)["a"]
        self.assertEqual((h["days"], h["n"], h["wins"]), (2, 5, 3))           # newest report per day, window 90 d
        self.assertEqual(h["by_method_phase"]["M001:RPBN"], {"n": 5, "wins": 3, "net_r": 1.0})
        self.assertEqual(sorted(h["by_side"]), ["buy", "sell"])

    def test_by_side_whitelist(self):
        stripped = set()
        blk = agg.sanitize_agg_block({"n": 5, "by_side": {"BUY": {"n": 3, "wins": 2}, "sell": {"n": 2, "wins": 0},
                                                           "M001": {"n": 1, "wins": 1}}}, "data.periods.day", stripped)
        self.assertEqual(sorted(blk["by_side"]), ["buy", "sell"])
        self.assertIn("data.periods.day.by_side.<invalid>", stripped)
        merged = agg.merge_aggs([dict(blk, wins=2, losses=3), dict(blk, wins=2, losses=3)])
        self.assertEqual(merged["by_side"]["buy"]["n"], 6)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sakura_replay_"))
        self.root = make_root()
        self.c = cr.coord_cfg(load_config(self.root))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def stream(self, rows):
        path = self.tmp / "s.csv"
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["t0", "t1", "code", "family", "side", "phase", "r_STD", "size", "risk_mult"])
            w.writeheader()
            w.writerows(rows)
        return path

    def synthetic(self):
        """X001 in RPBN loses 80 % of the time, X002 wins 60 %; 4 trades/day for 300 days (deterministic)."""
        rows, t = [], int(parse_utc("2025-01-02T00:00:00Z").timestamp())
        for d in range(300):
            for k, (code, ph) in enumerate((("X001", "RPBN"), ("X002", "SUPBH"), ("X002", "SUPBH"), ("X001", "RPBN"))):
                t0 = t + d * 86400 + 3600 * (2 + 5 * k)
                lose = (d + k) % 5 != 0 if code == "X001" else (d + k) % 5 in (0, 1)
                rows.append({"t0": t0, "t1": t0 + 1800, "code": code, "family": "F", "side": "BUY" if k % 2 else "SELL",
                             "phase": ph, "r_STD": -1.0 if lose else 1.0, "size": 1.0, "risk_mult": 1.0})
        return rows

    def test_load_stream_maps_codes(self):
        trades, skipped = coord_replay.load_stream(self.stream(self.synthetic()[:4] + [
            {"t0": 1, "t1": 2, "code": "bad", "family": "", "side": "SELL", "phase": "x y", "r_STD": "", "size": "", "risk_mult": ""}]))
        self.assertEqual(skipped, 1)
        self.assertEqual({t["code"] for t in trades}, {"M001", "M002"})
        self.assertEqual(trades[0]["base"], 1.0)

    def test_metrics(self):
        rows = [{"t0": i, "t1": 86400 * (i // 2) + i, "r": r, "w": 1.0, "mo": "2025-01"} for i, r in enumerate([1, -1, -1, -1, 1, 1])]
        m = coord_replay.metrics(rows, 1)
        self.assertEqual((m["n"], m["win"], m["net"], m["dd"], m["maxls"], m["losers"], m["worst_day"]), (6, 50.0, 0.0, 3.0, 3, 3, -2.0))

    def test_replay_pauses_weak_cell_and_is_deterministic(self):
        trades, _ = coord_replay.load_stream(self.stream(self.synthetic()))
        out, summ = coord_replay.replay(trades, self.c, [], 1.0, 0.5)
        res = coord_replay.evaluate_periods(out, ["2025-01:2025-10"], 200, 3)["2025-01:2025-10"]
        self.assertGreater(res["after"]["win"], res["before"]["win"] + 10)
        self.assertGreater(res["blocked"].get("CTRL", 0), 0)
        win = [x for x in res["table"] if x["metric"] == "win"][0]
        self.assertLess(win["p"], 0.05)
        self.assertGreater(summ["hits"].get("R01 pause", 0) + summ["hits"].get("R02 pause", 0), 0)
        out_w, summ_w = coord_replay.replay(trades, dict(self.c, streak_pause_n=999), [], 1.0, 0.5)   # Wilson alone
        self.assertGreater(summ_w["hits"].get("R02 pause", 0), 0)
        res_w = coord_replay.evaluate_periods(out_w, ["2025-01:2025-10"], 0, 3)["2025-01:2025-10"]
        self.assertGreater(res_w["after"]["win"], res_w["before"]["win"] + 10)
        out2, _ = coord_replay.replay(copy.deepcopy(trades), self.c, [], 1.0, 0.5)
        res2 = coord_replay.evaluate_periods(out2, ["2025-01:2025-10"], 200, 3)["2025-01:2025-10"]
        self.assertEqual(json.dumps(res, sort_keys=True), json.dumps(res2, sort_keys=True))
        neutral, _ = coord_replay.replay(trades, self.c, [], neutral=True)
        self.assertTrue(all(t["w"] == t["base"] for t in neutral))

    def test_news_block_and_no_lookahead(self):
        ev = [{"ev": "NFP", "t_utc": parse_utc("2025-03-07T13:30:00Z")}]
        rows = [{"t0": int(parse_utc("2025-03-07T13:40:00Z").timestamp()), "t1": int(parse_utc("2025-03-07T14:40:00Z").timestamp()),
                 "code": "X005", "family": "F", "side": "BUY", "phase": "RPBN", "r_STD": 1.0, "size": 1.0, "risk_mult": 1.0},
                {"t0": int(parse_utc("2025-03-07T15:00:00Z").timestamp()), "t1": int(parse_utc("2025-03-07T16:00:00Z").timestamp()),
                 "code": "X005", "family": "F", "side": "BUY", "phase": "RPBN", "r_STD": -1.0, "size": 1.0, "risk_mult": 1.0}]
        rows.insert(0, dict(rows[0], t0=int(parse_utc("2025-03-06T01:00:00Z").timestamp()), t1=int(parse_utc("2025-03-06T02:00:00Z").timestamp())))
        trades, _ = coord_replay.load_stream(self.stream(rows))
        out, _ = coord_replay.replay(trades, self.c, ev, 1.0, 0.5)
        self.assertEqual([t["why"] for t in out], ["", "NEWS", ""])
        # the outcome of a trade never changes a decision taken before it closed
        rows2 = copy.deepcopy(rows)
        rows2[2]["r_STD"] = 1.0
        out2, _ = coord_replay.replay(coord_replay.load_stream(self.stream(rows2))[0], self.c, ev, 1.0, 0.5)
        self.assertEqual([t["w"] for t in out], [t["w"] for t in out2])

    def test_cli_json(self):
        path = self.stream(self.synthetic()[:200])
        out = self.tmp / "o.json"
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(coord_replay.main([str(path), "--root", str(self.root), "--periods", "2025-01:2025-02",
                                                "--reps", "20", "--json", str(out), "--set", "alloc_enable=true"]), 0)
        self.assertIn("vs control", buf.getvalue())
        data = json.loads(out.read_text())
        self.assertTrue(data["settings"]["alloc_enable"])
        self.assertIn("2025-01:2025-02", data["periods"])


if __name__ == "__main__":
    unittest.main()
