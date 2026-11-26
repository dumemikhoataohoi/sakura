#!/usr/bin/env python3
"""Render journal analytics: reports/daily/YYYY-MM-DD.md, reports/weekly/YYYY-Www.md,
reports/monthly/YYYY-MM.md and the dashboard data docs/dashboard/data.json.

Output is deterministic (no generation timestamp), so files change only when the
underlying trades / logs change.

Usage:
  python scripts/daily_summary.py --dates 2026-11-25 2026-11-26
  python scripts/daily_summary.py --dates-file touched.txt
  python scripts/daily_summary.py --recent 3          # today and the 2 previous UTC days
  python scripts/daily_summary.py --all               # every day present in the journal
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import journal_stats as js  # noqa: E402
import reports_lib as rl  # noqa: E402
from zumi_common import UTC, dumps_pretty, load_config, repo_root, utc_now, write_text_atomic  # noqa: E402


# ---------------------------------------------------------------- formatting
def f(v, nd: int = 2, sign: bool = False) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def hhmm(ts) -> str:
    return dt.datetime.fromtimestamp(int(ts), UTC).strftime("%H:%M") if ts else "-"


def md_escape(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ")


def metrics_table(m: dict) -> list[str]:
    return [
        "| trades | W / L / BE | win% | PF | sum R | exp R | sum % | exp % | avg win R | avg loss R | max L-streak | L-streak at end |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        f"| {m['n']} | {m['wins']} / {m['losses']} / {m['be']} | {f(m['win_pct'], 1)} | {f(m['pf'])} | "
        f"{f(m['sum_r'], 2, True)} | {f(m['exp_r'], 3, True)} | {f(m['sum_pct'], 3, True)} | {f(m['exp_pct'], 3, True)} | "
        f"{f(m['avg_win_r'])} | {f(m['avg_loss_r'])} | {m['max_loss_streak']} | {m['end_loss_streak']} |",
    ]


def group_table(title: str, groups) -> list[str]:
    if not groups or (len(groups) == 1 and "(none)" in groups):
        return []
    out = [f"#### {title}", "", "| key | n | win% | PF | sum R | exp R | sum % |", "|---|---|---|---|---|---|---|"]
    for k, m in groups.items():
        out.append(f"| {md_escape(str(k))} | {m['n']} | {f(m['win_pct'], 1)} | {f(m['pf'])} | "
                   f"{f(m['sum_r'], 2, True)} | {f(m['exp_r'], 3, True)} | {f(m['sum_pct'], 3, True)} |")
    return out + [""]


def heatmap_table(cells: dict) -> list[str]:
    if not cells:
        return []
    hours = sorted({h for _, h in cells})
    out = ["#### Weekday x hour (UTC entry): n / sum R", "",
           "| day | " + " | ".join(f"{h:02d}" for h in hours) + " |",
           "|---|" + "---|" * len(hours)]
    for wd in sorted({w for w, _ in cells}):
        row = []
        for h in hours:
            c = cells.get((wd, h))
            row.append(f"{c['n']} / {c['sum_r']:+.1f}" if c else "")
        out.append(f"| {js.WEEKDAYS[wd]} | " + " | ".join(row) + " |")
    return out + [""]


def trades_table(positions: list[dict]) -> list[str]:
    if not positions:
        return []
    out = ["#### Trades", "",
           "| close UTC | pos | sym | side | vol | entry | exit | SL | TP | R | P/L % | risk % | MAE R | MFE R | method | magic | phase | exit |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in positions:
        out.append(
            f"| {hhmm(p['t_out'])} | {p['pos']} | {md_escape(p['sym'])} | {p['side']} | {f(p['vol_in'])} | "
            f"{f(p['entry'], 2)} | {f(p['exit'], 2)} | {f(p['sl'], 2)} | {f(p['tp'], 2)} | {f(p['r'], 2, True)} | "
            f"{f(p['pl_pct'], 3, True)} | {f(p['risk_pct'])} | {f(p['mae_r'])} | {f(p['mfe_r'])} | "
            f"{md_escape(p['method'])} | {p['magic'] if p['magic'] is not None else '-'} | {md_escape(p['phase'])} | "
            f"{md_escape(p['reason'])} |")
    return out + [""]


def dd_table(eps: list[dict]) -> list[str]:
    if not eps:
        return []
    out = ["#### Drawdown episodes (closed P/L %, additive)", "", "| from | trough | recovered | depth % |",
           "|---|---|---|---|"]
    for e in eps:
        out.append(f"| {rl._iso(e['start']) or 'start'} | {rl._iso(e['trough'])} | {rl._iso(e['end']) or 'open'} | {f(e['depth_pct'], 2)} |")
    return out + [""]


def flags_list(flags: list[dict]) -> list[str]:
    if not flags:
        return []
    return ["#### Rule breaks", ""] + [f"- **{fl['rule']}**: {fl['en']} / {fl['vi']}" for fl in flags] + [""]


# ---------------------------------------------------------------- data access
def closed_positions(root: Path) -> dict[str, list[dict]]:
    out = {}
    for acct in rl.list_accounts(root):
        pos = rl.build_positions(rl.read_deals(root, acct))
        rl.write_positions_csv(root, acct, pos)
        out[acct] = [p for p in pos if p["closed"] and p.get("t_out")]
    return out


def day_extras(root: Path, day: dt.date, rcfg: dict) -> dict[str, dict]:
    """Phase/analysis log lines, EA flags and the last equity/market snapshot of ``day`` per account."""
    first = day - dt.timedelta(days=rcfg.get("summary_window_before_d", 1))
    last = day + dt.timedelta(days=rcfg.get("summary_window_after_d", 3))
    out: dict[str, dict] = {}
    for rep in rl.load_reports(root, first, last):
        acct = rep.get("acct", "unknown")
        x = out.setdefault(acct, {"log": {}, "flags": {}, "eq": None, "mkt": None, "eq_ts": 0, "phase": None})
        data = rep.get("data", {})
        for item in data.get("log", []):
            if dt.datetime.fromtimestamp(item["t"], UTC).date() == day:
                x["log"][(item["t"], item["m"])] = item
        for item in data.get("flags", []):
            if dt.datetime.fromtimestamp(item["t"], UTC).date() == day:
                x["flags"][(item["key"], item["msg"])] = item
        if dt.datetime.fromtimestamp(rep["ts"], UTC).date() == day and rep["ts"] >= x["eq_ts"]:
            if data.get("eq"):
                x["eq"], x["eq_ts"] = data["eq"], rep["ts"]
            if data.get("mkt"):
                x["mkt"] = data["mkt"]
            if data.get("phase"):
                x["phase"] = data["phase"]
    return out


# ---------------------------------------------------------------- pages
def account_section(acct: str, positions: list[dict], cfg: dict, events: list[dict], kind: str,
                    extra: dict | None = None, history: list[dict] | None = None) -> list[str]:
    out = [f"## Account `{acct}`", ""]
    m = js.metrics(positions)
    out += metrics_table(m) + [""]
    if history is not None and positions:
        _, cur = js.streaks(history)
        out += [f"Losing streak across the journal at the end of this period: **{cur}**", ""]
    if extra and extra.get("eq"):
        eq = extra["eq"]
        out += ["| EA day P/L % | equity vs day start % | floating % | margin level % |", "|---|---|---|---|",
                f"| {f(eq.get('day_pl_pct'), 3, True)} | {f(eq.get('eq_day_pct'), 3, True)} | "
                f"{f(eq.get('float_pct'), 3, True)} | {f(eq.get('margin_level'), 0)} |", ""]
    if extra and extra.get("mkt"):
        mk = extra["mkt"]
        out += [f"Market at last report: {md_escape(mk.get('sym', ''))} bid {f(mk.get('bid'))}, "
                f"move vs prev close {f(mk.get('move'), 2, True)} = {f(mk.get('move_atr'), 2, True)} x ATR14(D1) "
                f"(ATR {f(mk.get('atr'))})", ""]
    out += group_table("By method (EA comment)", js.group_metrics(positions, lambda p: p["method"]))
    out += group_table("By phase", js.group_metrics(positions, lambda p: p["phase"]))
    if len({p["magic"] for p in positions}) > 1:
        out += group_table("By magic", js.group_metrics(positions, lambda p: str(p["magic"])))
    if kind != "daily":
        out += heatmap_table(js.heatmap(positions))
    out += dd_table(js.drawdown_episodes(positions, cfg.get("rules", {}).get("dd_episode_min_pct", 1.0)))
    out += flags_list(js.rule_breaks(positions, cfg.get("rules", {}), events))
    if kind == "daily":
        out += trades_table(positions)
        if extra and extra.get("flags"):
            out += ["#### EA flags", ""]
            out += [f"- {hhmm(fl['t'])} `{fl['key']}` {md_escape(fl['msg'])}" for fl in sorted(extra["flags"].values(), key=lambda x: x["t"])]
            out += [""]
        if extra and extra.get("log"):
            out += ["#### Phase / analysis log", ""]
            out += [f"- {hhmm(item['t'])} {md_escape(item['m'])}" for item in sorted(extra["log"].values(), key=lambda x: x["t"])]
            out += [""]
    return out


def render_page(title: str, kind: str, per_acct: dict[str, list[dict]], cfg: dict, events: list[dict],
                extras: dict[str, dict] | None = None, history: dict[str, list[dict]] | None = None) -> str:
    accts = sorted(set(per_acct) | set(extras or {}))
    lines = [f"# {title}", "",
             "Money is shown as % of balance only. R uses the stop-loss at entry. Times are UTC.", ""]
    if not accts:
        lines += ["No trades or reports for this period.", ""]
    for acct in accts:
        lines += account_section(acct, per_acct.get(acct, []), cfg, events, kind,
                                 (extras or {}).get(acct), (history or {}).get(acct))
    return "\n".join(lines).rstrip() + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    write_text_atomic(path, text)
    return True


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def summarize(root: Path, dates: list[dt.date], cfg: dict | None = None) -> list[Path]:
    cfg = cfg or load_config(root)
    rcfg = cfg.get("reports", {})
    events = calendar_events.load_events(root / cfg.get("calendar", {}).get("file", "data/calendar/events_us.csv"))
    allpos = closed_positions(root)
    written: list[Path] = []

    for day in sorted(set(dates)):
        per = {a: [p for p in ps if js.close_date(p) == day] for a, ps in allpos.items()}
        per = {a: ps for a, ps in per.items() if ps}
        hist = {a: [p for p in ps if js.close_date(p) <= day] for a, ps in allpos.items()}
        extras = day_extras(root, day, rcfg)
        text = render_page(f"Zumi daily report {day.isoformat()} (UTC)", "daily", per, cfg, events, extras, hist)
        path = root / "reports" / "daily" / f"{day.isoformat()}.md"
        if write_if_changed(path, text):
            written.append(path)

    for wk in sorted({iso_week(d) for d in dates}):
        per = {a: [p for p in ps if iso_week(js.close_date(p)) == wk] for a, ps in allpos.items()}
        per = {a: ps for a, ps in per.items() if ps}
        text = render_page(f"Zumi weekly report {wk} (UTC)", "weekly", per, cfg, events)
        path = root / "reports" / "weekly" / f"{wk}.md"
        if write_if_changed(path, text):
            written.append(path)

    for mo in sorted({d.strftime("%Y-%m") for d in dates}):
        per = {a: [p for p in ps if js.close_date(p).strftime("%Y-%m") == mo] for a, ps in allpos.items()}
        per = {a: ps for a, ps in per.items() if ps}
        text = render_page(f"Zumi monthly report {mo} (UTC)", "monthly", per, cfg, events)
        path = root / "reports" / "monthly" / f"{mo}.md"
        if write_if_changed(path, text):
            written.append(path)

    if write_dashboard_data(root, allpos, cfg):
        written.append(root / "docs" / "dashboard" / "data.json")
    return written


def _clean_num(v):
    return None if isinstance(v, float) and (math.isinf(v) or math.isnan(v)) else v


def write_dashboard_data(root: Path, allpos: dict[str, list[dict]], cfg: dict) -> bool:
    """Static dashboard data (percent and R only, account hashes only)."""
    limit = int(cfg.get("reports", {}).get("dashboard_max_positions", 2000))
    accounts = {}
    for acct, ps in allpos.items():
        ps = ps[-limit:]
        daily: dict[str, dict] = {}
        for p in ps:
            d = js.close_date(p).isoformat()
            x = daily.setdefault(d, {"n": 0, "pl_pct": 0.0, "r": 0.0})
            x["n"] += 1
            x["pl_pct"] = round(x["pl_pct"] + (p.get("pl_pct") or 0.0), 4)
            x["r"] = round(x["r"] + (p.get("r") or 0.0), 3)
        accounts[acct] = {
            "metrics": {k: _clean_num(v) for k, v in js.metrics(ps).items()},
            "by_method": {k: {kk: _clean_num(vv) for kk, vv in m.items()} for k, m in js.group_metrics(ps, lambda p: p["method"]).items()},
            "by_phase": {k: {kk: _clean_num(vv) for kk, vv in m.items()} for k, m in js.group_metrics(ps, lambda p: p["phase"]).items()},
            "daily": daily,
            "positions": [{"t": p["t_out"], "r": p["r"], "pl": p["pl_pct"], "m": p["method"], "ph": p["phase"],
                           "s": p["side"]} for p in ps],
        }
    text = dumps_pretty({"schema": "zumi.dashboard", "v": 1, "accounts": accounts})
    return write_if_changed(root / "docs" / "dashboard" / "data.json", text)


def all_dates(root: Path) -> list[dt.date]:
    dates = set()
    for ps in closed_positions(root).values():
        dates.update(js.close_date(p) for p in ps)
    return sorted(dates)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render daily/weekly/monthly journal pages")
    ap.add_argument("--root")
    ap.add_argument("--dates", nargs="*", default=[])
    ap.add_argument("--dates-file")
    ap.add_argument("--recent", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    dates = [dt.date.fromisoformat(d) for d in args.dates]
    if args.dates_file and Path(args.dates_file).exists():
        dates += [dt.date.fromisoformat(x) for x in Path(args.dates_file).read_text().split() if x.strip()]
    today = utc_now().date()
    dates += [today - dt.timedelta(days=i) for i in range(args.recent)]
    if args.all:
        dates += all_dates(root)
    if not dates:
        dates = [today]
    for p in summarize(root, dates):
        print(f"wrote {p.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
