"""GitHub agent self-monitoring (scripts/agents_status.py): health rows per agent / sub-agent, daily KPIs, market-hours
ages, write-on-change and the data.yml step. Offline: a temp repo root with synthetic files, a fixed clock."""
import contextlib
import datetime as dt
import io
import json
import re
import shutil
import unittest

from helpers import REPO, make_root

import agents_status as ast
import control
from zumi_common import dumps_pretty, iso_z, parse_utc

NOW = parse_utc("2026-11-27T15:30:00Z")   # Friday, market open
ACCT = "0123456789abcdef"


def day_block(n, wins, net_pct, cur_ls=0, dd=0.5, rule=None):
    return {"n": n, "wins": wins, "losses": n - wins, "be": 0, "net_r": float(2 * wins - n), "net_pct": net_pct,
            "cur_ls": cur_ls, "dd_pct": dd, "rule": rule or {}, "by_method": {"M001": {"n": n, "wins": wins}} if n else {}}


class AgentsStatusTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.write_latest(NOW - dt.timedelta(minutes=20))
        self.write_quality(True, [])
        self.write_control(NOW - dt.timedelta(hours=3))
        (self.root / "reports" / "coord").mkdir(parents=True)
        (self.root / "reports" / "coord" / f"{NOW.date().isoformat()}.md").write_text("# x\n", encoding="utf-8")
        (self.root / "mql5").mkdir()
        (self.root / "mql5" / "ZumiGitHubBridge.mqh").write_text('#define ZGB_VERSION "2.7"\n', encoding="utf-8")
        for page in ("daily/2026-11-27.md", "weekly/2026-W48.md", "monthly/2026-11.md"):
            (self.root / "reports" / page).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "reports" / page).write_text("x\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- fixtures
    def write_latest(self, gen):
        obj = {"day": gen.date().isoformat(), "generated_utc": iso_z(gen),
               "events": {"covered_until": "2027-12-31", "next": [{"ev": "NFP", "utc": "2026-12-04T13:30:00Z"}]}}
        (self.root / "data" / "latest.json").write_text(json.dumps(obj), encoding="utf-8")

    def write_quality(self, ok, issues):
        (self.root / "data" / "quality.json").write_text(json.dumps({"ok": ok, "issues": issues}), encoding="utf-8")

    def write_control(self, updated, ttl_h=48):
        raw = {"v": 1, "author": "claude", "seq": 7, "updated_utc": iso_z(updated),
               "global": {"risk_mult": 0.5, "pause_all": False, "reason_code": "R05",
                          "expires_utc": iso_z(updated + dt.timedelta(hours=ttl_h))},
               "methods": {}, "news_blocks": [], "notes_vi": "test"}
        norm, errors, _ = control.normalize(raw, updated)
        self.assertEqual(errors, [])
        (self.root / "control").mkdir(exist_ok=True)
        (self.root / control.CONTROL_PATH).write_text(dumps_pretty(norm), encoding="utf-8")
        (self.root / control.CHANGELOG_PATH).write_text(
            f"# Control changelog\n\n- {updated.strftime('%Y-%m-%dT%H:%M')}Z seq 7: test (R05)\n", encoding="utf-8")

    def write_report(self, ts, day, blocks, reason="periodic", ver="2.7", ctx=None, shadow=None, flags=None):
        data = {"ea": "Zumi", "ver": ver, "reason": reason, "day": day, "periods": blocks, "flags": flags or []}
        if ctx:
            data["mkt"] = {"ctx": ctx}
        if shadow:
            data["shadow"] = shadow
        rep = {"v": 2, "kind": "ea_agg", "acct": ACCT, "rid": f"{ACCT}-{int(ts.timestamp())}", "part": 1, "parts": 1,
               "ts": int(ts.timestamp()), "data": data}
        folder = self.root / "reports" / ts.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ACCT}_{ts.strftime('%Y%m%dT%H%M%SZ')}.json").write_text(json.dumps(rep), encoding="utf-8")

    def healthy(self, age_min=30):
        ts = NOW - dt.timedelta(minutes=age_min)
        blk = day_block(4, 3, 0.4)
        self.write_report(ts, "2026-11-27", {"day": blk, "week": day_block(9, 6, 0.6), "mtd": day_block(20, 12, 1.1),
                                             "r30": day_block(25, 14, 1.2)},
                          reason="heartbeat", ctx={"phase": "SDPBN", "cr": "OK", "gside": "both"},
                          shadow={"v": 1, "open": 2, "periods": {"r30": {"CELL": {"n": 5, "wins": 2, "losses": 3, "to": 0, "net_r": -1.0}}}})

    def rows(self, res):
        return {(r["agent"], r["sub"]): r for r in res["rows"]}

    # ---- tests
    def test_market_hours(self):
        fri = parse_utc("2026-11-27T20:00:00Z")
        self.assertAlmostEqual(ast.open_hours(fri, parse_utc("2026-11-30T00:00:00Z")), 2.0)   # Fri 20-21 + Sun 23-24
        self.assertFalse(ast.market_open(parse_utc("2026-11-28T12:00:00Z")))
        self.assertTrue(ast.market_open(parse_utc("2026-11-29T23:30:00Z")))
        self.assertAlmostEqual(ast.open_hours(fri, fri + dt.timedelta(hours=1)), 1.0)

    def test_all_healthy(self):
        self.healthy()
        res = ast.evaluate(self.root, NOW)
        rows = self.rows(res)
        for key in (("EA", "Reporter"), ("EA", "Market"), ("EA", "Signal"), ("EA", "Risk"), ("EA", "Shadow"),
                    ("EA", "Control client"), ("GitHub", "Data"), ("GitHub", "Quality"), ("GitHub", "Coordinator"),
                    ("GitHub", "Guard"), ("Claude", "Coordinator"), ("Claude", "Builder")):
            self.assertEqual(rows[key]["status"], "OK", (key, rows[key]))
        self.assertEqual(rows[("GitHub", "Alerts")]["status"], "SKIP")
        self.assertEqual(res["overall"], "OK")
        self.assertEqual(rows[("EA", "Shadow")]["metrics"]["shadow_n"], "CELL=5")
        co = rows[("GitHub", "Coordinator")]["metrics"]
        self.assertEqual((co["seq"], co["past_48h"], co["past_72h"], co["hours_left"]), (7, False, False, 45.0))
        # every sub-agent of docs/agents.md has a row
        doc = (REPO / "docs" / "agents.md").read_text(encoding="utf-8")
        for sub in ("Market", "Signal", "Risk", "Execution", "Reporter", "Data", "Quality", "Analytics",
                    "Coordinator", "Guard", "Alerts", "Analyst", "Researcher", "Builder", "Reviewer"):
            self.assertIn(f"**{sub}**", doc)
            self.assertTrue(any(r["sub"] == sub for r in res["rows"]), sub)

    def test_report_age_uses_market_hours(self):
        self.healthy(age_min=8 * 60)                                   # 8 open hours: EA silent
        self.assertEqual(self.rows(ast.evaluate(self.root, NOW))[("EA", "Reporter")]["status"], "FAIL")
        sunday = parse_utc("2026-11-29T22:00:00Z")                     # last report Fri 20:30, market still closed
        shutil.rmtree(self.root / "reports" / "2026")
        self.write_report(parse_utc("2026-11-27T20:30:00Z"), "2026-11-27", {"day": day_block(1, 1, 0.1)})
        r = self.rows(ast.evaluate(self.root, sunday))[("EA", "Reporter")]
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["metrics"]["report_age_h"], 0.5)

    def test_old_bridge_and_missing_blocks(self):
        self.write_report(NOW - dt.timedelta(minutes=40), "2026-11-27", {"day": day_block(2, 1, 0.1)}, ver="2.3")
        rows = self.rows(ast.evaluate(self.root, NOW))
        self.assertIn("sends no heartbeat", rows[("EA", "Reporter")]["reason"])
        self.assertEqual(rows[("EA", "Market")]["status"], "WARN")
        self.assertEqual(rows[("EA", "Shadow")]["status"], "WARN")
        self.assertEqual(rows[("EA", "Control client")]["status"], "SKIP")
        self.assertEqual(rows[("Claude", "Builder")]["status"], "WARN")
        self.assertIn("2.3", rows[("Claude", "Builder")]["reason"])

    def test_risk_and_execution(self):
        self.write_report(NOW - dt.timedelta(minutes=30), "2026-11-27",
                          {"day": day_block(2, 0, -0.5, cur_ls=5), "week": day_block(5, 1, -2.0, cur_ls=5, dd=3.4, rule={"news": 1}),
                           "mtd": day_block(9, 3, -1.0, dd=3.4), "r30": day_block(9, 3, -1.0, cur_ls=5)},
                          flags=[{"key": "dd_week"}], ctx={"cr": "BLOCK:VOL"})
        rows = self.rows(ast.evaluate(self.root, NOW))
        risk = rows[("EA", "Risk")]
        self.assertEqual(risk["status"], "WARN")
        for part in ("week drawdown >= 3 %", "losing streak >= 4", "crisis layer BLOCK:VOL"):
            self.assertIn(part, risk["reason"])
        self.assertEqual(rows[("EA", "Execution")]["status"], "WARN")
        self.write_report(NOW - dt.timedelta(minutes=20), "2026-11-27", {"week": day_block(5, 1, -1.0, rule={"no_sl": 1})})
        self.assertEqual(self.rows(ast.evaluate(self.root, NOW))[("EA", "Risk")]["status"], "FAIL")

    def test_data_quality_and_control_staleness(self):
        self.healthy()
        self.write_quality(True, [{"sid": "DTWEXBGS", "kind": "stale", "level": "warn", "msg": "old"}])
        self.write_latest(NOW - dt.timedelta(hours=40))
        self.write_control(NOW - dt.timedelta(hours=50), ttl_h=72)
        rows = self.rows(ast.evaluate(self.root, NOW))
        self.assertEqual(rows[("GitHub", "Quality")]["status"], "WARN")
        self.assertIn("stale: DTWEXBGS", rows[("GitHub", "Quality")]["reason"])
        self.assertEqual(rows[("GitHub", "Data")]["status"], "FAIL")
        co = rows[("GitHub", "Coordinator")]
        self.assertEqual((co["status"], co["metrics"]["past_48h"], co["metrics"]["past_72h"]), ("WARN", True, False))
        self.assertEqual(rows[("Claude", "Coordinator")]["status"], "WARN")
        self.write_control(NOW - dt.timedelta(hours=80))
        co = self.rows(ast.evaluate(self.root, NOW))[("GitHub", "Coordinator")]
        self.assertEqual((co["status"], co["metrics"]["past_72h"]), ("FAIL", True))
        self.assertIn("EA NEUTRAL", co["reason"])
        self.write_quality(False, [{"sid": "VIXCLS", "kind": "range", "level": "error", "msg": "x"}])
        self.assertEqual(self.rows(ast.evaluate(self.root, NOW))[("GitHub", "Quality")]["status"], "FAIL")

    def test_kpis(self):
        days = {"2026-11-27": day_block(3, 2, 0.3), "2026-11-26": day_block(2, 0, -0.4), "2026-11-25": day_block(0, 0, 0.0),
                "2026-11-24": day_block(4, 3, 0.2), "2026-11-18": day_block(5, 4, 0.9)}
        for d, blk in days.items():
            t = parse_utc(d + "T20:00:00Z") if d != "2026-11-27" else NOW - dt.timedelta(hours=1)
            self.write_report(t, d, {"day": blk, "week": day_block(9, 5, 0.1, dd=1.2), "mtd": day_block(14, 9, 1.0, dd=1.5),
                                     "r30": day_block(14, 9, 1.0, cur_ls=2)})
        # an older report of the same day is ignored (newest per day wins)
        self.write_report(NOW - dt.timedelta(hours=5), "2026-11-27", {"day": day_block(1, 0, -0.1)})
        k = ast.evaluate(self.root, NOW)["kpis"][ACCT]
        self.assertEqual(k["trades_today"], 3)
        self.assertEqual((k["days_traded_7d"], k["profitable_days_7d"], k["weekdays_7d"]), (3, 2, 5))
        self.assertEqual((k["days_traded_30d"], k["profitable_days_30d"]), (4, 3))
        self.assertEqual((k["cur_losing_streak"], k["dd_week_pct"], k["dd_mtd_pct"]), (2, 1.2, 1.5))

    def test_write_only_on_change_and_deterministic(self):
        self.healthy()
        res = ast.evaluate(self.root, NOW)
        self.assertTrue(ast.write(self.root, res))
        js = self.root / "reports" / "agents" / "2026-11-27.json"
        md = js.with_suffix(".md")
        first = (js.read_text(encoding="utf-8"), md.read_text(encoding="utf-8"))
        self.assertEqual(js.read_text(encoding="utf-8"), dumps_pretty(json.loads(first[0])))   # sorted keys, pretty
        later = NOW + dt.timedelta(minutes=40)                    # ages move, statuses do not
        self.assertFalse(ast.write(self.root, ast.evaluate(self.root, later)))
        self.assertEqual((js.read_text(encoding="utf-8"), md.read_text(encoding="utf-8")), first)
        self.assertTrue(ast.write(self.root, ast.evaluate(self.root, NOW + dt.timedelta(hours=3))))   # EA silent -> WARN
        self.assertIn("| EA | Reporter | WARN |", md.read_text(encoding="utf-8"))
        other = make_root()
        try:
            shutil.copytree(self.root / "reports", other / "reports", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("agents"))
            for p in ("data/latest.json", "data/quality.json", "control", "mql5"):
                src = self.root / p
                (shutil.copytree if src.is_dir() else shutil.copy)(src, other / p)
            self.assertEqual(ast.render(ast.evaluate(other, NOW)), ast.render(res))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_main_and_privacy(self):
        self.healthy()
        msg = self.root / "msg.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ast.main(["--root", str(self.root), "--now", iso_z(NOW), "--msg-file", str(msg)]), 0)
        self.assertTrue(msg.read_text(encoding="utf-8").startswith("agents: 2026-11-27 overall OK"))
        text = (self.root / "reports" / "agents" / "2026-11-27.json").read_text(encoding="utf-8")
        for key in ('"t_in"', '"price"', '"ticket"', '"balance"', '"login"', '"cmt"'):
            self.assertNotIn(key, text)
        self.assertNotRegex(text, r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_")

    def test_no_reports(self):
        res = ast.evaluate(self.root, NOW)
        rows = self.rows(res)
        self.assertEqual(rows[("EA", "Reporter")]["status"], "FAIL")
        self.assertEqual(res["kpis"]["-"]["trades_today"], 0)
        self.assertIn("| `-` | 0 |", ast.render(res))

    def test_workflow_step(self):
        wf = (REPO / ".github" / "workflows" / "data.yml").read_text(encoding="utf-8")
        self.assertIn("python scripts/agents_status.py --msg-file", wf)
        self.assertIn("reports/agents/", wf)
        self.assertRegex(wf, r"permissions:\n  contents: write")
        self.assertNotIn("pull-requests", wf)
        for line in wf.splitlines():                              # no event text interpolated into a shell command
            self.assertFalse(re.search(r"\$\{\{\s*github\.event", line), line)


if __name__ == "__main__":
    unittest.main()
