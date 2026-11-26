#!/usr/bin/env python3
"""Fetch FRED (+ optional gold) data, update data/history/*.csv and write data/latest.json.

latest.json is only rewritten when its content (everything except the generation
timestamps) changed, so the workflow commits only real changes. The 'day' field
(UTC date of the run) changes once per day, which acts as a daily heartbeat that
lets the EA detect a dead pipeline (see 'stale_after_h').

Causality ("as of D-1"): every value is the last observation dated strictly before
'day'. FRED posts most daily series on the next US business day (H.15 yields
around 16:15 ET), so in practice the newest value is often D-2; 'age' tells you.

Usage: python scripts/build_latest.py [--root DIR] [--offline] [--now ISO] [--msg-file PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import fred  # noqa: E402
import gold  # noqa: E402
import quality  # noqa: E402
from zumi_common import (dumps_pretty, iso_z, load_config, mask_secrets, parse_utc, repo_root,  # noqa: E402
                         set_github_output, utc_now, write_text_atomic)

SCHEMA = "zumi.latest"
VERSION = 1
VOLATILE_KEYS = ("generated_utc", "generated_unix")


def _start_for(old_rows: list, cfg_block: dict, asof: dt.date) -> str:
    if not old_rows:
        return cfg_block.get("start", "2018-01-01")
    return (asof - dt.timedelta(days=int(cfg_block.get("refetch_days", 60)))).isoformat()


def stable_part(obj: dict | None) -> dict | None:
    if obj is None:
        return None
    return {k: v for k, v in obj.items() if k not in VOLATILE_KEYS}


def flags_for(series: dict, acfg: dict) -> dict:
    vix = series.get("VIXCLS") or {}
    v, c1, c1p = vix.get("v"), vix.get("c1"), vix.get("c1p")
    jump = int(bool(c1 is not None and c1p is not None and
                    (c1p >= acfg.get("vix_jump_pct", 15) or c1 >= acfg.get("vix_jump_abs", 3))))
    high = int(bool(v is not None and v >= acfg.get("vix_level", 30)))
    return {"vix_high": high, "vix_jump": jump}


def change_summary(old: dict | None, new: dict) -> str:
    """One-line description of what moved, used as the commit message."""
    parts = []
    old_series = (old or {}).get("series", {})
    for sid, s in new.get("series", {}).items():
        o = old_series.get(sid)
        if not o or o.get("d") != s.get("d") or o.get("v") != s.get("v"):
            prev = o.get("v") if o else None
            parts.append(f"{sid} {prev}->{s['v']} ({s['d']})" if prev is not None else f"{sid} {s['v']} ({s['d']})")
    og, ng = (old or {}).get("gold"), new.get("gold")
    if ng and (not og or og.get("d") != ng.get("d")):
        parts.append(f"XAUUSD {ng.get('c')} ({ng.get('d')})")
    if (old or {}).get("events") != new.get("events"):
        parts.append("calendar")
    if not parts and (old or {}).get("day") != new.get("day"):
        parts.append(f"heartbeat {new['day']}")
    if not parts:
        return "data: no change"
    msg = "data: " + "; ".join(parts[:8])
    if len(parts) > 8:
        msg += f"; +{len(parts) - 8} more"
    return msg


def build(root: Path, now: dt.datetime, session=None, offline: bool = False) -> dict:
    """Returns {'latest': dict|None, 'issues': [...], 'written': bool, 'message': str}."""
    cfg = load_config(root)
    asof = now.date()
    hist_dir = root / "data" / "history"
    qcfg = cfg.get("quality", {})
    issues: list[dict] = []
    series_out: dict = {}

    for sid in cfg["fred"]["series"]:
        path = hist_dir / f"{sid}.csv"
        old = fred.read_history(path)
        rows = old
        if not offline:
            try:
                text = fred.fetch_series_csv(sid, _start_for(old, cfg["fred"], asof), session,
                                             cfg["fred"].get("timeout_s", 30))
                rows = fred.merge_rows(old, fred.parse_fred_csv(text, sid))
            except Exception as exc:  # network or format problem: keep history
                issues.append({"sid": sid, "kind": "fetch", "level": "warn", "msg": f"{sid}: fetch failed: {exc}"[:300]})
        summ = fred.summarize(rows, asof)
        s_issues = quality.check_series(sid, summ, qcfg)
        issues.extend(s_issues)
        if quality.has_errors(s_issues):
            continue  # do not persist suspicious rows
        if rows is not old:
            fred.write_history_if_changed(path, rows)
        if summ:
            series_out[sid] = summ

    gold_out = None
    gcfg = cfg.get("gold", {})
    if gcfg.get("enabled"):
        gpath = hist_dir / "XAUUSD.csv"
        old_bars = gold.read_bars(gpath)
        bars = old_bars
        if not offline:
            try:
                text = gold.fetch_stooq_csv(gcfg.get("symbol", "xauusd"), _start_for(old_bars, gcfg, asof),
                                            session, gcfg.get("timeout_s", 20))
                bars = gold.merge_bars(old_bars, gold.parse_ohlc_csv(text))
            except Exception as exc:
                issues.append({"sid": "XAUUSD", "kind": "fetch", "level": "warn", "msg": f"XAUUSD: fetch failed: {exc}"[:300]})
        gsum = gold.summarize(bars, asof)
        if gsum:
            g_issues = quality.check_series("XAUUSD", {"v": gsum["c"], "c1p": gsum["c1p"], "age": gsum["age"], "d": gsum["d"]}, qcfg)
            issues.extend(g_issues)
            if not quality.has_errors(g_issues):
                if bars is not old_bars:
                    gold.write_bars_if_changed(gpath, bars)
                gold_out = gsum

    ccfg = cfg.get("calendar", {})
    events = calendar_events.load_events(root / ccfg.get("file", "data/calendar/events_us.csv"))
    cal = calendar_events.calendar_block(events, now, ccfg.get("next_n", 6), ccfg.get("recent_h", 24))
    if not cal["next"]:
        issues.append({"sid": "calendar", "kind": "stale", "level": "warn",
                       "msg": "events_us.csv has no upcoming events: extend the calendar"})

    latest = {
        "schema": SCHEMA,
        "v": VERSION,
        "day": asof.isoformat(),
        "generated_utc": iso_z(now),
        "generated_unix": int(now.timestamp()),
        "asof": "D-1",
        "stale_after_h": cfg.get("latest", {}).get("stale_after_h", 36),
        "series": series_out,
        "events": cal,
        "flags": flags_for(series_out, cfg.get("alerts", {})),
    }
    if gold_out:
        latest["gold"] = gold_out
    issues.extend(quality.check_schema(latest))
    if len(series_out) < qcfg.get("min_series_ok", 1):
        issues.append({"sid": "*", "kind": "coverage", "level": "error",
                       "msg": f"only {len(series_out)} series usable (< {qcfg.get('min_series_ok')})"})

    latest_path = root / "data" / "latest.json"
    old_latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else None
    ok = not quality.has_errors(issues)
    written = False
    message = "data: quality guard tripped, latest.json kept"
    if ok:
        message = change_summary(old_latest, latest)
        if stable_part(old_latest) != stable_part(latest):
            write_text_atomic(latest_path, dumps_pretty(latest))
            written = True
    write_quality(root, now, ok, issues)
    return {"latest": latest if ok else None, "issues": issues, "written": written, "message": message, "ok": ok}


def write_quality(root: Path, now: dt.datetime, ok: bool, issues: list[dict]) -> bool:
    """data/quality.json changes only when the set of issues changes (no per-run churn)."""
    path = root / "data" / "quality.json"
    body = {"ok": ok, "issues": sorted(issues, key=lambda i: (i["level"], i["sid"], i["kind"]))}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if {"ok": old.get("ok"), "issues": old.get("issues")} == body:
            return False
    body["since_utc"] = iso_z(now)
    write_text_atomic(path, dumps_pretty(body))
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root")
    ap.add_argument("--offline", action="store_true", help="no network: rebuild from data/history only")
    ap.add_argument("--now", help="override current UTC time (ISO), for tests/backfills")
    ap.add_argument("--msg-file", help="write the commit message here")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    session = None
    if not args.offline:
        import requests  # imported lazily so offline runs need only the stdlib
        session = requests.Session()
    res = build(root, now, session=session, offline=args.offline)
    for i in res["issues"]:
        print(f"[{i['level']}] {i['msg']}")
    print(("written: " if res["written"] else "unchanged: ") + res["message"])
    if args.msg_file:
        Path(args.msg_file).write_text(mask_secrets(res["message"]) + "\n", encoding="utf-8")
    set_github_output("changed", "true" if res["written"] else "false")
    set_github_output("quality_ok", "true" if res["ok"] else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
