"""Department and company KPIs of scripts/agents_status.py (docs/agents.md section 6, requirement E5): values, the
pre-registered targets of config/settings.json > agents_kpi, OK / WARN / FAIL, 7 / 30-day trends, SKIP reasons, the
coordination lift from shadow CTRL outcomes, repo-level KPIs and write-on-change. Offline, fixed clock."""
import datetime as dt
import json
import shutil
import unittest

from helpers import REPO, make_root

import agents_status as ast
import control
from zumi_common import dumps_pretty, iso_z, load_config, parse_utc

NOW = parse_utc("2026-11-27T15:30:00Z")
TODAY = NOW.date()
ACCT = "0123456789abcdef"


class KpiTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_report(self, day: dt.date, blk: dict, shadow: dict | None = None, extra: dict | None = None):
        ts = dt.datetime.combine(day, dt.time(20, 0), dt.timezone.utc) if day != TODAY else NOW - dt.timedelta(minutes=30)
        data = {"ea": "Zumi", "ver": "2.7", "reason": "periodic", "day": day.isoformat(), "flags": [],
                "periods": {"day": blk, "week": dict(blk, dd_pct=1.5), "mtd": dict(blk, dd_pct=2.5)}}
        if shadow:
            data["shadow"] = shadow
        data.update(extra or {})
        rep = {"v": 2, "kind": "ea_agg", "acct": ACCT, "rid": f"{ACCT}-{int(ts.timestamp())}", "part": 1, "parts": 1,
               "ts": int(ts.timestamp()), "data": data}
        folder = self.root / "reports" / ts.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{ACCT}_{ts.strftime('%Y%m%dT%H%M%SZ')}.json"
        path.write_text(dumps_pretty(rep), encoding="utf-8")
        return path

    def history(self, wins_recent=3, wins_old=2, ctrl=None):
        """Every weekday of the last 61 days: 4 trades; the last 30 days win `wins_recent`, the 30 before `wins_old`."""
        for i in range(ast.KPI_LOOKBACK_DAYS):
            d = TODAY - dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            w = wins_recent if i < 30 else wins_old
            blk = {"n": 4, "wins": w, "losses": 4 - w, "be": 0, "net_r": float(2 * w - 4), "net_pct": 0.25 * (2 * w - 4),
                   "max_ls": 4 - w, "cur_ls": 0, "lead_ls": 0, "dd_pct": 0.5, "rule": {}}
            shadow = None
            if ctrl is not None:
                shadow = {"v": 1, "periods": {"day": {"CTRL": {"n": 4, "wins": ctrl, "losses": 4 - ctrl, "to": 0,
                                                                "net_r": float(2 * ctrl - 4)}}},
                          "by_method": {"M001:CTRL": {"n": 40, "wins": 10, "net_r": -20.0},
                                        "M002:CELL": {"n": 12, "wins": 6, "net_r": 0.0}}}
            self.write_report(d, blk, shadow)

    def kpi(self, res=None, scope=ACCT):
        res = res or ast.evaluate(self.root, NOW)
        out = {e["id"]: e for e in res["kpi"]["departments"] if e["scope"] in (scope, "repo")}
        out.update({e["id"]: e for e in res["kpi"]["company"].get(scope, [])})
        return out

    # ---- tests
    def test_targets_are_preregistered_in_config(self):
        specs = load_config(REPO)["agents_kpi"]["kpis"]
        measurable = [k for k, _, _, _, m in ast.KPI_DEFS if m in ast.ACCT_METRICS + ast.REPO_METRICS]
        for kid in measurable + [c[0] for c in ast.COMPANY_DEFS]:
            self.assertIn(kid, specs)
            self.assertIn(specs[kid]["op"], (">=", "<="))
            w = specs[kid].get("warn", specs[kid]["target"])
            self.assertTrue(w <= specs[kid]["target"] if specs[kid]["op"] == ">=" else w >= specs[kid]["target"], kid)
        self.assertEqual(sorted(set(specs) - set(measurable) - {c[0] for c in ast.COMPANY_DEFS}), [])
        # every department of the section 6 table has KPI rows (measured or SKIP with a reason)
        self.assertEqual(len({d for _, d, _, _, _ in ast.KPI_DEFS}), 11)
        doc = (REPO / "docs" / "agents.md").read_text(encoding="utf-8")
        table = doc[doc.index("## 6."):doc.index("## 7.")]
        self.assertEqual(table.count("\n| **"), 11)

    def test_company_block_values_status_and_trend(self):
        self.history(wins_recent=3, wins_old=2)
        k = self.kpi()
        wp = k["company.win_pct"]
        self.assertEqual((wp["value"], wp["status"], wp["target"]), (75.0, "OK", 55.0))
        self.assertEqual(wp["trend"]["30d"], {"cur": 75.0, "prev": 50.0, "delta": 25.0, "dir": "up", "better": True})
        self.assertEqual(wp["trend"]["7d"]["dir"], "flat")
        self.assertEqual(k["company.net_r_week"]["value"], 10.0)            # 5 weekdays x (3 - 1)
        self.assertEqual(k["company.net_r_month"]["window_d"], 30)
        self.assertEqual(k["company.net_r_month"]["trend"]["30d"]["prev"], 0.0)
        self.assertEqual(k["company.days_traded_pct"]["value"], 100.0)
        self.assertEqual(k["company.profitable_days_pct"]["value"], 100.0)
        self.assertEqual(k["company.dd_month_pct"]["value"], 2.5)
        self.assertEqual(k["company.lift_pp"]["status"], "SKIP")           # no shadow: lift not measurable
        self.assertIn("CTRL", k["company.lift_pp"]["reason"])
        self.assertEqual(k["signal.trades"]["value"], 88)                   # 22 weekdays x 4
        self.assertEqual(k["signal.trades"]["status"], "WARN")              # target 100, warn 50
        self.assertEqual(k["risk.max_losing_streak"]["value"], 1)
        self.assertEqual(k["risk.worst_day_r"]["trend"]["30d"]["better"], True)

    def test_coordination_lift_from_shadow_ctrl(self):
        self.history(wins_recent=3, wins_old=3, ctrl=1)
        k = self.kpi()
        lift = k["company.lift_pp"]
        self.assertEqual((lift["value"], lift["status"], lift["n"]), (50.0, "OK", 88))   # 75 % passed - 25 % blocked
        self.assertEqual(k["exec.lift_pp"]["value"], 50.0)
        self.assertEqual(k["shadow.cells_n30_pct"]["value"], 50.0)          # 1 of 2 cells has n >= 30
        self.assertEqual(k["shadow.cells_n30_pct"]["status"], "WARN")

    def test_status_follows_config_and_min_n(self):
        self.history()
        path = self.root / "config" / "settings.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["agents_kpi"]["kpis"]["company.win_pct"].update(target=80.0, warn=78.0)
        cfg["agents_kpi"]["kpis"]["signal.avg_r"]["min_n"] = 1000
        del cfg["agents_kpi"]["kpis"]["risk.worst_day_r"]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        k = self.kpi()
        self.assertEqual(k["company.win_pct"]["status"], "FAIL")
        self.assertEqual(k["signal.avg_r"]["status"], "SKIP")
        self.assertIn("< min_n 1000", k["signal.avg_r"]["reason"])
        self.assertEqual(k["risk.worst_day_r"]["status"], "SKIP")
        self.assertIn("no pre-registered target", k["risk.worst_day_r"]["reason"])

    def test_unmeasurable_kpis_skip_with_reason(self):
        k = self.kpi(scope="-")
        for kid in ("execution.slippage", "execution.duplicates", "execution.errors", "signal.trades_growth_pct",
                    "rnd.build_gate", "rnd.review_findings", "owner.report_f7", "analyst.scenario_hit_pct"):
            self.assertEqual(k[kid]["status"], "SKIP", kid)
            self.assertTrue(k[kid]["reason"], kid)
        self.assertEqual(k["company.net_r_week"]["status"], "SKIP")        # no report at all

    def test_scenario_ledger(self):
        rows = ["date,hit"] + [f"{(TODAY - dt.timedelta(days=i)).isoformat()},{1 if i % 4 else 0}" for i in range(40)]
        (self.root / "reports").mkdir(exist_ok=True)
        (self.root / "reports" / "scenario_ledger.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        e = self.kpi(scope="-")["analyst.scenario_hit_pct"]
        self.assertEqual((e["value"], e["n"], e["status"]), (73.3, 30, "OK"))

    def test_repo_kpis(self):
        # control: decisions 26 and 29 Oct (> 48 h gap = 1 overdue), then fresh ones; current file valid + normalized
        raw = control.neutral_file(NOW - dt.timedelta(hours=2), seq=3)
        norm, errors, _ = control.normalize(raw, NOW)
        self.assertEqual(errors, [])
        (self.root / "control").mkdir()
        (self.root / control.CONTROL_PATH).write_text(dumps_pretty(norm), encoding="utf-8")
        (self.root / control.CHANGELOG_PATH).write_text(
            "# Control changelog\n\n- 2026-11-20T10:00Z seq 1: a\n- 2026-11-24T10:00Z seq 2: b\n"
            "- 2026-11-25T10:00Z seq 3: c\n- 2026-11-27T09:00Z seq 4: d\n", encoding="utf-8")
        # data history: every series fresh except SP500, which stops 12 days ago (max_age 7) -> the last ~5 days stale
        cfg = load_config(self.root)
        (self.root / "data" / "history").mkdir(parents=True)
        for sid in cfg["fred"]["series"]:
            stop = 12 if sid == "SP500" else 1
            days = [TODAY - dt.timedelta(days=i) for i in range(70, stop - 1, -1)]
            text = "date,value\n" + "".join(f"{d.isoformat()},1.0\n" for d in days if d.weekday() < 5)
            (self.root / "data" / "history" / f"{sid}.csv").write_text(text, encoding="utf-8")
        # reports: one clean, one with a per-trade key (leak); pages exist only for the clean day
        self.write_report(TODAY, {"n": 1, "wins": 1, "losses": 0, "net_r": 1.0})
        self.write_report(TODAY - dt.timedelta(days=1), {"n": 1, "wins": 0, "losses": 1, "net_r": -1.0},
                          extra={"price": 1.0})
        for page in ("daily/2026-11-27.md", "weekly/2026-W48.md", "monthly/2026-11.md"):
            (self.root / "reports" / page).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "reports" / page).write_text("x\n", encoding="utf-8")
        k = self.kpi()
        self.assertEqual((k["exec.control_overdue_48h"]["value"], k["exec.control_overdue_48h"]["status"]), (1, "WARN"))
        fresh = k["data.fresh_days_pct"]
        self.assertLess(fresh["value"], 100.0)
        self.assertEqual(fresh["status"], "FAIL")                           # below warn 90
        self.assertEqual(fresh["trend"]["30d"]["better"], False)
        self.assertEqual((k["finance.pages_on_time_pct"]["value"], k["finance.pages_on_time_pct"]["n"]), (75.0, 4))
        self.assertEqual((k["legal.privacy_leaks"]["value"], k["legal.privacy_leaks"]["status"]), (1, "FAIL"))
        self.assertEqual(k["legal.control_valid_pct"]["value"], 100.0)
        self.assertEqual(k["legal.control_valid_pct"]["trend"]["30d"]["dir"], "n/a")

    def test_rewrite_only_on_kpi_change_and_deterministic(self):
        self.history()
        res = ast.evaluate(self.root, NOW)
        self.assertTrue(ast.write(self.root, res))
        js = self.root / "reports" / "agents" / f"{TODAY.isoformat()}.json"
        text = js.read_text(encoding="utf-8")
        self.assertEqual(text, dumps_pretty(json.loads(text)))             # sorted keys, pretty
        self.assertEqual(json.dumps(ast.evaluate(self.root, NOW)["kpi"], sort_keys=True), json.dumps(res["kpi"], sort_keys=True))
        self.assertFalse(ast.write(self.root, ast.evaluate(self.root, NOW + dt.timedelta(minutes=20))))
        self.write_report(TODAY, {"n": 9, "wins": 1, "losses": 8, "net_r": -7.0, "net_pct": -1.0})   # a KPI moves
        self.assertTrue(ast.write(self.root, ast.evaluate(self.root, NOW + dt.timedelta(minutes=40))))
        md = js.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("## Company KPIs (cash flow)", md)
        self.assertIn("## Department KPIs (docs/agents.md section 6)", md)
        for key in ('"price"', '"ticket"', '"balance"', '"profit"', '"t_in"'):
            self.assertNotIn(key, js.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
