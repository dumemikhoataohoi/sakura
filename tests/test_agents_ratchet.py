"""KPI roles (docs/agents.md 6.1), the KPI ratchet and the weekly review (6.2) of scripts/agents_status.py: 4 met ISO
weeks raise the target to the best weekly value + step, targets never drop below the configured floor, risk KPIs only
tighten or hold, the state lives in reports/agents/kpi_state.json (through the vault) and departments that miss 2 weeks
in a row are flagged. Offline, fixed clock."""
import datetime as dt
import json
import shutil
import unittest

from helpers import REPO, make_root

import agents_status as ast
import vault
from zumi_common import dumps_pretty, load_config, parse_utc

NOW = parse_utc("2026-11-27T15:30:00Z")   # Friday of 2026-W48; last complete week = W47 (ends Sunday 22 Nov)
TODAY = NOW.date()
ACCT = "0123456789abcdef"


class RatchetTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def history(self, trades=8, wins=6, days=ast.KPI_LOOKBACK_DAYS + 14):
        for i in range(days):
            d = TODAY - dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            ts = dt.datetime.combine(d, dt.time(20, 0), dt.timezone.utc)
            blk = {"n": trades, "wins": wins, "losses": trades - wins, "be": 0, "net_r": float(2 * wins - trades),
                   "net_pct": 0.1 * (2 * wins - trades), "max_ls": 1, "cur_ls": 0, "lead_ls": 0, "dd_pct": 0.4, "rule": {}}
            rep = {"v": 2, "kind": "ea_agg", "acct": ACCT, "rid": f"{ACCT}-{int(ts.timestamp())}", "part": 1, "parts": 1,
                   "ts": int(ts.timestamp()), "data": {"ea": "Zumi", "ver": "2.8", "reason": "periodic", "day": d.isoformat(),
                                                       "flags": [], "periods": {"day": blk, "week": dict(blk, dd_pct=1.0),
                                                                                "mtd": dict(blk, dd_pct=2.0)}}}
            folder = self.root / "reports" / ts.strftime("%Y/%m/%d")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{ACCT}_{ts.strftime('%Y%m%dT%H%M%SZ')}.json").write_text(dumps_pretty(rep), encoding="utf-8")

    def set_spec(self, kid, **kw):
        path = self.root / "config" / "settings.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["agents_kpi"]["kpis"][kid].update(kw)
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def state(self, res, kid, scope=ACCT):
        return res["kpi_state"]["kpis"][f"{kid}@{scope}"]

    # ---- 6.1 roles
    def test_roles_from_config_match_section_6_1(self):
        roles = load_config(REPO)["agents_kpi"]["roles"]
        doc = (REPO / "docs" / "agents.md").read_text(encoding="utf-8")
        org = doc[doc.index("### 6.1"):doc.index("### 6.2")]
        for dept in {d for _, d, _, _, _ in ast.KPI_DEFS} | {"Company"}:
            self.assertIn(dept, roles)
            self.assertIn(f"**{roles[dept]}**", org, dept)
        self.history()
        res = ast.evaluate(self.root, NOW)
        for e in res["kpi"]["departments"] + res["kpi"]["company"][ACCT]:
            self.assertEqual(e["role"], roles[e["dept"]])
        self.assertIn("| department | chức vụ | KPI |", ast.render(res))

    # ---- 6.2 ratchet
    def test_four_met_weeks_raise_the_target(self):
        self.history(trades=8)                                    # ~170 trades per 30 days, floor 100, step +5 %
        res = ast.evaluate(self.root, NOW)
        st = self.state(res, "signal.trades")
        hist = st["history"]
        self.assertEqual([h["outcome"] for h in hist], ["met"] * 4)
        self.assertEqual([h["week"] for h in hist], ["2026-W44", "2026-W45", "2026-W46", "2026-W47"])
        best = max(h["value"] for h in hist)
        self.assertEqual(st["current_target"], round(best * 1.05, 3))
        self.assertEqual((st["floor"], st["weeks_met"], hist[-1]["raised_to"]), (100.0, 0, st["current_target"]))
        row = next(e for e in res["kpi"]["departments"] if e["id"] == "signal.trades")
        self.assertEqual((row["target"], row["floor"]), (st["current_target"], 100.0))   # status against the raised target
        # % weekdays traded: floor 90, +2 points, capped at 100
        self.assertEqual(self.state(res, "signal.days_traded_pct")["current_target"], 100.0)
        # no step configured -> holds at the floor even after 4 met weeks
        self.assertEqual(self.state(res, "signal.avg_r")["current_target"], 0.05)

    def test_target_never_below_floor_and_never_looser(self):
        st = {"op": ">=", "target": 100.0, "step": 0.05, "step_kind": "pct"}
        self.assertEqual(ast.raised_target("signal.trades", st, 150.0, [120.0, 130.0]), 150.0)   # best*1.05 < current
        self.assertEqual(ast.raised_target("signal.trades", dict(st, step=-0.5), 150.0, [300.0]), 150.0)  # negative step holds
        self.history(trades=8)
        state = ast.evaluate(self.root, NOW)["kpi_state"]
        state["kpis"][f"signal.trades@{ACCT}"]["current_target"] = 50.0        # tampered below the floor
        (self.root / "reports" / "agents").mkdir(parents=True)
        vault.write_text(self.root / ast.STATE_PATH, dumps_pretty(state), self.root)
        later = ast.evaluate(self.root, NOW + dt.timedelta(days=7))
        self.assertGreaterEqual(self.state(later, "signal.trades")["current_target"], 100.0)
        self.set_spec("signal.trades", target=500.0)                          # a raised floor is adopted at once
        self.assertEqual(self.state(ast.evaluate(self.root, NOW), "signal.trades")["current_target"], 500.0)

    def test_risk_kpis_only_tighten_or_hold(self):
        for kid in ast.RISK_KPIS:
            self.assertTrue(kid.startswith("risk.") or kid == "company.dd_month_pct")
        dd = {"op": "<=", "target": 3.0, "step": 0.1, "step_kind": "pct"}
        self.assertEqual(ast.raised_target("risk.dd_week_pct", dd, 3.0, [1.0, 2.0]), 0.9)    # tighter: best 1.0 - 10 %
        self.assertEqual(ast.raised_target("risk.dd_week_pct", dd, 0.5, [1.0, 2.0]), 0.5)    # never looser than now
        worst = {"op": ">=", "target": -3.0, "step": 0.1, "step_kind": "pct"}
        self.assertEqual(ast.raised_target("risk.worst_day_r", worst, -3.0, [-1.0, -2.0]), -0.9)
        self.assertEqual(ast.raised_target("risk.hard_violations", {"op": "<=", "target": 0, "step": 0}, 0.0, [0.0]), 0.0)
        self.history()
        res = ast.evaluate(self.root, NOW)
        for kid in ("risk.dd_week_pct", "risk.dd_month_pct", "risk.worst_day_r", "risk.max_losing_streak",
                    "risk.hard_violations", "company.dd_month_pct"):
            st = self.state(res, kid)
            self.assertTrue(st["risk"])
            looser = st["current_target"] > st["floor"] if st["op"] == "<=" else st["current_target"] < st["floor"]
            self.assertFalse(looser, kid)

    def test_state_persisted_through_vault_and_advances_one_week(self):
        self.history()
        res = ast.evaluate(self.root, NOW)
        self.assertTrue(ast.write(self.root, res))
        text = vault.read_text(self.root / ast.STATE_PATH, self.root)
        self.assertEqual(text, dumps_pretty(json.loads(text)))                # deterministic, sorted keys
        self.assertEqual(json.loads(text)["last_week"], "2026-W47")
        day = json.loads(vault.read_text(self.root / "reports" / "agents" / "2026-11-27.json", self.root))
        self.assertNotIn("kpi_state", day)
        self.assertFalse(ast.write(self.root, ast.evaluate(self.root, NOW + dt.timedelta(minutes=30))))   # same week
        nxt = ast.evaluate(self.root, NOW + dt.timedelta(days=7))
        self.assertEqual(nxt["kpi_state"]["last_week"], "2026-W48")
        self.assertEqual(len(self.state(nxt, "signal.avg_r")["history"]), 5)
        self.assertEqual(dumps_pretty(nxt["kpi_state"]), dumps_pretty(ast.evaluate(self.root, NOW + dt.timedelta(days=7))["kpi_state"]))

    def test_weekly_review_flags_departments(self):
        self.history(trades=1, wins=1)                            # ~21 trades / 30 days: Production misses every week
        res = ast.evaluate(self.root, NOW)
        review = {r["dept"]: r for r in res["kpi"]["review"]}
        prod = review["Production (EA Signal)"]
        self.assertEqual((prod["weeks_failed"], prod["flag"], prod["role"]),
                         (4, ast.IMPROVE_FLAG, "Giám đốc sản xuất (Trưởng phòng tín hiệu)"))
        self.assertEqual(review["Risk (EA Risk)"]["weeks_failed"], 0)
        self.assertEqual(review["Risk (EA Risk)"]["flag"], "")
        self.assertEqual(review["Operations (EA Execution)"]["last_outcome"], "not measured")   # only SKIP KPIs
        self.assertEqual(len(review), 12)
        md = ast.render(res)
        self.assertIn("## Weekly review", md)
        self.assertIn(f"**{ast.IMPROVE_FLAG}**", md)


if __name__ == "__main__":
    unittest.main()
