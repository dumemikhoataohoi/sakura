#!/usr/bin/env python3
"""Detect big moves / risk events and comment on the open PR labelled 'zumi-alerts'.

Claude subscribes to that PR, so each comment is a notification. Alerts are
de-duplicated with hidden markers (<!-- zumi-alert-key: ... -->) in earlier
comments, so the PR itself is the state store and nothing is committed.

Inputs: data/latest.json, data/quality.json, reports/ (recent raw reports and the
deal journal), config/settings.json. Env: GITHUB_TOKEN, GITHUB_REPOSITORY,
DEFAULT_BRANCH, WF_NAME / WF_CONCLUSION (from the workflow_run event).

Usage: python scripts/alerts.py [--dry-run] [--now ISO]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import journal_stats as js  # noqa: E402
import reports_lib as rl  # noqa: E402
from zumi_common import UTC, iso_z, load_config, mask_secrets, parse_utc, repo_root, utc_now  # noqa: E402

KEY_RE = re.compile(r"<!-- zumi-alert-key: (\S+) -->")
MAX_COMMENT = 60000


def alert(key: str, level: str, vi: str, en: str) -> dict:
    return {"key": key, "level": level, "vi": mask_secrets(vi), "en": mask_secrets(en)}


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def data_alerts(root: Path, cfg: dict, now: dt.datetime) -> list[dict]:
    a = cfg["alerts"]
    out: list[dict] = []
    q = _load_json(root / "data" / "quality.json")
    if q:
        errs = [i for i in q.get("issues", []) if i["level"] == "error"]
        if errs:
            h = hashlib.sha1(json.dumps(errs, sort_keys=True).encode()).hexdigest()[:8]
            msg = "; ".join(i["msg"] for i in errs)[:600]
            out.append(alert(f"quality:{now.date()}:{h}", "high",
                             f"Kiểm tra chất lượng dữ liệu thất bại, latest.json KHÔNG được cập nhật: {msg}",
                             f"Data-quality guard tripped, latest.json was NOT updated: {msg}"))
        for i in q.get("issues", []):
            if i["level"] == "warn" and i["kind"] == "stale":
                out.append(alert(f"stale_series:{i['sid']}:{now.date()}", "info",
                                 f"Dữ liệu cũ: {i['msg']}", f"Stale data: {i['msg']}"))
    latest = _load_json(root / "data" / "latest.json")
    if not latest:
        out.append(alert(f"no_latest:{now.date()}", "high", "Không đọc được data/latest.json",
                         "data/latest.json is missing or unreadable"))
        return out
    age_h = (now.timestamp() - latest.get("generated_unix", 0)) / 3600
    if age_h > a["data_stale_h"]:
        out.append(alert(f"stale:{now.date()}", "high",
                         f"latest.json đã {age_h:.0f} giờ không cập nhật (workflow data có thể hỏng)",
                         f"latest.json has not been updated for {age_h:.0f} h (data workflow may be broken)"))
    s = latest.get("series", {})
    vix = s.get("VIXCLS")
    if vix and vix.get("c1") is not None:
        if vix.get("c1p", 0) >= a["vix_jump_pct"] or vix["c1"] >= a["vix_jump_abs"]:
            out.append(alert(f"vix_jump:{vix['d']}", "high",
                             f"VIX tăng vọt {vix['p']} -> {vix['v']} ({vix['c1p']:+.1f}%) ngày {vix['d']}",
                             f"VIX jump {vix['p']} -> {vix['v']} ({vix['c1p']:+.1f}%) on {vix['d']}"))
    if vix and vix.get("v", 0) >= a["vix_level"] and (vix.get("p") or 0) < a["vix_level"]:
        out.append(alert(f"vix_level:{vix['d']}", "high",
                         f"VIX vượt ngưỡng {a['vix_level']}: {vix['v']} ({vix['d']})",
                         f"VIX crossed {a['vix_level']}: {vix['v']} ({vix['d']})"))
    ry = s.get("DFII10")
    if ry and ry.get("c1") is not None and abs(ry["c1"]) * 100 >= a["real_yield_jump_bp"]:
        out.append(alert(f"real_yield:{ry['d']}", "medium",
                         f"Lợi suất thực 10 năm (DFII10) đổi {ry['c1'] * 100:+.0f} bp -> {ry['v']}% ({ry['d']})",
                         f"10y real yield (DFII10) moved {ry['c1'] * 100:+.0f} bp to {ry['v']}% ({ry['d']})"))
    usd = s.get("DTWEXBGS")
    if usd and usd.get("c1p") is not None and abs(usd["c1p"]) >= a["usd_jump_pct"]:
        out.append(alert(f"usd:{usd['d']}", "medium",
                         f"USD broad index đổi {usd['c1p']:+.2f}% ({usd['d']})",
                         f"Broad USD index moved {usd['c1p']:+.2f}% ({usd['d']})"))
    g = latest.get("gold")
    if g and g.get("move_atr") is not None and abs(g["move_atr"]) >= a["gold_move_atr_k"]:
        out.append(alert(f"gold:stooq:{g['d']}", "high",
                         f"Vàng biến động mạnh ngày {g['d']}: {g['c1']:+.2f} = {g['move_atr']:+.2f} x ATR14",
                         f"Big gold daily move on {g['d']}: {g['c1']:+.2f} = {g['move_atr']:+.2f} x ATR14"))
    horizon = now + dt.timedelta(hours=a["event_lookahead_h"])
    for e in latest.get("events", {}).get("next", []):
        t = parse_utc(e["utc"])
        if e["ev"] in a["event_types"] and now < t <= horizon:
            unc = " (lịch chưa xác nhận)" if e.get("unc") else ""
            unc_en = " (date not confirmed)" if e.get("unc") else ""
            out.append(alert(f"event:{e['ev']}:{e['utc']}", "info",
                             f"Sự kiện rủi ro sắp tới: {e['ev']} lúc {e['utc']}{unc}",
                             f"Upcoming risk event: {e['ev']} at {e['utc']}{unc_en}"))
    return out


def agg_alerts(root: Path, cfg: dict, now: dt.datetime) -> list[dict]:
    """Alerts from aggregate (v2) reports: daily loss, losing streak, drawdown, rule breaks,
    gold move vs ATR and EA flags. Uses the newest aggregate report of each account."""
    a = cfg["alerts"]
    out: list[dict] = []
    since = now.timestamp() - a["report_max_age_h"] * 3600
    reps = [r for r in rl.load_reports(root, now.date() - dt.timedelta(days=1), now.date() + dt.timedelta(days=1))
            if r.get("kind") == "ea_agg" and r.get("ts", 0) >= since]
    last: dict[str, dict] = {}
    for r in reps:
        last[r["acct"]] = r
        for fl in r["data"].get("flags", []):
            msg = fl.get("msg") or fl["key"]
            out.append(alert(f"eaflag:{r['acct']}:{fl['key']}:{r['data']['day']}", "medium",
                             f"EA `{r['acct']}` báo: {msg}", f"EA `{r['acct']}` flagged: {msg}"))
    for acct, r in last.items():
        d = r["data"]
        day = d["day"]
        per = d.get("periods", {})
        pday, pweek, pmtd = per.get("day") or {}, per.get("week") or {}, per.get("mtd") or {}
        eq = d.get("eq") or {}
        vals = [v for v in (eq.get("day_pl_pct"), eq.get("eq_day_pct"), pday.get("net_pct")) if v is not None]
        if vals and min(vals) <= -a["ea_daily_loss_pct"]:
            out.append(alert(f"dayloss:{acct}:{day}", "high",
                             f"EA `{acct}` lỗ trong ngày {min(vals):+.2f}% (ngưỡng -{a['ea_daily_loss_pct']}%)",
                             f"EA `{acct}` daily loss {min(vals):+.2f}% (threshold -{a['ea_daily_loss_pct']}%)"))
        streak = max(pday.get("cur_ls", 0) or 0, pweek.get("cur_ls", 0) or 0, pmtd.get("cur_ls", 0) or 0)
        if streak >= a["losing_streak_n"]:
            out.append(alert(f"streak:{acct}:{day}:{streak}", "high",
                             f"EA `{acct}` đang thua {streak} lệnh liên tiếp (ngưỡng {a['losing_streak_n']})",
                             f"EA `{acct}` is on a losing streak of {streak} trades (threshold {a['losing_streak_n']})"))
        dd = max(pweek.get("dd_pct") or 0.0, pmtd.get("dd_pct") or 0.0)
        if dd >= a.get("dd_alert_pct", 5.0):
            out.append(alert(f"dd:{acct}:{day}", "high",
                             f"EA `{acct}` drawdown {dd:.2f}% số dư (ngưỡng {a.get('dd_alert_pct', 5.0)}%)",
                             f"EA `{acct}` drawdown {dd:.2f}% of balance (threshold {a.get('dd_alert_pct', 5.0)}%)"))
        for rule, count in sorted((pday.get("rule") or {}).items()):
            if count:
                out.append(alert(f"rule:{acct}:{day}:{rule}:{count}", "medium",
                                 f"`{acct}` vi phạm quy tắc `{rule}`: {count} lần hôm nay",
                                 f"`{acct}` rule break `{rule}`: {count} time(s) today"))
        mva = (d.get("mkt") or {}).get("move_atr")
        if mva is not None and abs(mva) >= a["gold_move_atr_k"]:
            sym = (d.get("mkt") or {}).get("sym", "XAU")
            out.append(alert(f"gold:{acct}:{day}", "high",
                             f"{sym} biến động {mva:+.2f} x ATR14(D1) so với giá đóng cửa hôm trước",
                             f"{sym} moved {mva:+.2f} x ATR14(D1) vs previous close"))
    return out


def ea_alerts(root: Path, cfg: dict, now: dt.datetime) -> list[dict]:
    """Alerts from per-trade (v1) reports and the deal journal: private detail mode only."""
    a = cfg["alerts"]
    out: list[dict] = []
    since = now.timestamp() - a["report_max_age_h"] * 3600
    reps = [r for r in rl.load_reports(root, now.date() - dt.timedelta(days=1), now.date())
            if r.get("ts", 0) >= since and r.get("kind") != "ea_agg"]
    last: dict[str, dict] = {}
    for r in reps:
        last[r["acct"]] = r
        for fl in r["data"].get("flags", []):
            out.append(alert(f"eaflag:{r['acct']}:{fl['key']}:{fl['t']}", "medium",
                             f"EA `{r['acct']}` báo: {fl['msg']}", f"EA `{r['acct']}` flagged: {fl['msg']}"))
    for acct, r in last.items():
        day = dt.datetime.fromtimestamp(r["ts"], UTC).date()
        mkt, eq = r["data"].get("mkt") or {}, r["data"].get("eq") or {}
        if mkt.get("move_atr") is not None and abs(mkt["move_atr"]) >= a["gold_move_atr_k"]:
            out.append(alert(f"gold:{acct}:{day}", "high",
                             f"{mkt.get('sym', 'XAU')} biến động {mkt.get('move', 0):+.2f} = {mkt['move_atr']:+.2f} x ATR14(D1) so với giá đóng cửa hôm trước",
                             f"{mkt.get('sym', 'XAU')} moved {mkt.get('move', 0):+.2f} = {mkt['move_atr']:+.2f} x ATR14(D1) vs previous close"))
        vals = [v for v in (eq.get("day_pl_pct"), eq.get("eq_day_pct")) if v is not None]
        if vals and min(vals) <= -a["ea_daily_loss_pct"]:
            out.append(alert(f"dayloss:{acct}:{day}", "high",
                             f"EA `{acct}` lỗ trong ngày {min(vals):+.2f}% (ngưỡng -{a['ea_daily_loss_pct']}%)",
                             f"EA `{acct}` daily loss {min(vals):+.2f}% (threshold -{a['ea_daily_loss_pct']}%)"))
    events = calendar_events.load_events(root / cfg["calendar"]["file"])
    recent = now.timestamp() - a["rule_break_lookback_h"] * 3600
    for acct in rl.list_accounts(root):
        pos = [p for p in rl.build_positions(rl.read_deals(root, acct)) if p["closed"] and p.get("t_out")]
        _, cur = js.streaks(pos)
        if cur >= a["losing_streak_n"] and pos[-1]["t_out"] >= recent:
            first = pos[-cur]["pos"]
            out.append(alert(f"streak:{acct}:{first}", "high",
                             f"EA `{acct}` thua {cur} lệnh liên tiếp (ngưỡng {a['losing_streak_n']})",
                             f"EA `{acct}` losing streak of {cur} trades (threshold {a['losing_streak_n']})"))
        recent_pos = [p for p in pos if p["t_out"] >= recent]
        for fl in js.rule_breaks(recent_pos, cfg.get("rules", {}), events):
            out.append(alert(f"rule:{acct}:{fl['key']}", "medium", f"`{acct}` vi phạm quy tắc: {fl['vi']}",
                             f"`{acct}` rule break: {fl['en']}"))
    return out


def workflow_alerts(now: dt.datetime) -> list[dict]:
    name, concl = os.environ.get("WF_NAME", ""), os.environ.get("WF_CONCLUSION", "")
    if name and concl and concl not in ("success", "skipped", "cancelled"):
        return [alert(f"wf_fail:{name}:{now.date()}", "high",
                      f"Workflow `{name}` kết thúc với trạng thái `{concl}`",
                      f"Workflow `{name}` finished with conclusion `{concl}`")]
    return []


def evaluate(root: Path, cfg: dict, now: dt.datetime) -> list[dict]:
    seen, out = set(), []
    for al in workflow_alerts(now) + data_alerts(root, cfg, now) + agg_alerts(root, cfg, now) + ea_alerts(root, cfg, now):
        if al["key"] not in seen:
            seen.add(al["key"])
            out.append(al)
    return out


def render_comment(alerts: list[dict], now: dt.datetime) -> str:
    order = {"high": 0, "medium": 1, "info": 2}
    lines = [f"**Cảnh báo Zumi / Zumi alert** - {now.strftime('%Y-%m-%d %H:%M')} UTC", ""]
    for al in sorted(alerts, key=lambda x: order.get(x["level"], 3)):
        lines.append(f"- **[{al['level'].upper()}]** {al['vi']}  \n  EN: {al['en']}")
    lines.append("")
    lines += [f"<!-- zumi-alert-key: {al['key']} -->" for al in alerts]
    body = "\n".join(lines)
    return body[:MAX_COMMENT]


def existing_keys(bodies: list[str]) -> set[str]:
    keys = set()
    for b in bodies:
        keys.update(KEY_RE.findall(b or ""))
    return keys


PR_BODY = """This pull request is a **notification channel**, not a code change. **Do not merge.**

Đây là PR dùng làm **kênh cảnh báo**, không phải thay đổi mã. **Không merge.**

`alerts.yml` comments here when it detects a big move or a risk event
(VIX jump, gold move > k x ATR, EA daily loss, losing streak, rule breaks, upcoming
NFP/CPI/FOMC, data-quality failures). Claude subscribes to this PR's activity.
"""


def ensure_pr(gh, cfg: dict, base: str) -> int:
    label, branch = cfg["alerts"]["label"], cfg["alerts"]["branch"]
    number = gh.find_open_pr_with_label(label)
    if number:
        return number
    gh.ensure_label(label)
    if not gh.branch_sha(branch):
        base_sha = gh.branch_sha(base)
        if not base_sha:
            raise RuntimeError(f"base branch {base!r} not found")
        gh.create_branch(branch, base_sha)
    # A marker commit guarantees the branch differs from base (a PR needs >= 1 commit).
    gh.put_file(branch, "alerts/README.md",
                f"# Zumi alerts channel\n\nMarker file for the long-lived alerts PR. Updated {iso_z(utc_now())}.\n",
                "alerts: marker commit for the alerts PR")
    number = gh.create_pr(branch, base, "Zumi alerts (do not merge)", PR_BODY)
    gh.add_labels(number, [label])
    return number


def post_alerts(gh, cfg: dict, alerts: list[dict], now: dt.datetime, base: str) -> tuple[int, int]:
    """Ensure the alerts PR exists, then post one comment with the alerts not posted before."""
    number = ensure_pr(gh, cfg, base)
    known = existing_keys(gh.comment_bodies(number))
    new = [a for a in alerts if a["key"] not in known]
    if new:
        gh.comment(number, render_comment(new, now))
    return number, len(new)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate alert rules and comment on the alerts PR")
    ap.add_argument("--root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    cfg = load_config(root)
    alerts = evaluate(root, cfg, now)
    print(f"{len(alerts)} alert(s) detected: {', '.join(a['key'] for a in alerts) or '-'}")
    dry = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"
    if not alerts:
        return 0
    if dry:
        print(render_comment(alerts, now))
        return 0
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("error: GITHUB_TOKEN and GITHUB_REPOSITORY are required (or use --dry-run)", file=sys.stderr)
        return 2
    from gh_api import GitHub, GitHubError
    gh = GitHub(token, repo)
    try:
        number, posted = post_alerts(gh, cfg, alerts, now, os.environ.get("DEFAULT_BRANCH", "main"))
        print(f"posted {posted} new alert(s) on PR #{number}" if posted else f"all alerts already posted on PR #{number}")
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.status == 403:
            print("hint: enable Settings > Actions > General > 'Allow GitHub Actions to create and approve "
                  "pull requests', or open the alerts PR by hand (see docs/workflows.md)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
