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

Usage: python scripts/agents_status.py [--root DIR] [--now ISO] [--msg-file PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import control
import coordinator_report as coord
import reports_lib as rl
from zumi_common import UTC, dumps_pretty, iso_z, load_config, parse_utc, repo_root, utc_now, write_text_atomic

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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _ver(text) -> tuple[int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _row(agent: str, sub: str, status: str, reason: str, **metrics) -> dict:
    return {"agent": agent, "sub": sub, "status": status, "reason": reason,
            "metrics": {k: v for k, v in sorted(metrics.items()) if v is not None}}


# ---------------------------------------------------------------- inputs
def agg_reports(root: Path, now: dt.datetime) -> dict[str, list[dict]]:
    """Stored aggregate reports of the last LOOKBACK_DAYS per account, oldest first (none from the future)."""
    out: dict[str, list[dict]] = {}
    first = now.date() - dt.timedelta(days=LOOKBACK_DAYS)
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
        missing = [k for k, p in pages.items() if not p.is_file()]
        rows.append(_row(agent, "Analytics", "WARN" if missing else "OK",
                         f"summary page missing for the last report day: {', '.join(missing)}" if missing
                         else "daily, weekly and monthly pages exist for the last report day", day=d.isoformat()))

    # Coordinator: control.json in force (seq, expiry, hours left, 48 h refresh / 72 h staleness) + latest reports/coord
    raw = _load(root / control.CONTROL_PATH)
    norm, errors, _ = control.normalize(raw, now) if isinstance(raw, dict) else (None, ["missing or unreadable"], [])
    eff = control.effective(norm, now)
    coord_days = sorted(p.stem for p in (root / "reports" / "coord").glob("*.md") if re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem))
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
    hits = CHANGELOG_RE.findall(log.read_text(encoding="utf-8")) if log.is_file() else []
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
            "rows": rows, "kpis": {a or "-": kpis(all_reps.get(a, []), now.date()) for a in accts}}


def stable(res: dict) -> str:
    """What decides a rewrite: statuses, reasons and KPIs (ages and the clock are left out)."""
    return json.dumps({"day": res.get("day"), "overall": res.get("overall"), "kpis": res.get("kpis"),
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
    return "\n".join(lines)


def write(root: Path, res: dict) -> bool:
    out = root / OUT_DIR
    js = out / f"{res['day']}.json"
    prev = _load(js)
    if isinstance(prev, dict) and stable(prev) == stable(res) and (out / f"{res['day']}.md").is_file():
        return False
    write_text_atomic(js, dumps_pretty(res))
    write_text_atomic(out / f"{res['day']}.md", render(res))
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
        Path(args.msg_file).write_text(msg + "\n", encoding="utf-8")
    print(("wrote " if changed else "unchanged ") + f"{OUT_DIR.as_posix()}/{res['day']}.md: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
