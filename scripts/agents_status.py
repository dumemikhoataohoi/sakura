"""GitHub agent self-monitoring: health of the three agents of docs/agents.md and their sub-agents, plus daily KPIs.

Writes reports/agents/<UTC date>.md and .json. Offline and deterministic: it reads only repository files (stored
aggregate reports, data/latest.json, data/quality.json, control/control.json, control/CHANGELOG.md, reports/coord,
the summary pages and the bridge source) and takes the clock from --now. Aggregate data only: no per-trade data,
prices, times of trades or money; accounts appear as the public 16-hex hash the reports already use.

Every row has a status OK / WARN / FAIL (SKIP = not checkable offline), a stable reason (no ages, so it only changes
when the situation changes) and metrics (ages and counts). The files are rewritten only when a status, a reason or a
KPI changed (or on a new UTC day), so the hourly workflow commits on change, not on every run.

EA ages are counted in open-market hours: gold is closed from Friday WEEKEND_CLOSE to Sunday WEEKEND_OPEN (UTC), and
the EA sends no heartbeat over a closed market, so a quiet weekend is not an outage.

Department KPIs (docs/agents.md section 6, requirement E5): every KPI of the table that repository files can measure,
with its current value, the pre-registered target (config/settings.json > agents_kpi), OK / WARN / FAIL against it and
the trend of the last 7 / 30 days versus the 7 / 30 days before (up / down / flat + delta). KPIs that need data outside
the repo (EA journal, research rounds, the session itself) are SKIP with the reason. The company block (cash flow)
repeats the figures the owner reads first: net R per week / month, % days with trades, % profitable days, month
drawdown, win% after coordination and the coordination lift (real win% minus the win% of the signals blocked by CTRL).

Usage: python scripts/agents_status.py [--root DIR] [--now ISO] [--msg-file PATH]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

import agg
import control
import coordinator_report as coord
import reports_lib as rl
import vault
from zumi_common import (UTC, dumps_pretty, iso_z, load_config, mask_secrets, parse_utc, repo_root, utc_now,
                         write_text_atomic)

SCHEMA = "zumi.agents_status"
VERSION = 1
OUT_DIR = Path("reports") / "agents"
STATUSES = ("OK", "SKIP", "WARN", "FAIL")          # severity order; SKIP never raises a roll-up
LOOKBACK_DAYS = 30

WEEKEND_CLOSE = (4, 21)          # Friday 21:00 UTC: gold closes (conservative: early close)
WEEKEND_OPEN = (6, 23)           # Sunday 23:00 UTC: gold reopens (conservative: late open)
REPORT_OK_H = 2.5                # report slot 60 min + delivery; heartbeat (bridge >= 2.4) fills quiet slots
REPORT_FAIL_H = 6.0
CTX_OK_H = 2.5                   # mkt.ctx / shadow refreshed on each M15 bar, sent with every report
CTX_FAIL_H = 24.0
LATEST_OK_H = 3.0                # data.yml runs hourly
LATEST_FAIL_H = 36.0             # the EA ignores latest.json older than InpGhMaxAgeHours (36)
CALENDAR_MIN_DAYS = 14           # events.covered_until should reach this far ahead
CONTROL_REFRESH_H = 48.0         # docs/control.md: refresh at least every 48 h while directives should stay active
CONTROL_STALE_H = control.STALE_AFTER.total_seconds() / 3600   # 72 h: the EA falls back to NEUTRAL
CONTROL_EXPIRY_WARN_H = 12.0
COORD_OK_DAYS = 1                # reports/coord written by the coordinator run of Claude (3 times a day)
COORD_FAIL_DAYS = 3

BRIDGE_SRC = Path("mql5") / "ZumiGitHubBridge.mqh"
VER_RE = re.compile(r'^#define ZGB_VERSION "(\d+)\.(\d+)"', re.M)
CHANGELOG_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2})Z seq (\d+):", re.M)


# ---------------------------------------------------------------- small helpers
def worst(statuses) -> str:
    vals = [s for s in statuses if s in STATUSES]
    real = [s for s in vals if s != "SKIP"]
    if not real:
        return "SKIP" if vals else "OK"
    return max(real, key=STATUSES.index)


def grade(age_h: float | None, ok_h: float, fail_h: float) -> str:
    if age_h is None:
        return "FAIL"
    return "OK" if age_h <= ok_h else ("WARN" if age_h <= fail_h else "FAIL")


def h1(v: float | None):
    return None if v is None else round(v, 1)


def _closures(t0: dt.datetime, t1: dt.datetime):
    monday = (t0 - dt.timedelta(days=t0.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    while monday <= t1:
        yield (monday + dt.timedelta(days=WEEKEND_CLOSE[0], hours=WEEKEND_CLOSE[1]),
               monday + dt.timedelta(days=WEEKEND_OPEN[0], hours=WEEKEND_OPEN[1]))
        monday += dt.timedelta(days=7)


def market_open(t: dt.datetime) -> bool:
    return not any(a <= t < b for a, b in _closures(t, t))


def open_hours(t0: dt.datetime, t1: dt.datetime) -> float:
    """Hours in [t0, t1) when gold trades (weekly closure removed; daily breaks and holidays are ignored)."""
    if t1 <= t0:
        return 0.0
    closed = sum(max(0.0, (min(t1, b) - max(t0, a)).total_seconds()) for a, b in _closures(t0, t1))
    return ((t1 - t0).total_seconds() - closed) / 3600


def _load(path: Path):
    try:
        return json.loads(vault.read_text(path))
    except (OSError, json.JSONDecodeError):
        return None


def _ver(text) -> tuple[int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _row(agent: str, sub: str, status: str, reason: str, **metrics) -> dict:
    return {"agent": agent, "sub": sub, "status": status, "reason": reason,
            "metrics": {k: v for k, v in sorted(metrics.items()) if v is not None}}


# ---------------------------------------------------------------- inputs
def agg_reports(root: Path, now: dt.datetime, days: int = LOOKBACK_DAYS) -> dict[str, list[dict]]:
    """Stored aggregate reports of the last `days` days per account, oldest first (none from the future)."""
    out: dict[str, list[dict]] = {}
    first = now.date() - dt.timedelta(days=days)
    for r in rl.load_reports(root, first, now.date() + dt.timedelta(days=1)):
        if r.get("kind") == "ea_agg" and isinstance(r.get("data"), dict) and r.get("ts", 0) <= now.timestamp() + 600:
            out.setdefault(str(r.get("acct", "")), []).append(r)
    return dict(sorted(out.items()))


def _ts(r: dict) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(r.get("ts", 0)), UTC)


def _ctx(r: dict) -> dict:
    return ((r.get("data") or {}).get("mkt") or {}).get("ctx") or {}


def newest_per_day(reps: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in reps:                                  # oldest first: the last one of a day wins
        out[str(r["data"].get("day", ""))] = r
    return out


def _block(r: dict | None, period: str) -> dict:
    return (((r or {}).get("data") or {}).get("periods") or {}).get(period) or {}


# ---------------------------------------------------------------- KPIs
def kpis(reps: list[dict], today: dt.date) -> dict:
    """Daily KPIs from the aggregate day blocks (newest report of each server day; Exness servers run on UTC)."""
    days = newest_per_day(reps)
    out: dict = {"trades_today": int(_block(days.get(today.isoformat()), "day").get("n", 0) or 0)}
    for w in (7, 30):
        window = [today - dt.timedelta(days=i) for i in range(w)]
        traded = profit = reported = 0
        for d in window:
            r = days.get(d.isoformat())
            if r is None:
                continue
            reported += 1
            blk = _block(r, "day")
            if int(blk.get("n", 0) or 0) > 0:
                traded += 1
                pnl = blk.get("net_pct") if blk.get("net_pct") is not None else blk.get("net_r")
                if pnl is not None and float(pnl) > 0:
                    profit += 1
        out[f"days_traded_{w}d"] = traded
        out[f"profitable_days_{w}d"] = profit
        out[f"reported_days_{w}d"] = reported
        out[f"weekdays_{w}d"] = sum(1 for d in window if d.weekday() < 5)
    last = reps[-1] if reps else None
    streak = None
    for period in ("r30", "mtd", "week", "day"):
        if "cur_ls" in _block(last, period):
            streak = int(_block(last, period)["cur_ls"] or 0)
            break
    out["cur_losing_streak"] = streak
    out["dd_week_pct"] = _block(last, "week").get("dd_pct")
    out["dd_mtd_pct"] = _block(last, "mtd").get("dd_pct")
    return out


# ---------------------------------------------------------------- EA agent
def risk_limits(cfg: dict) -> tuple[float, int]:
    """Drawdown warning = the coordinator's first R05 ladder step; streak warning = its R01 pause length."""
    c = coord.coord_cfg(cfg)
    ladder = c["dd_ladder"] if isinstance(c.get("dd_ladder"), list) and c["dd_ladder"] else [[c["dd_reduce_pct"], 0]]
    return float(min(step[0] for step in ladder)), int(c["streak_pause_n"])


def ea_rows(acct: str, reps: list[dict], now: dt.datetime, eff: dict, label: str, cfg: dict) -> list[dict]:
    agent = label
    if not reps:
        return [_row(agent, s, "FAIL", f"no aggregate report in {LOOKBACK_DAYS} days") for s in
                ("Reporter", "Market", "Signal", "Risk", "Execution", "Shadow", "Control client")]
    last = reps[-1]
    data = last["data"]
    ver = str(data.get("ver", ""))
    ver_t = _ver(ver) or (0, 0)
    rows = []

    # Reporter: last report and last heartbeat (bridge 2.4+ sends one per quiet slot while the market is open)
    age = open_hours(_ts(last), now)
    hbs = [r for r in reps if r["data"].get("reason") == "heartbeat"]
    hb_age = open_hours(_ts(hbs[-1]), now) if hbs else None
    st = grade(age, REPORT_OK_H, REPORT_FAIL_H)
    reason = {"OK": f"last report within {REPORT_OK_H} market h",
              "WARN": f"no report for > {REPORT_OK_H} market h",
              "FAIL": f"no report for > {REPORT_FAIL_H} market h (EA, terminal or token down?)"}[st]
    if hbs:
        reason += "; heartbeats seen"
    elif ver_t < (2, 4):
        reason += f"; bridge {ver or '?'} sends no heartbeat (needs >= 2.4)"
    else:
        reason += f"; no heartbeat in {LOOKBACK_DAYS} days (fine while every slot has a trade report)"
    rows.append(_row(agent, "Reporter", st, reason, report_age_h=h1(age), heartbeat_age_h=h1(hb_age),
                     last_reason=data.get("reason"), bridge=ver or None,
                     reports_24h=sum(1 for r in reps if (now - _ts(r)).total_seconds() <= 86400)))

    # Market: mkt.ctx (bridge 2.4 / EA161 v1.31)
    with_ctx = [r for r in reps if _ctx(r)]
    if not with_ctx:
        rows.append(_row(agent, "Market", "WARN", f"no mkt.ctx in {LOOKBACK_DAYS} days (needs bridge >= 2.4 and EA161 >= v1.31)"))
    else:
        c = _ctx(with_ctx[-1])
        cage = open_hours(_ts(with_ctx[-1]), now)
        st = grade(cage, CTX_OK_H, CTX_FAIL_H)
        rows.append(_row(agent, "Market", st, {"OK": "mkt.ctx fresh", "WARN": f"mkt.ctx older than {CTX_OK_H} market h",
                                               "FAIL": f"mkt.ctx older than {CTX_FAIL_H} market h"}[st],
                         ctx_age_h=h1(cage), phase=c.get("phase"), cds_side=c.get("side"), crisis=c.get("cr")))

    # Signal: does the EA still produce trades?
    week, r30 = _block(last, "week"), _block(last, "r30")
    wn, rn = int(week.get("n", 0) or 0), int(r30.get("n", 0) or 0)
    codes = len(r30.get("by_method") or {}) if r30 else len(_block(last, "mtd").get("by_method") or {})
    rows.append(_row(agent, "Signal", "OK" if wn > 0 else "WARN",
                     "trades in the last 7 days" if wn > 0 else "no closed trade in the last 7 days",
                     trades_7d=wn, trades_30d=rn if r30 else None, method_codes_30d=codes))

    # Risk: hard-rule counts, drawdown ladder, losing streak, crisis layer
    rule = week.get("rule") or {}
    hard = {k: int(rule.get(k, 0) or 0) for k in ("no_sl", "risk_cap") if int(rule.get(k, 0) or 0) > 0}
    soft = []
    if int(rule.get("daily_cap", 0) or 0) > 0:
        soft.append("daily loss cap hit this week")
    dd_warn, streak_n = risk_limits(cfg)
    wdd, mdd = week.get("dd_pct"), _block(last, "mtd").get("dd_pct")
    if wdd is not None and wdd >= dd_warn:
        soft.append(f"week drawdown >= {dd_warn:g} % (R05 ladder)")
    if mdd is not None and mdd >= dd_warn:
        soft.append(f"month drawdown >= {dd_warn:g} % (R05 ladder)")
    streak = kpis(reps, now.date())["cur_losing_streak"]
    if streak is not None and streak >= streak_n:
        soft.append(f"losing streak >= {streak_n}")
    cr = _ctx(with_ctx[-1]).get("cr") if with_ctx else None
    if isinstance(cr, str) and cr.startswith("BLOCK"):
        soft.append(f"crisis layer {cr}")
    if hard:
        st, reason = "FAIL", "hard rule broken this week: " + ", ".join(sorted(hard))
    elif soft:
        st, reason = "WARN", "; ".join(soft)
    else:
        st, reason = "OK", "no rule break this week; drawdown below the R05 ladder; no long losing streak"
    rows.append(_row(agent, "Risk", st, reason, dd_week_pct=wdd, dd_mtd_pct=mdd, cur_ls=streak,
                     rule_week=",".join(f"{k}={v}" for k, v in sorted(rule.items()) if v) or None))

    # Execution: only indirect signs in aggregates (news-window entries, EA flags of the day)
    day = str(data.get("day", ""))
    keys = sorted({f.get("key") for r in reps if str(r["data"].get("day")) == day
                   for f in (r["data"].get("flags") or []) if f.get("key")})
    news = int(rule.get("news", 0) or 0)
    issues = ([f"EA flags today: {', '.join(keys)}"] if keys else []) + (["entries inside a news window this week"] if news else [])
    rows.append(_row(agent, "Execution", "WARN" if issues else "OK",
                     "; ".join(issues) or "no EA flag today, no news-window entry this week", flags_today=len(keys) or None))

    # Shadow / Learning: blocked-signal statistics (bridge 2.5 / EA161 v1.32)
    with_sh = [r for r in reps if r["data"].get("shadow")]
    if not with_sh:
        rows.append(_row(agent, "Shadow", "WARN", f"no data.shadow in {LOOKBACK_DAYS} days (needs bridge >= 2.5 and EA161 >= v1.32)"))
    else:
        sh = with_sh[-1]["data"]["shadow"]
        sage = open_hours(_ts(with_sh[-1]), now)
        per = sh.get("periods") or {}
        pk = next((p for p in ("r30", "mtd", "week", "day") if p in per), None)
        counts = {k: int(v.get("n", 0)) for k, v in sorted((per.get(pk) or {}).items())} if pk else {}
        st = grade(sage, CTX_OK_H, CTX_FAIL_H)
        rows.append(_row(agent, "Shadow", st, {"OK": "shadow statistics fresh",
                                               "WARN": f"shadow statistics older than {CTX_OK_H} market h",
                                               "FAIL": f"shadow statistics older than {CTX_FAIL_H} market h"}[st],
                         shadow_age_h=h1(sage), shadow_open=sh.get("open"), shadow_period=pk,
                         shadow_n=",".join(f"{k}={v}" for k, v in counts.items()) or None))

    # Control client: the global.side the EA reports (mkt.ctx.gside, EA161 v1.33) vs the file in force
    gside = _ctx(with_ctx[-1]).get("gside") if with_ctx else None
    expect = (eff.get("global") or {}).get("side", "both")
    if gside is None:
        rows.append(_row(agent, "Control client", "SKIP", "EA does not report gside (needs EA161 >= v1.33)", file_side=expect))
    elif gside == expect:
        rows.append(_row(agent, "Control client", "OK", "EA applies the global.side of the file", ea_side=gside, file_side=expect))
    else:
        rows.append(_row(agent, "Control client", "WARN", "EA global.side differs from the file (lag up to "
                         "InpGhControlEveryMin, or the control channel is off)", ea_side=gside, file_side=expect))
    return rows


# ---------------------------------------------------------------- GitHub agent
def github_rows(root: Path, now: dt.datetime, all_reps: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    rows = []
    agent = "GitHub"

    latest = _load(root / "data" / "latest.json")
    if not isinstance(latest, dict) or not latest.get("generated_utc"):
        rows.append(_row(agent, "Data", "FAIL", "data/latest.json missing or unreadable"))
    else:
        age = (now - parse_utc(latest["generated_utc"])).total_seconds() / 3600
        st = grade(age, LATEST_OK_H, LATEST_FAIL_H)
        reason = {"OK": "latest.json fresh", "WARN": f"latest.json older than {LATEST_OK_H} h (data.yml late?)",
                  "FAIL": f"latest.json older than {LATEST_FAIL_H} h: the EA ignores it"}[st]
        cov = (latest.get("events") or {}).get("covered_until")
        if cov and cov < (now.date() + dt.timedelta(days=CALENDAR_MIN_DAYS)).isoformat():
            st = worst([st, "WARN"])
            reason += f"; calendar ends {cov}: extend data/calendar/events_us.csv"
        nxt = ((latest.get("events") or {}).get("next") or [{}])[0]
        rows.append(_row(agent, "Data", st, reason, latest_age_h=h1(age), day=latest.get("day"),
                         next_event=(f"{nxt.get('ev')} {nxt.get('utc')}" if nxt.get("ev") else None)))

    q = _load(root / "data" / "quality.json")
    if not isinstance(q, dict):
        rows.append(_row(agent, "Quality", "WARN", "data/quality.json missing"))
    else:
        issues = q.get("issues") or []
        stale = sorted({str(i.get("sid")) for i in issues if i.get("kind") == "stale"})
        errs = sorted({f"{i.get('sid')}:{i.get('kind')}" for i in issues if i.get("level") == "error"})
        warns = sorted({f"{i.get('sid')}:{i.get('kind')}" for i in issues if i.get("level") != "error" and i.get("kind") != "stale"})
        if not q.get("ok", True) or errs:
            st, reason = "FAIL", "quality not ok: " + (", ".join(errs) or "see data/quality.json")
        elif stale or warns:
            st, reason = "WARN", "; ".join(x for x in ((f"stale: {', '.join(stale)}" if stale else ""),
                                                        (f"warnings: {', '.join(warns)}" if warns else "")) if x)
        else:
            st, reason = "OK", "quality ok, no stale series"
        rows.append(_row(agent, "Quality", st, reason, ok=bool(q.get("ok", True)), issues=len(issues), since=q.get("since_utc")))

    reps = sorted((r for rs in all_reps.values() for r in rs), key=lambda r: r.get("ts", 0))
    stored_24h = [r for r in reps if (now - _ts(r)).total_seconds() <= 86400]
    if not reps:
        rows.append(_row(agent, "Report store", "WARN", f"no aggregate report stored in {LOOKBACK_DAYS} days"))
    else:
        age = open_hours(_ts(reps[-1]), now)
        st = "OK" if age <= 24 else "WARN"
        rows.append(_row(agent, "Report store", st, "reports stored in the last 24 market h" if st == "OK"
                         else "no report stored for > 24 market h", stored_24h=len(stored_24h),
                         accounts=len(all_reps), last_store_age_h=h1(age)))

    if not reps:
        rows.append(_row(agent, "Analytics", "SKIP", "no report to summarise"))
    else:
        d = dt.date.fromisoformat(str(reps[-1]["data"].get("day")))
        iso = d.isocalendar()
        pages = {"daily": root / "reports" / "daily" / f"{d.isoformat()}.md",
                 "weekly": root / "reports" / "weekly" / f"{iso[0]}-W{iso[1]:02d}.md",
                 "monthly": root / "reports" / "monthly" / f"{d.strftime('%Y-%m')}.md"}
        missing = [k for k, p in pages.items() if not vault.exists(p)]
        rows.append(_row(agent, "Analytics", "WARN" if missing else "OK",
                         f"summary page missing for the last report day: {', '.join(missing)}" if missing
                         else "daily, weekly and monthly pages exist for the last report day", day=d.isoformat()))

    # Coordinator: control.json in force (seq, expiry, hours left, 48 h refresh / 72 h staleness) + latest reports/coord
    raw = _load(root / control.CONTROL_PATH)
    norm, errors, _ = control.normalize(raw, now) if isinstance(raw, dict) else (None, ["missing or unreadable"], [])
    eff = control.effective(norm, now)
    coord_days = sorted(p.stem for p in vault.glob(root / "reports" / "coord", "*.md") if re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem))
    coord_last = coord_days[-1] if coord_days else None
    coord_age = (now.date() - dt.date.fromisoformat(coord_last)).days if coord_last else None
    if norm is None:
        st, parts = "FAIL", ["control.json invalid -> EA NEUTRAL"]
        upd_age = hours_left = None
    else:
        upd_age = (now - parse_utc(norm["updated_utc"])).total_seconds() / 3600
        exp = norm["global"].get("expires_utc")
        hours_left = (parse_utc(exp) - now).total_seconds() / 3600 if exp else None
        parts = [f"seq {norm['seq']}"]
        if upd_age > CONTROL_STALE_H:
            st = "FAIL"
            parts.append(f"updated > {CONTROL_STALE_H:.0f} h ago: stale, EA NEUTRAL")
        elif upd_age > CONTROL_REFRESH_H:
            st = "WARN"
            parts.append(f"updated > {CONTROL_REFRESH_H:.0f} h ago: refresh due (NEUTRAL after {CONTROL_STALE_H:.0f} h)")
        else:
            st = "OK"
            parts.append(f"updated within {CONTROL_REFRESH_H:.0f} h")
        if hours_left is not None and hours_left <= 0:
            parts.append("global directive expired")
        elif hours_left is not None and hours_left < CONTROL_EXPIRY_WARN_H:
            st = worst([st, "WARN"])
            parts.append(f"global directive expires within {CONTROL_EXPIRY_WARN_H:.0f} h")
        parts.append(f"effective: {eff['reason']}")
    if coord_age is None or coord_age > COORD_FAIL_DAYS:
        st = worst([st, "FAIL" if coord_age is not None else "WARN"])
        parts.append("no reports/coord suggestion" if coord_age is None else f"reports/coord older than {COORD_FAIL_DAYS} days")
    elif coord_age > COORD_OK_DAYS:
        st = worst([st, "WARN"])
        parts.append(f"reports/coord older than {COORD_OK_DAYS} day")
    g = (norm or {}).get("global") or {}
    rows.append(_row(agent, "Coordinator", st, "; ".join(parts), seq=(norm or {}).get("seq"),
                     updated_age_h=h1(upd_age), expires_utc=g.get("expires_utc"), hours_left=h1(hours_left),
                     past_48h=(None if upd_age is None else upd_age > CONTROL_REFRESH_H),
                     past_72h=(None if upd_age is None else upd_age > CONTROL_STALE_H), coord_last=coord_last))

    # Guard: the file the EA reads must be valid and normalized (control.yml check)
    if norm is None:
        rows.append(_row(agent, "Guard", "FAIL", "control.json invalid: " + "; ".join(errors)[:200]))
    elif raw != norm:
        rows.append(_row(agent, "Guard", "WARN", "control.json valid but not normalized (run scripts/control.py normalize)"))
    else:
        rows.append(_row(agent, "Guard", "OK", "control.json valid and normalized"))

    rows.append(_row(agent, "Alerts", "SKIP", "zumi-alerts PR needs the GitHub API: not checked offline"))
    return rows, eff


# ---------------------------------------------------------------- Claude agent
def claude_rows(root: Path, now: dt.datetime, all_reps: dict[str, list[dict]]) -> list[dict]:
    agent = "Claude"
    rows = [_row(agent, "Analyst", "SKIP", "market analysis lives in the session report: not checked offline")]
    log = root / control.CHANGELOG_PATH
    hits = CHANGELOG_RE.findall(vault.read_text(log)) if vault.exists(log) else []
    if not hits:
        rows.append(_row(agent, "Coordinator", "WARN", "no decision in control/CHANGELOG.md"))
    else:
        t, seq = hits[-1]
        age = (now - parse_utc(t + ":00Z")).total_seconds() / 3600
        st = grade(age, CONTROL_REFRESH_H, CONTROL_STALE_H)
        rows.append(_row(agent, "Coordinator", st, {"OK": f"control decision committed within {CONTROL_REFRESH_H:.0f} h",
                                                    "WARN": f"last control decision > {CONTROL_REFRESH_H:.0f} h ago",
                                                    "FAIL": f"last control decision > {CONTROL_STALE_H:.0f} h ago"}[st],
                         last_decision_age_h=h1(age), last_seq=int(seq)))
    rows.append(_row(agent, "Researcher", "SKIP", "research runs outside this public repo: not checked offline"))
    src = root / BRIDGE_SRC
    m = VER_RE.search(src.read_text(encoding="utf-8")) if src.is_file() else None
    repo_ver = f"{m.group(1)}.{m.group(2)}" if m else None
    ea_vers = sorted({str(rs[-1]["data"].get("ver")) for rs in all_reps.values() if rs and rs[-1]["data"].get("ver")})
    if repo_ver is None or not ea_vers:
        rows.append(_row(agent, "Builder", "SKIP", "bridge version unknown (no source or no report)", repo_bridge=repo_ver))
    else:
        behind = [v for v in ea_vers if (_ver(v) or (0, 0)) < _ver(repo_ver)]
        rows.append(_row(agent, "Builder", "WARN" if behind else "OK",
                         f"EA runs bridge {', '.join(behind)}, repo has {repo_ver}: compile in MetaEditor and deploy "
                         "(user decision)" if behind else f"EA runs the repo bridge {repo_ver}",
                         repo_bridge=repo_ver, ea_bridge=",".join(ea_vers)))
    rows.append(_row(agent, "Reviewer", "SKIP", "code review happens in the session: not checked offline"))
    rows.append(_row(agent, "Reporter", "SKIP", "user report happens in the session: not checked offline"))
    return rows


# ---------------------------------------------------------------- department KPIs (docs/agents.md section 6)
KPI_LOOKBACK_DAYS = 61           # 30-day window + the 30 days before it, plus today
TRENDS = (7, 30)
CELL_MIN_N = 30                  # shadow cell sample target (docs/agents.md: n >= 30 per cell)
EXEC_SKIP = "needs the EA journal (slippage, rejects, duplicates, spread): not in the aggregate reports of a public repo"
ACCT_METRICS = ("lift", "trades", "net_r", "avg_r", "win_pct", "days_traded", "profitable_days", "dd_week", "dd_month",
                "worst_day", "max_ls", "violations", "cells")
REPO_METRICS = ("overdue", "scenario", "fresh", "pages", "control_valid", "leaks")
# (id, department, KPI, scope "acct" / "repo", metric); a metric outside the two lists is the SKIP reason
KPI_DEFS = (
    ("exec.lift_pp", "Executive (Claude Coordinator)", "win% let through minus win% blocked by CTRL (shadow), pp", "acct", "lift"),
    ("exec.control_overdue_48h", "Executive (Claude Coordinator)", "control.json refresh overdue > 48 h (count)", "repo", "overdue"),
    ("analyst.scenario_hit_pct", "Analysis (Claude Analyst, EA Market)", "3-scenario hit rate %", "repo", "scenario"),
    ("signal.trades", "Production (EA Signal)", "closed trades in the window", "acct", "trades"),
    ("signal.days_traded_pct", "Production (EA Signal)", "% weekdays with >= 1 trade", "acct", "days_traded"),
    ("signal.avg_r", "Production (EA Signal)", "net R per trade", "acct", "avg_r"),
    ("signal.trades_growth_pct", "Production (EA Signal)", "+20 % trades per EA version (A14)", "acct",
     "per-version comparison comes from the research round (docs/research-protocol.md), not from live aggregates"),
    ("risk.dd_week_pct", "Risk (EA Risk)", "drawdown rolling week %", "acct", "dd_week"),
    ("risk.dd_month_pct", "Risk (EA Risk)", "drawdown month to date %", "acct", "dd_month"),
    ("risk.worst_day_r", "Risk (EA Risk)", "worst day net R", "acct", "worst_day"),
    ("risk.max_losing_streak", "Risk (EA Risk)", "longest losing streak", "acct", "max_ls"),
    ("risk.hard_violations", "Risk (EA Risk)", "hard-limit breaks (no_sl + risk_cap)", "acct", "violations"),
    ("execution.slippage", "Operations (EA Execution)", "slippage at entry", "acct", EXEC_SKIP),
    ("execution.duplicates", "Operations (EA Execution)", "duplicate orders", "acct", EXEC_SKIP),
    ("execution.errors", "Operations (EA Execution)", "rejected / failed orders", "acct", EXEC_SKIP),
    ("shadow.cells_n30_pct", "Audit (EA Shadow)", f"% shadow method x reason cells with n >= {CELL_MIN_N}", "acct", "cells"),
    ("data.fresh_days_pct", "Data (GitHub Data + Quality)", "% days with every series fresh (quality.max_age_days)", "repo", "fresh"),
    ("finance.pages_on_time_pct", "Finance (GitHub Analytics)", "% daily / weekly / monthly pages present for report days", "repo", "pages"),
    ("legal.control_valid_pct", "Legal (GitHub Guard)", "control.json valid and normalized (now) %", "repo", "control_valid"),
    ("legal.privacy_leaks", "Legal (GitHub Guard)", "stored reports failing the public whitelist or the token mask", "repo", "leaks"),
    ("rnd.build_gate", "R&D (Claude Researcher, Builder, Reviewer)", "new version beats the running one (section 9)", "repo",
     "build-gate results live in the research rounds outside this public repo"),
    ("rnd.review_findings", "R&D (Claude Researcher, Builder, Reviewer)", "bugs found in code review", "repo",
     "review findings are kept in the session, not in repo files"),
    ("owner.report_f7", "Owner relations (Claude Reporter)", "reports with true figures and a fix per weakness (F7)", "repo",
     "the user report is a session output: not measurable from repo files"),
)
# (id, KPI, metric, primary window in days or None = the configured window)
COMPANY_DEFS = (
    ("company.net_r_week", "net R, last 7 days", "net_r", 7),
    ("company.net_r_month", "net R, last 30 days", "net_r", 30),
    ("company.days_traded_pct", "% weekdays with >= 1 trade", "days_traded", None),
    ("company.profitable_days_pct", "% profitable days (net % > 0) of the days with trades", "profitable_days", None),
    ("company.dd_month_pct", "drawdown month to date %", "dd_month", None),
    ("company.win_pct", "win% after coordination (real trades)", "win_pct", None),
    ("company.lift_pp", "coordination lift: win% let through minus win% blocked by CTRL, pp", "lift", None),
)


def _window(end: dt.date, w: int) -> list[dt.date]:
    return [end - dt.timedelta(days=i) for i in range(w)]


def _days_in(per_day: dict[str, dict], end: dt.date, w: int) -> list[dict]:
    """Newest report of each server day in the window, oldest first."""
    return [per_day[d.isoformat()] for d in reversed(_window(end, w)) if d.isoformat() in per_day]


def _shadow_day(r: dict, reason: str) -> dict:
    return ((((r.get("data") or {}).get("shadow") or {}).get("periods") or {}).get("day") or {}).get(reason) or {}


def acct_metric(name: str, per_day: dict[str, dict], end: dt.date, w: int):
    """(value, sample n) of an account metric over the w days ending at `end`; value None = no data."""
    days = _days_in(per_day, end, w)
    blocks = [_block(r, "day") for r in days]
    n = sum(int(b.get("n", 0) or 0) for b in blocks)
    wins = sum(int(b.get("wins", 0) or 0) for b in blocks)
    losses = sum(int(b.get("losses", 0) or 0) for b in blocks)
    net_r = sum(float(b.get("net_r", 0) or 0) for b in blocks)
    if name == "trades":
        return (n if days else None), n
    if name == "net_r":
        return (round(net_r, 2) if days else None), n
    if name == "avg_r":
        return (round(net_r / n, 3) if n else None), n
    if name == "win_pct":
        return (round(100.0 * wins / (wins + losses), 1) if wins + losses else None), wins + losses
    if name in ("days_traded", "profitable_days"):
        start = min(per_day) if per_day else None        # days before the first stored report are not counted
        span = [d for d in _window(end, w) if start and d.isoformat() >= start]
        traded = [d for d in span if int(_block(per_day.get(d.isoformat()), "day").get("n", 0) or 0) > 0]
        if name == "days_traded":
            weekdays = sum(1 for d in span if d.weekday() < 5)
            hits = sum(1 for d in traded if d.weekday() < 5)
            return (round(100.0 * hits / weekdays, 1) if weekdays and days else None), weekdays
        prof = 0
        for d in traded:
            b = _block(per_day[d.isoformat()], "day")
            pnl = b.get("net_pct") if b.get("net_pct") is not None else b.get("net_r")
            prof += 1 if pnl is not None and float(pnl) > 0 else 0
        return (round(100.0 * prof / len(traded), 1) if traded else None), len(traded)
    if name in ("dd_week", "dd_month"):                 # snapshot: the block of the newest report in the window
        v = _block(days[-1] if days else None, "week" if name == "dd_week" else "mtd").get("dd_pct")
        return (None if v is None else round(float(v), 2)), len(days)
    if name == "worst_day":
        rs = [float(b.get("net_r", 0) or 0) for b in blocks if int(b.get("n", 0) or 0) > 0]
        return (round(min(rs), 2) if rs else (0.0 if days else None)), len(rs)
    if name == "max_ls":
        return (agg.merge_aggs(blocks)["max_ls"] if days else None), n
    if name == "violations":
        return (sum(int((b.get("rule") or {}).get(k, 0) or 0) for b in blocks for k in ("no_sl", "risk_cap"))
                if days else None), n
    if name == "lift":
        ctrl = [_shadow_day(r, "CTRL") for r in days]
        cw, cl = sum(int(c.get("wins", 0)) for c in ctrl), sum(int(c.get("losses", 0)) for c in ctrl)
        if not cw + cl or not wins + losses:
            return None, cw + cl
        return round(100.0 * wins / (wins + losses) - 100.0 * cw / (cw + cl), 1), cw + cl
    if name == "cells":                                 # snapshot: rolling-30-day cells of the newest shadow block
        snap = next((r for r in reversed(days) if (r["data"].get("shadow") or {}).get("by_method")), None)
        cells = snap["data"]["shadow"]["by_method"] if snap else {}
        if not cells:
            return None, 0
        return round(100.0 * sum(1 for c in cells.values() if int(c.get("n", 0)) >= CELL_MIN_N) / len(cells), 1), len(cells)
    raise KeyError(name)


class RepoFacts:
    """Repo-level inputs of the KPIs, read once (control changelog, data history, pages, stored report files)."""

    def __init__(self, root: Path, now: dt.datetime, cfg: dict, all_reps: dict[str, list[dict]]):
        self.root, self.now, self.cfg = root, now, cfg
        log = root / control.CHANGELOG_PATH
        self.decisions = sorted(parse_utc(t + ":00Z") for t, _ in
                                CHANGELOG_RE.findall(vault.read_text(log) if vault.exists(log) else ""))
        self.history: dict[str, list[str]] = {}
        for sid in (cfg.get("fred") or {}).get("series", []):
            path = root / "data" / "history" / f"{sid}.csv"
            if vault.exists(path):
                with io.StringIO(vault.read_text(path)) as fh:
                    self.history[sid] = sorted(row[0] for row in csv.reader(fh)
                                               if row and re.match(r"^\d{4}-\d{2}-\d{2}$", row[0]))
        self.report_days = sorted({str(r["data"].get("day")) for rs in all_reps.values() for r in rs})
        raw = _load(root / control.CONTROL_PATH)
        norm = control.normalize(raw, now)[0] if isinstance(raw, dict) else None
        self.control_ok = norm is not None and raw == norm
        ledger = root / str((cfg.get("agents_kpi") or {}).get("scenario_ledger", "reports/scenario_ledger.csv"))
        self.scenarios: list[tuple[str, int]] | None = None
        if vault.exists(ledger):                            # columns date, hit (1 = the scenario that played out was called)
            with io.StringIO(vault.read_text(ledger)) as fh:
                rows = list(csv.DictReader(fh))
            if rows and {"date", "hit"} <= set(rows[0]):
                self.scenarios = [(r["date"][:10], 1 if str(r["hit"]).strip().lower() in ("1", "true") else 0) for r in rows]
        self._leaks: dict[str, tuple[int, int]] = {}

    def leaks_on(self, day: dt.date) -> tuple[int, int]:
        """(files, leaking files) stored for `day`: a stored report must pass the public whitelist unchanged."""
        key = day.isoformat()
        if key not in self._leaks:
            files = bad = 0
            rcfg = self.cfg.get("reports") or {}
            for path in rl.iter_report_files(self.root, day, day):
                files += 1
                text = vault.read_text(path, self.root)
                try:
                    r = json.loads(text)
                    if r.get("kind") != "ea_agg":
                        bad += 1                        # a per-trade (v1) report has no place in a public repo
                        continue
                    clean, stripped = agg.sanitize_v2(r, rcfg, dt.datetime.fromtimestamp(int(r["ts"]), UTC), r["acct"])
                    bad += 1 if stripped or clean != r or mask_secrets(text) != text else 0
                except (ValueError, KeyError, TypeError, AttributeError):
                    bad += 1
            self._leaks[key] = (files, bad)
        return self._leaks[key]

    def metric(self, name: str, end: dt.date, w: int):
        days = _window(end, w)
        first = days[-1]
        if name == "overdue":                           # refresh deadlines (decision + 48 h) passed before the next decision
            limit = min(self.now, dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), UTC))
            if not self.decisions or self.decisions[0] > limit:
                return None, 0
            count = 0
            for i, t in enumerate(self.decisions):
                due = t + dt.timedelta(hours=CONTROL_REFRESH_H)
                nxt = self.decisions[i + 1] if i + 1 < len(self.decisions) else None
                if first <= due.date() <= end and due <= limit and (nxt is None or nxt > due):
                    count += 1
            return count, sum(1 for t in self.decisions if t <= limit)
        if name == "scenario":
            if self.scenarios is None:
                return None, 0
            hits = [h for d, h in self.scenarios if first.isoformat() <= d <= end.isoformat()]
            return (round(100.0 * sum(hits) / len(hits), 1) if hits else None), len(hits)
        if name == "fresh":                             # the quality.json stale rule, replayed on data/history
            series = (self.cfg.get("fred") or {}).get("series", [])
            if not series or any(s not in self.history for s in series):
                return None, 0
            ages = (self.cfg.get("quality") or {}).get("max_age_days") or {}
            ok = 0
            for d in days:
                fresh = True
                for sid in series:
                    lo = (d - dt.timedelta(days=int(ages.get(sid, ages.get("default", 7))))).isoformat()
                    dates = self.history[sid]
                    i = bisect.bisect_left(dates, d.isoformat())   # observations strictly before d (as-of D-1)
                    fresh = fresh and i > 0 and dates[i - 1] >= lo
                ok += 1 if fresh else 0
            return round(100.0 * ok / len(days), 1), len(days)
        if name == "pages":
            need = set()
            for d in (dt.date.fromisoformat(x) for x in self.report_days if first.isoformat() <= x <= end.isoformat()):
                iso = d.isocalendar()
                need |= {f"daily/{d.isoformat()}.md", f"weekly/{iso[0]}-W{iso[1]:02d}.md", f"monthly/{d.strftime('%Y-%m')}.md"}
            if not need:
                return None, 0
            return round(100.0 * sum(1 for p in need if vault.exists(self.root / "reports" / p)) / len(need), 1), len(need)
        if name == "control_valid":                     # only the current file is known offline
            return ((100.0 if self.control_ok else 0.0) if end == self.now.date() else None), 1
        if name == "leaks":
            counts = [self.leaks_on(d) for d in days]
            files = sum(c[0] for c in counts)
            return (sum(c[1] for c in counts) if files else None), files
        raise KeyError(name)


NO_DATA = {"lift": "no CTRL shadow outcomes in the window (needs bridge >= 2.5 and EA161 >= v1.32)",
           "cells": "no shadow by_method cells in the window (needs bridge >= 2.5 and EA161 >= v1.32)",
           "scenario": "no scenario ledger (agents_kpi.scenario_ledger, columns date, hit) with rows in the window",
           "fresh": "data/history files missing for a configured series"}


def kpi_status(value, n: int, spec: dict | None, metric: str = "") -> tuple[str, str]:
    if spec is None:
        return "SKIP", "no pre-registered target in config agents_kpi"
    if value is None:
        return "SKIP", NO_DATA.get(metric, "no data in the window")
    if spec.get("min_n") and n < spec["min_n"]:
        return "SKIP", f"sample n={n} < min_n {spec['min_n']}"
    t, wv, ge = spec["target"], spec.get("warn", spec["target"]), spec["op"] == ">="
    if (value >= t) if ge else (value <= t):
        return "OK", f"{spec['op']} target {t:g}"
    if (value >= wv) if ge else (value <= wv):
        return "WARN", f"misses target {t:g}, within warn {wv:g}"
    return "FAIL", f"beyond warn {wv:g} (target {t:g})"


def trend(fn, today: dt.date, w: int, spec: dict | None) -> dict:
    """The last w days versus the w days before them."""
    cur, _ = fn(today, w)
    prev, _ = fn(today - dt.timedelta(days=w), w)
    if cur is None or prev is None:
        return {"cur": cur, "prev": prev, "delta": None, "dir": "n/a", "better": None}
    delta = round(cur - prev, 3)
    d = "flat" if abs(delta) <= float((spec or {}).get("flat", 0.0)) else ("up" if delta > 0 else "down")
    better = None if d == "flat" or spec is None else ((d == "up") == (spec["op"] == ">="))
    return {"cur": cur, "prev": prev, "delta": delta, "dir": d, "better": better}


def kpi_entry(kid: str, dept: str, label: str, scope: str, fn, today: dt.date, specs: dict,
              window: int | None = None, skip: str | None = None, metric: str = "") -> dict:
    spec = specs.get(kid)
    out = {"id": kid, "dept": dept, "kpi": label, "scope": scope}
    if skip:
        return dict(out, status="SKIP", reason=skip)
    w = int(window or (spec or {}).get("window", 30))
    value, n = fn(today, w)
    st, reason = kpi_status(value, n, spec, metric)
    out.update(value=value, n=n, window_d=w, status=st, reason=reason,
               trend={f"{tw}d": trend(fn, today, tw, spec) for tw in TRENDS})
    if spec:
        out.update(op=spec["op"], target=spec["target"], warn=spec.get("warn", spec["target"]))
    return out


def department_kpis(root: Path, now: dt.datetime, cfg: dict) -> dict:
    specs = (cfg.get("agents_kpi") or {}).get("kpis") or {}
    reps = agg_reports(root, now, KPI_LOOKBACK_DAYS)
    facts = RepoFacts(root, now, cfg, reps)
    today = now.date()
    accts = list(reps) or ["-"]
    per_day = {a: newest_per_day(reps.get(a, [])) for a in accts}

    def acct_fn(metric, a):
        return lambda e, w: acct_metric(metric, per_day[a], e, w)

    rows = []
    for kid, dept, label, scope, metric in KPI_DEFS:
        scopes = accts if scope == "acct" else ["repo"]
        for a in scopes:
            if metric in ACCT_METRICS:
                rows.append(kpi_entry(kid, dept, label, a, acct_fn(metric, a), today, specs, metric=metric))
            elif metric in REPO_METRICS:
                rows.append(kpi_entry(kid, dept, label, a, lambda e, w, m=metric: facts.metric(m, e, w), today, specs,
                                      metric=metric))
            else:
                rows.append(kpi_entry(kid, dept, label, a, None, today, specs, skip=metric))
    company = {a: [kpi_entry(kid, "Company", label, a, acct_fn(metric, a), today, specs, window=win, metric=metric)
                   for kid, label, metric, win in COMPANY_DEFS] for a in accts}
    return {"departments": rows, "company": company}


# ---------------------------------------------------------------- assembly / output
def evaluate(root: Path, now: dt.datetime) -> dict:
    cfg = load_config(root)
    all_reps = agg_reports(root, now)
    gh, eff = github_rows(root, now, all_reps)
    ea: list[dict] = []
    accts = list(all_reps) or [""]
    for acct in accts:
        label = "EA" if len(accts) == 1 else f"EA {acct}"
        ea += ea_rows(acct, all_reps.get(acct, []), now, eff, label, cfg)
    cl = claude_rows(root, now, all_reps)
    rows = []
    for name, sub in (("EA", ea), ("GitHub", gh), ("Claude", cl)):
        st = worst(r["status"] for r in sub)
        bad = sorted({r["sub"] for r in sub if r["status"] == st and st in ("WARN", "FAIL")})
        rows.append(_row(name, "-", st, (f"{st}: {', '.join(bad)}" if bad else "all checked sub-agents OK")))
        rows += sub
    return {"schema": SCHEMA, "v": VERSION, "day": now.date().isoformat(), "as_of": iso_z(now),
            "market_open": market_open(now), "overall": worst(r["status"] for r in rows if r["sub"] == "-"),
            "rows": rows, "kpis": {a or "-": kpis(all_reps.get(a, []), now.date()) for a in accts},
            "kpi": department_kpis(root, now, cfg)}


def stable(res: dict) -> str:
    """What decides a rewrite: statuses, reasons and KPIs (ages and the clock are left out)."""
    return json.dumps({"day": res.get("day"), "overall": res.get("overall"), "kpis": res.get("kpis"), "kpi": res.get("kpi"),
                       "rows": [[r.get("agent"), r.get("sub"), r.get("status"), r.get("reason")] for r in res.get("rows", [])]},
                      sort_keys=True)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def render(res: dict) -> str:
    lines = [f"# Agent health {res['day']}", "",
             f"as of {res['as_of']} (UTC); overall **{res['overall']}**; market {'open' if res['market_open'] else 'closed'}. "
             "Machine-generated by `scripts/agents_status.py` from repository files only (aggregate data). "
             "EA ages are open-market hours (gold closed Friday 21:00 to Sunday 23:00 UTC). "
             "SKIP = not checkable offline. Rewritten only when a status, a reason or a KPI changes.", "",
             "## Agents (docs/agents.md)", "",
             "| agent | sub-agent | status | reason | metrics |", "|---|---|---|---|---|"]
    for r in res["rows"]:
        name = f"**{r['agent']}**" if r["sub"] == "-" else r["agent"]
        met = "; ".join(f"{k} {_fmt(v)}" for k, v in r["metrics"].items()) or "-"
        lines.append(f"| {name} | {r['sub']} | {r['status']} | {r['reason'].replace('|', '/')} | {met.replace('|', '/')} |")
    lines += ["", "## Daily KPIs (aggregate day blocks, newest report per server day)", "",
              "| account | trades today | days with trades 7d | 30d | profitable days 7d | 30d | losing streak | DD week % | DD MTD % |",
              "|---|---|---|---|---|---|---|---|---|"]
    for acct, k in res["kpis"].items():
        lines.append(f"| `{acct}` | {k['trades_today']} | {k['days_traded_7d']}/{k['weekdays_7d']} | "
                     f"{k['days_traded_30d']}/{k['weekdays_30d']} | {k['profitable_days_7d']}/{k['days_traded_7d']} | "
                     f"{k['profitable_days_30d']}/{k['days_traded_30d']} | {_fmt(k['cur_losing_streak'])} | "
                     f"{_fmt(k['dd_week_pct'])} | {_fmt(k['dd_mtd_pct'])} |")
    lines += ["", "Days with trades: x / weekdays in the window (days without a stored report count as no trade). "
              "Profitable days: day blocks with net % > 0 (after costs) / days with trades. Goal (docs/agents.md section 5): "
              "more days with trades and more profitable days, losing days capped. Not a promise of daily profit.", ""]
    kpi = res.get("kpi") or {}
    for acct, entries in (kpi.get("company") or {}).items():
        lines += [f"## Company KPIs (cash flow), account `{acct}`", ""] + kpi_table(entries, with_dept=False)
    if kpi.get("departments"):
        lines += ["## Department KPIs (docs/agents.md section 6)", ""] + kpi_table(kpi["departments"], with_dept=True)
        lines += ["Targets are pre-registered in `config/settings.json` > `agents_kpi`. Trend: the last 7 / 30 days versus "
                  "the 7 / 30 days before (snapshot KPIs: the value at each window end); `+` = better against the target, "
                  "`-` = worse. SKIP = not measurable from repo files, no data, or a sample below `min_n`.", ""]
    return "\n".join(lines)


def _trend_txt(t: dict | None) -> str:
    if not t or t.get("dir") == "n/a":
        return "n/a"
    mark = {True: " +", False: " -"}.get(t.get("better"), "")
    return f"{t['dir']} {t['delta']:+g}{mark}" if t["dir"] != "flat" else f"flat {t['delta']:+g}"


def kpi_table(entries: list[dict], with_dept: bool) -> list[str]:
    head = "| department | KPI | scope | value | target | status | 7d trend | 30d trend | note |" if with_dept else \
        "| KPI | value | target | status | 7d trend | 30d trend | note |"
    out = [head, "|" + "---|" * (head.count("|") - 1)]
    for e in entries:
        target = f"{e['op']} {e['target']:g} (warn {e['warn']:g})" if "op" in e else "-"
        val = "-" if e.get("value") is None else f"{e['value']:g}"
        if e.get("window_d"):
            val += f" ({e['window_d']}d, n {e.get('n', 0)})"
        tr = e.get("trend") or {}
        cells = [e["kpi"], val, target, e["status"], _trend_txt(tr.get("7d")), _trend_txt(tr.get("30d")), e["reason"]]
        if with_dept:
            cells = [e["dept"], e["kpi"], e["scope"]] + cells[1:]
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    return out + [""]


def write(root: Path, res: dict) -> bool:
    out = root / OUT_DIR
    js = out / f"{res['day']}.json"
    prev = _load(js)
    if isinstance(prev, dict) and stable(prev) == stable(res) and vault.exists(out / f"{res['day']}.md"):
        return False
    vault.write_text(js, dumps_pretty(res), root)
    vault.write_text(out / f"{res['day']}.md", render(res), root)
    return True


def commit_message(res: dict) -> str:
    tops = ", ".join(f"{r['agent']} {r['status']}" for r in res["rows"] if r["sub"] == "-")
    k = next(iter(res["kpis"].values()), {})
    return (f"agents: {res['day']} overall {res['overall']} ({tops}); trades today {k.get('trades_today', 0)}, "
            f"days with trades 7d {k.get('days_traded_7d', 0)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent health + daily KPIs -> reports/agents/<date>.md/.json (offline)")
    ap.add_argument("--root")
    ap.add_argument("--now")
    ap.add_argument("--msg-file", help="write the commit message here")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    res = evaluate(root, now)
    changed = write(root, res)
    msg = commit_message(res)
    if args.msg_file:
        Path(args.msg_file).write_text(vault.commit_message(root, msg, "agents") + "\n", encoding="utf-8")
    line = ("wrote " if changed else "unchanged ") + f"{OUT_DIR.as_posix()}/{res['day']}.md"
    print(vault.public_log(root, f"{line}: {msg}", line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
