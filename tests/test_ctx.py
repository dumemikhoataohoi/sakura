"""EA market context (bridge 2.4 mkt.ctx), report reason whitelist and the hourly heartbeat report. Public-mode repo."""
import copy
import datetime as dt
import json
import unittest

from helpers import FakeDataSession, fixture_json, make_root

import agg
import alerts
import build_latest
import daily_summary
import store_report
from zumi_common import load_config, parse_utc

NOW = parse_utc("2026-11-25T16:00:00Z")
ACCT = "a1b2c3d4e5f60718"

CTX = {
    "tf": {"D1": {"dir": -1, "rsi": 38, "adx": 27, "pb": 12, "e50": -1, "e200": 1, "ichi": 0},
           "H4": {"dir": -1, "rsi": 41, "adx": 22, "pb": -5, "e50": -1, "e200": -1, "ichi": -1},
           "H1": {"dir": 1, "rsi": 55, "adx": 18, "pb": 71, "e50": 1, "e200": -1, "ichi": 1},
           "M15": {"dir": 1, "rsi": 61, "adx": 25, "pb": 96, "e50": 1, "e200": 1, "ichi": 1}},
    "phase": "SDPBL", "ref": -1,
    "rev": ["D1:SWEEP_LO", "H4:PIN_BULL", "H1:DIV_BULL", "REF:R7"],
    "sr_up_atr": 1.25, "sr_dn_atr": 0.4,
    "vol": {"atrp": 0.35, "terc": "N", "rk": 62},
    "mv": {"h4": 0.8, "h24": -1.65},
    "w1": "INSIDE", "d1": "PIN_UP", "h4": "ENG_DN",
    "an": {"pl": 0.48, "ps": 0.47, "n": 63},
    "scn": {"n": 100, "h": 24, "a": {"p": 39, "to_atr": -1.2}, "b": {"p": 11, "pb_atr": 0.3, "to_atr": -0.8},
            "c": {"p": 41, "to_atr": 0.9, "ext_atr": 0.4}, "none": 9},
    "dir_auc": 0.512, "cr": "OK",
}


def payload(ctx=None, reason="periodic", ts_shift=0):
    p = fixture_json("payload_agg.json")
    p["ts"] += ts_shift
    p["data"]["reason"] = reason
    if ctx is not None:
        p["data"]["mkt"]["ctx"] = copy.deepcopy(ctx)
    return p


class CtxValidatorTest(unittest.TestCase):
    def test_valid_ctx_passes_unchanged(self):
        stripped = set()
        self.assertEqual(agg.sanitize_ctx(copy.deepcopy(CTX), stripped), CTX)
        self.assertEqual(stripped, set())
        self.assertLess(len(json.dumps(CTX, separators=(",", ":"))), 1500)   # the EA block stays under ~1.5 KB
        root = make_root()
        res = store_report.store(root, payload(CTX), NOW)
        self.assertEqual(res["stripped"], [])
        self.assertEqual(json.loads(res["path"].read_text())["data"]["mkt"]["ctx"], CTX)

    def test_prices_and_unknown_keys_are_stripped(self):
        bad = copy.deepcopy(CTX)
        bad["price"] = 4123.5                                      # unknown key
        bad["tf"]["D1"]["close"] = 4100.25                         # unknown key inside a TF row
        bad["tf"]["W1"] = {"dir": 1}                               # TF not in the whitelist
        bad["tf"]["H1"]["rsi"] = 140                               # out of range
        bad["tf"]["H4"]["e50"] = 0                                 # e50 is -1 / 1 only
        bad["sr_up_atr"] = 4188.0                                  # a price disguised as an ATR distance
        bad["scn"]["a"]["to_atr"] = 4123.5
        bad["scn"]["c"]["level"] = 4090.0
        bad["mv"]["h24"] = 4100
        bad["phase"] = "SD|PB|L"                                   # not [A-Z]{2,8}
        bad["cr"] = "BLOCK Gap 12.50 > 0.50 x ATR D1 25.00"        # free text with numbers
        bad["rev"] = ["H4:PIN_BEAR", "London breakout 4100", "H4:PIN_BEAR", "X1:SWEEP_HI", "D1:R7"]
        bad["vol"]["atrp"] = 4100.0
        stripped = set()
        out = agg.sanitize_ctx(bad, stripped)
        for key in ("data.mkt.ctx.price", "data.mkt.ctx.tf.D1.close", "data.mkt.ctx.tf.W1", "data.mkt.ctx.tf.H1.rsi",
                    "data.mkt.ctx.tf.H4.e50", "data.mkt.ctx.sr_up_atr", "data.mkt.ctx.scn.a.to_atr",
                    "data.mkt.ctx.scn.c.level", "data.mkt.ctx.mv.h24", "data.mkt.ctx.phase", "data.mkt.ctx.cr",
                    "data.mkt.ctx.rev.<invalid>", "data.mkt.ctx.vol.atrp"):
            self.assertIn(key, stripped)
        self.assertEqual(out["rev"], ["H4:PIN_BEAR"])              # invalid codes dropped, duplicates merged
        self.assertNotIn("sr_up_atr", out)
        self.assertEqual(out["scn"]["a"], {"p": 39})
        self.assertNotIn("rsi", out["tf"]["H1"])
        self.assertFalse(any("London" in k for k in stripped))     # stripped values never reach the log
        root = make_root()
        res = store_report.store(root, payload(bad), NOW)
        text = res["path"].read_text()
        for leaked in ("4100", "4123", "4188", "4090", "London", "Gap 12"):
            self.assertNotIn(leaked, text)

    def test_oversize_lists_are_truncated(self):
        many = copy.deepcopy(CTX)
        many["rev"] = [f"REF:R{i}" for i in range(1, 10)] + ["D1:SWEEP_HI", "D1:SWEEP_LO", "H1:PIN_BEAR"]
        stripped = set()
        out = agg.sanitize_ctx(many, stripped)
        self.assertEqual(len(out["rev"]), agg.CTX_REV_MAX)
        self.assertEqual(out["rev"], [f"REF:R{i}" for i in range(1, 9)])
        self.assertIn("data.mkt.ctx.rev.<truncated>", stripped)
        huge = copy.deepcopy(CTX)
        huge["rev"] = ["H1:PIN_BULL"] * 10000
        self.assertEqual(agg.sanitize_ctx(huge, set())["rev"], ["H1:PIN_BULL"])

    def test_non_object_ctx_and_bad_types(self):
        stripped = set()
        self.assertIsNone(agg.sanitize_ctx("4100", stripped))
        self.assertIn("data.mkt.ctx", stripped)
        out = agg.sanitize_ctx({"ref": True, "dir_auc": "0.5", "an": {"pl": 1.5}, "scn": {"a": {"to_atr": 1.0}}}, set())
        self.assertIsNone(out)                                     # nothing valid left


class ReasonAndHeartbeatTest(unittest.TestCase):
    def test_reason_whitelist(self):
        root = make_root()
        res = store_report.store(root, payload(CTX, reason="heartbeat"), NOW)
        self.assertEqual(res["report"]["data"]["reason"], "heartbeat")
        res = store_report.store(root, payload(CTX, reason="ask me at 4100", ts_shift=60), NOW)
        self.assertEqual(res["report"]["data"]["reason"], "other")
        self.assertIn("data.reason", res["stripped"])

    def test_unchanged_heartbeat_is_not_stored_and_does_not_alert(self):
        root = make_root()
        build_latest.build(root, NOW, session=FakeDataSession())
        first = store_report.store(root, payload(CTX), NOW)
        daily_summary.summarize(root, [dt.date(2026, 11, 25)])
        keys = {a["key"] for a in alerts.evaluate(root, load_config(root), NOW)}
        files = sorted((root / "reports").rglob("*.json"))
        # same content one slot later: no new file, so the reports workflow has nothing to commit
        hb = store_report.store(root, payload(CTX, reason="heartbeat", ts_shift=3600), NOW)
        self.assertTrue(hb.get("unchanged"))
        self.assertIsNone(hb["path"])
        self.assertEqual(sorted((root / "reports").rglob("*.json")), files)
        self.assertEqual(daily_summary.summarize(root, hb["dates"]), [])
        # a heartbeat with new market context is stored, but raises no alert that was not there before
        moved = copy.deepcopy(CTX)
        moved["tf"]["M15"]["rsi"] = 66
        hb2 = store_report.store(root, payload(moved, reason="heartbeat", ts_shift=7200), NOW + dt.timedelta(hours=2))
        self.assertIsNotNone(hb2["path"])
        self.assertNotEqual(hb2["path"], first["path"])
        keys2 = {a["key"] for a in alerts.evaluate(root, load_config(root), NOW + dt.timedelta(hours=2))}
        self.assertEqual(keys2 - keys, set())

    def test_zero_trade_heartbeat_raises_no_alerts(self):
        root = make_root()
        build_latest.build(root, NOW, session=FakeDataSession())
        p = payload(CTX, reason="heartbeat")
        p["data"]["eq"] = {"day_pl_pct": 0.0, "eq_day_pct": 0.0, "float_pct": 0.0}
        p["data"]["mkt"]["move_atr"] = 0.2
        p["data"]["flags"] = []
        p["data"]["periods"] = {k: {"n": 0} for k in ("day", "week", "mtd", "r30")}
        store_report.store(root, p, NOW)
        keys = {a["key"] for a in alerts.agg_alerts(root, load_config(root), NOW)}
        self.assertEqual(keys, set())


class CtxRenderTest(unittest.TestCase):
    def test_daily_market_context_section(self):
        root = make_root()
        store_report.store(root, payload(CTX), NOW)
        later = copy.deepcopy(CTX)
        later["scn"]["a"]["p"] = 45
        store_report.store(root, payload(later, reason="heartbeat", ts_shift=3600), NOW)
        daily_summary.summarize(root, [dt.date(2026, 11, 25)])
        day = (root / "reports/daily/2026-11-25.md").read_text()
        self.assertIn("#### Market context (EA report 16:00 UTC)", day)
        self.assertIn("phase `SDPBL`; reference side SELL; crisis layer `OK`; ATR(H1) 0.35 % of price (tercile N, rank 62).", day)
        self.assertIn("| D1 | -1 | 38 | 27 | 12 | below | above | in |", day)
        self.assertIn("`D1:SWEEP_LO`, `H4:PIN_BULL`", day)
        self.assertIn("direction AUC over 30 days 0.512", day)
        self.assertIn("| a continuation | 45 | - | -1.20 | - |", day)
        self.assertIn("| b pullback, then resume | 11 | +0.30 | -0.80 | - |", day)
        self.assertIn("| c reversal | 41 | - | +0.90 | +0.40 |", day)
        self.assertIn("| 15:00 | SDPBL | SELL | 39 / -1.20 |", day)    # scenario log keeps every report of the day
        self.assertIn("| 16:00 | SDPBL | SELL | 45 / -1.20 |", day)
        self.assertNotIn("Market context", (root / "reports/weekly/2026-W48.md").read_text())


class CdsCtxTest(unittest.TestCase):
    """EA161 v1.33: confluence direction score in mkt.ctx (ints x100, side words only)."""
    CDS = {"cds": 34, "side": "buy", "gside": "auto", "cds_tf": {"W1": -22, "D1": 41, "H4": 63, "H1": 55, "M15": 0, "M5": -11}}

    def test_valid_cds_fields_pass(self):
        ctx = dict(copy.deepcopy(CTX), **copy.deepcopy(self.CDS))
        stripped = set()
        self.assertEqual(agg.sanitize_ctx(copy.deepcopy(ctx), stripped), ctx)
        self.assertEqual(stripped, set())
        self.assertLess(len(json.dumps(ctx, separators=(",", ":"))), 1800)   # bridge ZGB_CTX_MAX

    def test_invalid_cds_fields_are_stripped(self):
        bad = {"cds": 150, "side": "long", "gside": "both;", "cds_tf": {"W1": 10, "H2": 5, "D1": 0.5, "H4": 4100}}
        stripped = set()
        out = agg.sanitize_ctx(dict(copy.deepcopy(CTX), **bad), stripped)
        for k in ("cds", "side", "gside"):
            self.assertNotIn(k, out)
        self.assertEqual(out["cds_tf"], {"W1": 10})
        self.assertTrue(any("cds_tf" in x for x in stripped))
        self.assertIsNone(agg.sanitize_ctx({"cds_tf": {"D1": 4100}}, set()))

    def test_daily_summary_shows_cds(self):
        rep = {"ts": int(NOW.timestamp()), "data": {"mkt": {"ctx": dict(copy.deepcopy(CTX), **copy.deepcopy(self.CDS))}}}
        txt = "\n".join(daily_summary.ctx_section(rep, [rep]))
        self.assertIn("CDS +34 -> buy", txt)
        self.assertIn("global.side `auto`", txt)
        self.assertIn("H4 +63", txt)


if __name__ == "__main__":
    unittest.main()
