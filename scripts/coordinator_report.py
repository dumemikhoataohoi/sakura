#!/usr/bin/env python3
"""Daily coordination helper: suggest control.json changes from the aggregates, using ONLY the
pre-registered rules in config/settings.json > coordinator. Claude reads the suggestion and decides.

Inputs: the newest aggregate EA report per account (periods day / week / mtd / r30 with by_method,
by_phase, by_method_phase and flags), data/latest.json, data/quality.json, the event calendar and the
current control/control.json.

Rules (per method code and phase, from the rolling-30-day block, falling back to month to date):
  R01 pause   current losing streak of the code in that phase >= streak_pause_n
  R02 pause   last-20 win% < winrate_pause_pct with at least winrate_min_trades trades
  R07 boost   last-20 n >= boost_min_trades, win% >= boost_min_winpct, avg R >= boost_min_avg_r, no current streak
  R08 on      a code paused by control has recovered (last-20 win% >= recover_winpct, no current streak)
  R07 on      a code listed in enable_codes (pre-approved by the user) when the EA's current phase is listed
  R05 global  drawdown (week or month to date) >= dd_reduce_pct -> risk_mult = dd_reduce_mult
  R04 global  VIX high flag and vix_high_mult < 1 -> risk_mult = vix_high_mult (off by default)
  R03 block   NFP/CPI/FOMC within news_lookahead_h -> block [t - before, t + after]
Directives that are no longer triggered are kept until they expire, then dropped ("restored").
Pause wins over boost for the same code. Nothing is applied unless --apply is given.

Output: reports/coord/YYYY-MM-DD.md (+ .json). With --apply the proposal is written to
control/control.json (seq + 1, normalized, CHANGELOG line).

Usage: python scripts/coordinator_report.py [--now ISO] [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import control  # noqa: E402
import reports_lib as rl  # noqa: E402
from zumi_common import (dumps_pretty, iso_z, load_config, parse_utc, repo_root, utc_now,  # noqa: E402
                         write_text_atomic)


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_agg_reports(root: Path, now: dt.datetime, max_age_h: float) -> dict[str, dict]:
    since = now.timestamp() - max_age_h * 3600
    out: dict[str, dict] = {}
    for r in rl.load_reports(root, now.date() - dt.timedelta(days=2), now.date() + dt.timedelta(days=1)):
        if r.get("kind") == "ea_agg" and r.get("ts", 0) >= since:
            out[r["acct"]] = r
    return out


def _wr(g: dict) -> float | None:
    n = g.get("l20_n")
    return 100.0 * g.get("l20_wins", 0) / n if n else None


def evaluate(root: Path, now: dt.datetime, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config(root)
    c = cfg["coordinator"]
    reps = latest_agg_reports(root, now, c["report_max_age_h"])
    current, cur_errors, _ = control.normalize(_load(root / control.CONTROL_PATH) or {}, None)
    eff = control.effective(current, now)
    hits: list[dict] = []
    pause: dict[str, dict] = {}      # code -> {"phases": set, "reason": str}
    boost: dict[str, set] = {}
    enable: dict[str, dict] = {}
    dd_max = 0.0
    phases_now = set()
    for acct, r in sorted(reps.items()):
        per = r["data"]["periods"]
        if r["data"].get("phase"):
            phases_now.add(r["data"]["phase"])
        dd_max = max(dd_max, (per.get("week") or {}).get("dd_pct") or 0.0, (per.get("mtd") or {}).get("dd_pct") or 0.0)
        block = per.get("r30") or per.get("mtd") or {}
        for key, g in sorted((block.get("by_method_phase") or {}).items()):
            code, _, phase = key.partition(":")
            if code == "M000":
                continue
            wr, n20, cur = _wr(g), g.get("l20_n", 0), g.get("cur_ls", 0)
            avg_r = (g.get("net_r", 0.0) / g["n"]) if g.get("n") else 0.0
            stats = {"acct": acct, "code": code, "phase": phase, "n": g.get("n", 0), "l20_n": n20,
                     "l20_win_pct": round(wr, 1) if wr is not None else None, "cur_ls": cur, "avg_r": round(avg_r, 3)}
            if cur >= c["streak_pause_n"]:
                hits.append(dict(stats, rule="R01", action="pause"))
                pause.setdefault(code, {"phases": set(), "reason": "R01"})["phases"].add(phase)
            elif wr is not None and n20 >= c["winrate_min_trades"] and wr < c["winrate_pause_pct"]:
                hits.append(dict(stats, rule="R02", action="pause"))
                p = pause.setdefault(code, {"phases": set(), "reason": "R02"})
                p["phases"].add(phase)
            elif (wr is not None and n20 >= c["boost_min_trades"] and wr >= c["boost_min_winpct"]
                  and avg_r >= c["boost_min_avg_r"] and cur == 0):
                hits.append(dict(stats, rule="R07", action="boost"))
                boost.setdefault(code, set()).add(phase)
            d = eff["methods"].get(code)
            if (d and d["mode"] == "pause" and (not d["phases"] or phase in d["phases"]) and wr is not None
                    and wr >= c["recover_winpct"] and cur == 0):
                hits.append(dict(stats, rule="R08", action="on"))
                enable[code] = {"phases": set(d["phases"]), "reason": "R08"}
    for code, phases in sorted((c.get("enable_codes") or {}).items()):
        if control.METHOD_RE.match(code) and (not phases or phases_now & set(phases)):
            hits.append({"code": code, "phase": ",".join(sorted(phases_now)), "rule": "R07", "action": "on"})
            enable.setdefault(code, {"phases": set(phases or []), "reason": "R07"})

    # ---- proposal
    base = now.replace(minute=0, second=0, microsecond=0)
    exp = iso_z(base + dt.timedelta(hours=c["directive_hours"]))
    methods: dict[str, dict] = {}
    restored = []
    for code, d in (current or {}).get("methods", {}).items():   # keep untriggered directives until expiry
        if d["expires_unix"] > now.timestamp():
            methods[code] = {k: d[k] for k in ("mode", "risk_mult", "phases", "reason_code", "expires_utc")}
        else:
            restored.append(code)
    for code, e in enable.items():
        methods[code] = {"mode": "on", "risk_mult": 1.0, "phases": sorted(e["phases"]), "reason_code": e["reason"],
                         "expires_utc": exp}
    for code, phases in boost.items():
        if code not in pause:
            methods[code] = {"mode": "boost", "risk_mult": c["boost_mult"], "phases": sorted(phases),
                             "reason_code": "R07", "expires_utc": exp}
    for code, p in pause.items():   # pause wins
        methods[code] = {"mode": "pause", "risk_mult": 0.0, "phases": sorted(p["phases"]),
                         "reason_code": p["reason"], "expires_utc": exp}

    g = {"risk_mult": 1.0, "pause_all": False, "reason_code": "R00", "expires_utc": exp}
    latest = _load(root / "data" / "latest.json") or {}
    if dd_max >= c["dd_reduce_pct"]:
        g.update(risk_mult=c["dd_reduce_mult"], reason_code="R05")
        hits.append({"code": "*", "phase": "*", "rule": "R05", "action": f"global x{c['dd_reduce_mult']}",
                     "dd_pct": round(dd_max, 2)})
    elif (latest.get("flags") or {}).get("vix_high") and c["vix_high_mult"] < 1.0:
        g.update(risk_mult=c["vix_high_mult"], reason_code="R04")
        hits.append({"code": "*", "phase": "*", "rule": "R04", "action": f"global x{c['vix_high_mult']}"})

    windows = [{k: w[k] for k in ("kind", "start_utc", "end_utc", "reason_code")}
               for w in (current or {}).get("news_blocks", []) if w["end_unix"] > now.timestamp()]
    seen = {(w["kind"], w["start_utc"], w["end_utc"]) for w in windows}
    events = calendar_events.load_events(root / cfg["calendar"]["file"])
    horizon = now + dt.timedelta(hours=c["news_lookahead_h"])
    for e in events:
        if e["ev"] in c["news_events"] and now < e["t_utc"] <= horizon:
            w = {"kind": "block", "start_utc": iso_z(e["t_utc"] - dt.timedelta(minutes=c["news_before_min"])),
                 "end_utc": iso_z(e["t_utc"] + dt.timedelta(minutes=c["news_after_min"])), "reason_code": "R03"}
            if ("block", w["start_utc"], w["end_utc"]) not in seen:
                windows.append(w)
                seen.add(("block", w["start_utc"], w["end_utc"]))
    windows = [w for w in windows if parse_utc(w["end_utc"]) <= base + control.MAX_TTL]

    proposal_raw = {"v": control.VERSION, "author": "claude", "seq": ((current or {}).get("seq") or 0) + 1,
                    "updated_utc": iso_z(base), "global": g, "methods": methods, "news_blocks": windows,
                    "notes_vi": "Đề xuất tự động theo quy tắc đăng ký trước; Claude xem xét và quyết định."}
    proposal, errors, warnings = control.normalize(proposal_raw, now)
    ch = control.changes(current, proposal) if proposal else []
    ch = [x for x in ch if x != "note updated"]
    quality = _load(root / "data" / "quality.json") or {}
    return {"as_of": iso_z(base), "accounts": sorted(reps), "hits": hits, "restored": sorted(restored),
            "changes": ch, "proposal": proposal, "errors": errors + [f"current control.json invalid: {e}" for e in cur_errors],
            "warnings": warnings, "dd_max": round(dd_max, 2), "phases_now": sorted(phases_now),
            "quality_ok": quality.get("ok", True), "vix_high": (latest.get("flags") or {}).get("vix_high", 0),
            "current_effective": eff}


def render(res: dict, day: dt.date) -> str:
    lines = [f"# Coordinator suggestion {day.isoformat()}", "",
             "Machine-generated from pre-registered rules (`config/settings.json` > `coordinator`). "
             "Claude reviews and decides; nothing here is applied automatically.", "",
             f"* as of: {res['as_of']}; accounts: {', '.join(res['accounts']) or 'none (no recent aggregate report)'}",
             f"* EA phase(s) now: {', '.join(res['phases_now']) or '-'}; max drawdown (week/MTD): {res['dd_max']} %",
             f"* data quality ok: {res['quality_ok']}; VIX high flag: {res['vix_high']}",
             f"* current control: {res['current_effective']['reason']}", ""]
    if not res["quality_ok"]:
        lines += ["**R06**: data quality problem; treat macro context with care.", ""]
    lines += ["## Rule hits", ""]
    if res["hits"]:
        lines += ["| rule | action | code | phase | n | last-20 n | last-20 win% | streak | avg R |", "|---|---|---|---|---|---|---|---|---|"]
        for h in res["hits"]:
            lines.append(f"| {h['rule']} | {h['action']} | {h.get('code', '-')} | {h.get('phase', '-')} | {h.get('n', '-')} | "
                         f"{h.get('l20_n', '-')} | {h.get('l20_win_pct', '-')} | {h.get('cur_ls', '-')} | {h.get('avg_r', '-')} |")
    else:
        lines.append("No rule fired.")
    lines += ["", "## Proposed changes", ""]
    lines += [f"- {c}" for c in res["changes"]] or ["- none"]
    if res["restored"]:
        lines += [f"- expired directives dropped (method restored to the EA default): {', '.join(res['restored'])}"]
    if res["errors"]:
        lines += ["", "## Problems", ""] + [f"- {e}" for e in res["errors"]]
    lines += ["", "## Proposed control.json", "",
              "Apply with `python scripts/coordinator_report.py --apply` (sets the real time, seq + 1, normalizes and logs "
              "to `control/CHANGELOG.md`), or edit by hand and run `python scripts/control.py normalize`.", "",
              "```json", json.dumps(res["proposal"], indent=2, ensure_ascii=False) if res["proposal"] else "null", "```", ""]
    return "\n".join(lines)


def apply(root: Path, res: dict, now: dt.datetime) -> int:
    if not res["proposal"]:
        print("error: no valid proposal", file=sys.stderr)
        return 1
    raw = {k: v for k, v in res["proposal"].items() if not k.endswith("_unix")}
    raw["updated_utc"] = iso_z(now)
    raw["global"] = {k: v for k, v in raw["global"].items() if k != "expires_unix"}
    raw["methods"] = {c: {k: v for k, v in d.items() if k != "expires_unix"} for c, d in raw["methods"].items()}
    raw["news_blocks"] = [{k: v for k, v in w.items() if not k.endswith("_unix")} for w in raw["news_blocks"]]
    write_text_atomic(root / control.CONTROL_PATH, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return control.cmd_normalize(root, now, "HEAD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Suggest control.json changes from aggregates (pre-registered rules)")
    ap.add_argument("--root")
    ap.add_argument("--now")
    ap.add_argument("--apply", action="store_true", help="write the proposal to control/control.json")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    res = evaluate(root, now)
    day = now.date()
    out_dir = root / "reports" / "coord"
    write_text_atomic(out_dir / f"{day.isoformat()}.md", render(res, day))
    write_text_atomic(out_dir / f"{day.isoformat()}.json", dumps_pretty(
        {k: res[k] for k in ("as_of", "accounts", "hits", "changes", "restored", "proposal", "errors")}))
    print(f"wrote reports/coord/{day.isoformat()}.md: {len(res['hits'])} rule hit(s), {len(res['changes'])} change(s)")
    if args.apply:
        return apply(root, res, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
