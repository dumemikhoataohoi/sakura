#!/usr/bin/env python3
"""Daily coordination helper: suggest control.json changes from the aggregates, using ONLY the
pre-registered rules in config/settings.json > coordinator. Claude reads the suggestion and decides.

Inputs: the newest aggregate EA report per account (periods day / week / mtd / r30 with by_method,
by_phase, by_method_phase, by_side, flags, mkt.ctx and shadow), the daily blocks of the stored reports
of the last `health_window_days` / `side_window_days` (history), data/latest.json, data/quality.json,
the event calendar and the current control/control.json.

Rules (v2; per method code M### and EA phase label):
  R01 pause   current losing streak of the code in that phase (rolling 30 days) >= streak_pause_n
  R02 pause   Wilson health switch (r02_mode "wilson"): cell = code x phase (or code x D1 band) over the
              history window; pause when n >= health_min_n and the Wilson upper bound of the win% <
              health_pause_ub_pct. (r02_mode "last20" = the v1 rule: last-20 win% < winrate_pause_pct.)
  R08 on      a code paused by R02 whose cell Wilson lower bound >= health_restore_lb_pct, or whose
              blocked-signal record (shadow M###:CTRL, n >= health_min_n) has a lower bound >= that level;
              also the v1 rule: last-20 win% >= recover_winpct and no current streak
  R02 reduce  risk allocation (alloc_enable): per code risk_mult in [alloc_min_mult, 1] from the code's
              shrunk expectancy net_r / (n + alloc_prior_n) < 0; never above 1
  R07 boost   last-20 n >= boost_min_trades, win% >= boost_min_winpct, avg R >= boost_min_avg_r, no streak
              (effective only up to the user's local InpGhMaxRiskMult)
  R07 on      a code listed in enable_codes (pre-approved by the user) when the EA's current phase is listed
  R05 global  drawdown ladder: week or month-to-date drawdown >= step -> risk_mult (dd_ladder) and
              open_risk_pct (dd_open_risk_ladder, never above open_risk_local_pct; the EA applies min());
              pause_all only at the top step AND when the EA itself reports a crisis block (mkt.ctx.cr)
  R04 global  VIX high flag and vix_high_mult < 1 -> risk_mult = vix_high_mult (off by default)
  R09 global  side: "auto" when one side's Wilson upper bound < side_ub_pct over n >= side_min_n in the
              last side_window_days (needs the by_side aggregate of bridge 2.7), otherwise "both"
  R03 block   NFP/CPI/FOMC within news_lookahead_h -> block [t - before, t + after]
Directives that are no longer triggered are kept until they expire, then dropped ("restored").
Precedence per code: pause > boost > on > reduce (a kept pause is never lifted by a reduce).
Nothing is applied unless --apply is given. `decide()` is a pure function (used by coord_replay.py).

Output: reports/coord/YYYY-MM-DD.md (+ .json). With --apply the proposal is written to
control/control.json (seq + 1, normalized, CHANGELOG line).

Usage: python scripts/coordinator_report.py [--now ISO] [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import control  # noqa: E402
import reports_lib as rl  # noqa: E402
import vault  # noqa: E402
from zumi_common import dumps_pretty, iso_z, load_config, parse_utc, repo_root, utc_now  # noqa: E402

# v2 defaults; config/settings.json > coordinator overrides every key
V2_DEFAULTS = {
    "r02_mode": "wilson",              # "wilson" (v2), "last20" (v1) or "off"
    "health_window_days": 90,
    "health_min_n": 20,
    "health_z": 1.645,                 # two-sided 90 % Wilson interval
    "health_pause_ub_pct": 50.0,
    "health_restore_lb_pct": 50.0,
    "health_prior_n": 10,              # shrinkage weight towards the account's pooled win%
    "health_cell": "phase",            # "phase" (full EA label), "band" (D1 band only) or "code" (all phases)
    "phase_bands": ["SU", "WU", "R", "WD", "SD"],
    "phase_h4_states": ["IMP", "PB", "CTR"],
    "phase_vol_terciles": ["L", "N", "H"],
    "alloc_enable": False,
    "alloc_min_n": 10,
    "alloc_prior_n": 10,
    "alloc_slope": 1.5,
    "alloc_min_mult": 0.25,
    "alloc_step": 0.05,
    "dd_ladder": None,                 # None = v1 single step [[dd_reduce_pct, dd_reduce_mult]]
    "dd_open_risk_ladder": [],
    "open_risk_local_pct": 12.0,
    "dd_crisis_pause_all": True,
    "side_enable": True,
    "side_window_days": 14,
    "side_min_n": 30,
    "side_ub_pct": 45.0,
}
SIDE_KEYS = ("buy", "sell")


def coord_cfg(cfg: dict) -> dict:
    c = dict(V2_DEFAULTS)
    c.update(cfg.get("coordinator") or {})
    return c


def _load(path: Path):
    """JSON of a plaintext or .zv file; None when missing / invalid. vault.VaultError propagates (fail clearly)."""
    try:
        return json.loads(vault.read_text(path))
    except (OSError, json.JSONDecodeError):
        return None


def wilson(k: float, n: float, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval of k successes in n trials (fractions)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0))
    return max(0.0, (c - h) / d), min(1.0, (c + h) / d)


def shrunk(k: float, n: float, p0: float, m: float) -> float:
    return (k + m * p0) / (n + m) if n + m > 0 else p0


def band_of(phase: str, bands) -> str:
    best = ""
    for b in bands:
        if phase.startswith(b) and len(b) > len(best):
            best = b
    return best or phase


def cell_key(phase: str, c: dict) -> str:
    if c["health_cell"] == "code":
        return "*"
    return phase if c["health_cell"] != "band" or phase == "none" else band_of(phase, c["phase_bands"])


def expand_phases(key: str, c: dict) -> list[str]:
    """Phase labels a cell key covers (band mode: band x H4 state x vol tercile, the EA label grammar;
    code mode: "*" = every phase)."""
    if key == "*" or c["health_cell"] != "band" or key == "none" or key not in c["phase_bands"]:
        return [key]
    return [key + h + v for h in c["phase_h4_states"] for v in c["phase_vol_terciles"]]


def _add(dst: dict, key: str, g: dict) -> None:
    cur = dst.setdefault(key, {"n": 0, "wins": 0, "net_r": 0.0})
    cur["n"] += int(g.get("n", 0) or 0)
    cur["wins"] += int(g.get("wins", 0) or 0)
    cur["net_r"] = round(cur["net_r"] + float(g.get("net_r", 0.0) or 0.0), 4)


def latest_agg_reports(root: Path, now: dt.datetime, max_age_h: float) -> dict[str, dict]:
    since = now.timestamp() - max_age_h * 3600
    out: dict[str, dict] = {}
    for r in rl.load_reports(root, now.date() - dt.timedelta(days=2), now.date() + dt.timedelta(days=1)):
        if r.get("kind") == "ea_agg" and since <= r.get("ts", 0) <= now.timestamp() + 600:
            out[r["acct"]] = r
    return out


def build_history(root: Path, now: dt.datetime, days: int) -> dict[str, dict]:
    """Sum of the daily blocks (newest report of each UTC day) of the last `days` days, per account.
    Counts and R add up exactly (aggregate data only)."""
    first = (now - dt.timedelta(days=days)).date()
    newest: dict[tuple, dict] = {}
    for r in rl.load_reports(root, first, now.date()):
        if r.get("kind") != "ea_agg" or r.get("ts", 0) > now.timestamp() + 600:
            continue
        day = str((r.get("data") or {}).get("day", ""))
        if day < first.isoformat():
            continue
        key = (r["acct"], day)
        if key not in newest or r.get("ts", 0) >= newest[key].get("ts", 0):
            newest[key] = r
    out: dict[str, dict] = {}
    for (acct, _day), r in sorted(newest.items()):
        blk = ((r["data"].get("periods") or {}).get("day")) or {}
        h = out.setdefault(acct, {"days": 0, "n": 0, "wins": 0, "net_r": 0.0,
                                  "by_method_phase": {}, "by_method": {}, "by_side": {}})
        h["days"] += 1
        h["n"] += int(blk.get("n", 0) or 0)
        h["wins"] += int(blk.get("wins", 0) or 0)
        h["net_r"] = round(h["net_r"] + float(blk.get("net_r", 0.0) or 0.0), 4)
        for fld in ("by_method_phase", "by_method", "by_side"):
            for k, g in sorted((blk.get(fld) or {}).items()):
                if isinstance(g, dict):
                    _add(h[fld], k, g)
    return out


def _wr(g: dict) -> float | None:
    n = g.get("l20_n")
    return 100.0 * g.get("l20_wins", 0) / n if n else None


def _ladder_step(steps, value: float):
    hit = None
    for thr, val in sorted((float(a), float(b)) for a, b in (steps or [])):
        if value >= thr:
            hit = (thr, val)
    return hit


def decide(reps: dict, hist: dict | None, current: dict | None, now: dt.datetime, c: dict,
           latest: dict | None = None, events=(), quality: dict | None = None) -> dict:
    """Pure rule engine: reports + history + current control -> hits and a normalized proposal."""
    hist = hist or {}
    latest = latest or {}
    eff = control.effective(current, now)
    z = float(c["health_z"])
    hits: list[dict] = []
    pause: dict[str, dict] = {}      # code -> {"phases": {phase: severity}, "reason": str}
    boost: dict[str, set] = {}
    enable: dict[str, dict] = {}
    reduce: dict[str, float] = {}
    side_weak: list[dict] = []
    dd_max = 0.0
    crisis = False
    phases_now = set()

    def add_pause(code, phases, reason, sev):
        p = pause.setdefault(code, {"phases": {}, "reason": reason})
        for ph in phases:
            p["phases"][ph] = min(sev, p["phases"].get(ph, sev))

    for acct, r in sorted(reps.items()):
        data = r["data"]
        per = data.get("periods") or {}
        if data.get("phase"):
            phases_now.add(data["phase"])
        cr = ((data.get("mkt") or {}).get("ctx") or {}).get("cr")
        if isinstance(cr, str) and cr.startswith("BLOCK"):
            crisis = True
        dd_max = max(dd_max, (per.get("week") or {}).get("dd_pct") or 0.0, (per.get("mtd") or {}).get("dd_pct") or 0.0)
        block = per.get("r30") or per.get("mtd") or {}
        h = (hist.get(acct) or {}).get("health") or {}
        hs = (hist.get(acct) or {}).get("side") or {}
        shadow_m = (data.get("shadow") or {}).get("by_method") or {}

        # ---- v1 per-cell rules on the rolling-30-day block: R01 streak, (R02 last-20), R07 boost, R08 recovery
        for key, g in sorted((block.get("by_method_phase") or {}).items()):
            code, _, phase = key.partition(":")
            if code == "M000" or not control.METHOD_RE.match(code):
                continue
            wr, n20, cur = _wr(g), g.get("l20_n", 0), g.get("cur_ls", 0)
            avg_r = (g.get("net_r", 0.0) / g["n"]) if g.get("n") else 0.0
            stats = {"acct": acct, "code": code, "phase": phase, "n": g.get("n", 0), "l20_n": n20,
                     "l20_win_pct": round(wr, 1) if wr is not None else None, "cur_ls": cur, "avg_r": round(avg_r, 3)}
            if cur >= c["streak_pause_n"]:
                hits.append(dict(stats, rule="R01", action="pause"))
                add_pause(code, [phase], "R01", -1.0)
            elif (c["r02_mode"] == "last20" and wr is not None and n20 >= c["winrate_min_trades"]
                  and wr < c["winrate_pause_pct"]):
                hits.append(dict(stats, rule="R02", action="pause"))
                add_pause(code, [phase], "R02", wr / 100.0)
            elif (wr is not None and n20 >= c["boost_min_trades"] and wr >= c["boost_min_winpct"]
                  and avg_r >= c["boost_min_avg_r"] and cur == 0):
                hits.append(dict(stats, rule="R07", action="boost"))
                boost.setdefault(code, set()).add(phase)
            d = eff["methods"].get(code)
            if (d and d["mode"] == "pause" and (not d["phases"] or phase in d["phases"]) and wr is not None
                    and wr >= c["recover_winpct"] and cur == 0):
                hits.append(dict(stats, rule="R08", action="on"))
                enable[code] = {"phases": set(d["phases"]), "reason": "R08"}

        # ---- R02 v2: Wilson health switch per code x phase (or band) over the history window
        use_hist = h.get("n", 0) >= block.get("n", 0) and h.get("by_method_phase")
        src = h.get("by_method_phase") if use_hist else (block.get("by_method_phase") or {})
        cells: dict[str, dict] = {}
        for key, g in sorted(src.items()):
            code, _, phase = key.partition(":")
            if code == "M000" or not control.METHOD_RE.match(code):
                continue
            _add(cells.setdefault(code, {}), cell_key(phase or "none", c), g)
        if c["r02_mode"] == "wilson" and cells:
            tot_n = sum(g["n"] for cc in cells.values() for g in cc.values())
            tot_w = sum(g["wins"] for cc in cells.values() for g in cc.values())
            p0 = tot_w / tot_n if tot_n else 0.5
            for code, cc in sorted(cells.items()):
                rec = shadow_m.get(f"{code}:CTRL") or {}
                rec_ok = (rec.get("n", 0) >= c["health_min_n"]
                          and 100 * wilson(rec.get("wins", 0), rec["n"], z)[0] >= c["health_restore_lb_pct"])
                d = eff["methods"].get(code)
                for ck, g in sorted(cc.items()):
                    n, w = g["n"], g["wins"]
                    lb, ub = wilson(w, n, z)
                    stats = {"acct": acct, "code": code, "phase": ck, "n": n, "win_pct": round(100 * w / n, 1) if n else None,
                             "shrunk_pct": round(100 * shrunk(w, n, p0, c["health_prior_n"]), 1),
                             "lb_pct": round(100 * lb, 1), "ub_pct": round(100 * ub, 1), "avg_r": round(g["net_r"] / n, 3) if n else 0.0,
                             "src": f"{h.get('days', 0)}d" if use_hist else "r30"}
                    if n >= c["health_min_n"] and 100 * ub < c["health_pause_ub_pct"]:
                        if rec_ok:
                            hits.append(dict(stats, rule="R08", action="on (blocked signals recovered)"))
                        else:
                            hits.append(dict(stats, rule="R02", action="pause"))
                            add_pause(code, expand_phases(ck, c), "R02", ub)
                    elif (n >= c["health_min_n"] and 100 * lb >= c["health_restore_lb_pct"] and d
                          and d["mode"] == "pause" and d["reason_code"] == "R02"):
                        hits.append(dict(stats, rule="R08", action="on"))
                        enable[code] = {"phases": set(d["phases"]), "reason": "R08"}
                if rec_ok and d and d["mode"] == "pause" and d["reason_code"] == "R02" and code not in enable:
                    hits.append({"acct": acct, "code": code, "phase": ",".join(d["phases"]) or "*", "rule": "R08",
                                 "action": "on (blocked signals recovered)", "n": rec["n"],
                                 "win_pct": round(100 * rec.get("wins", 0) / rec["n"], 1)})
                    enable[code] = {"phases": set(d["phases"]), "reason": "R08"}

        # ---- R02 reduce: K7-style risk allocation from the code's shrunk expectancy (never above 1.0)
        if c["alloc_enable"]:
            src_m = h.get("by_method") if use_hist else (block.get("by_method") or {})
            for code, g in sorted((src_m or {}).items()):
                if code == "M000" or not control.METHOD_RE.match(code) or g.get("n", 0) < c["alloc_min_n"]:
                    continue
                e = float(g.get("net_r", 0.0)) / (g["n"] + c["alloc_prior_n"])
                if e >= 0:
                    continue
                m = min(1.0, max(c["alloc_min_mult"], 1.0 + c["alloc_slope"] * e))
                m = max(c["alloc_min_mult"], round(math.floor(m / c["alloc_step"] + 1e-9) * c["alloc_step"], 3))
                if m < 1.0:
                    reduce[code] = min(reduce.get(code, 1.0), m)
                    hits.append({"acct": acct, "code": code, "phase": "*", "rule": "R02", "action": f"reduce x{m:.2f}",
                                 "n": g["n"], "avg_r": round(g.get("net_r", 0.0) / g["n"], 3), "shrunk_e": round(e, 3)})

        # ---- R09: one side clearly weak over the last side_window_days (only when the EA sends by_side)
        if c["side_enable"]:
            for sd in SIDE_KEYS:
                g = (hs.get("by_side") or {}).get(sd)
                if g and g.get("n", 0) >= c["side_min_n"]:
                    lb, ub = wilson(g.get("wins", 0), g["n"], z)
                    if 100 * ub < c["side_ub_pct"]:
                        side_weak.append({"acct": acct, "code": "*", "phase": sd, "rule": "R09", "action": "side auto",
                                          "n": g["n"], "win_pct": round(100 * g.get("wins", 0) / g["n"], 1),
                                          "ub_pct": round(100 * ub, 1)})

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
    for code, m in reduce.items():
        if code in enable or code in boost or code in pause or (methods.get(code) or {}).get("mode") == "pause":
            continue
        methods[code] = {"mode": "reduce", "risk_mult": m, "phases": [], "reason_code": "R02", "expires_utc": exp}
    for code, e in enable.items():
        methods[code] = {"mode": "on", "risk_mult": 1.0, "phases": sorted(e["phases"]), "reason_code": e["reason"],
                         "expires_utc": exp}
    for code, phases in boost.items():
        if code not in pause:
            methods[code] = {"mode": "boost", "risk_mult": c["boost_mult"], "phases": sorted(phases),
                             "reason_code": "R07", "expires_utc": exp}
    for code, p in pause.items():   # pause wins
        ph = sorted(p["phases"].items(), key=lambda kv: (kv[1], kv[0]))[:control.MAX_PHASES]   # worst first
        phases = [] if "*" in p["phases"] else sorted(k for k, _ in ph)    # "*" = the whole code
        methods[code] = {"mode": "pause", "risk_mult": 0.0, "phases": phases,
                         "reason_code": p["reason"], "expires_utc": exp}

    g = {"risk_mult": 1.0, "pause_all": False, "reason_code": "R00", "expires_utc": exp}
    ladder = c["dd_ladder"] if isinstance(c.get("dd_ladder"), list) else [[c["dd_reduce_pct"], c["dd_reduce_mult"]]]
    step = _ladder_step(ladder, dd_max)
    ostep = _ladder_step(c.get("dd_open_risk_ladder"), dd_max)
    if step:
        g.update(risk_mult=step[1], reason_code="R05")
        hits.append({"code": "*", "phase": "*", "rule": "R05", "action": f"global x{step[1]}", "dd_pct": round(dd_max, 2)})
        top = max(float(a) for a, _ in ladder)
        if crisis and c["dd_crisis_pause_all"] and step[0] >= top:
            g["pause_all"] = True
            hits.append({"code": "*", "phase": "*", "rule": "R05", "action": "pause_all (EA crisis block)",
                         "dd_pct": round(dd_max, 2)})
    elif (latest.get("flags") or {}).get("vix_high") and c["vix_high_mult"] < 1.0:
        g.update(risk_mult=c["vix_high_mult"], reason_code="R04")
        hits.append({"code": "*", "phase": "*", "rule": "R04", "action": f"global x{c['vix_high_mult']}"})
    if ostep:
        cap = min(ostep[1], float(c["open_risk_local_pct"]))
        g["open_risk_pct"] = round(min(max(cap, control.OPEN_RISK_MIN), control.OPEN_RISK_MAX), 2)
        if g["reason_code"] == "R00":
            g["reason_code"] = "R05"
        hits.append({"code": "*", "phase": "*", "rule": "R05", "action": f"open risk cap {g['open_risk_pct']}%",
                     "dd_pct": round(dd_max, 2)})
    if side_weak:
        g["side"] = "auto"
        if g["reason_code"] == "R00":
            g["reason_code"] = "R09"
        hits.extend(side_weak)

    windows = [{k: w[k] for k in ("kind", "start_utc", "end_utc", "reason_code")}
               for w in (current or {}).get("news_blocks", []) if w["end_unix"] > now.timestamp()]
    seen = {(w["kind"], w["start_utc"], w["end_utc"]) for w in windows}
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
    return {"as_of": iso_z(base), "accounts": sorted(reps), "hits": hits, "restored": sorted(restored),
            "changes": ch, "proposal": proposal, "errors": errors, "warnings": warnings,
            "dd_max": round(dd_max, 2), "phases_now": sorted(phases_now), "crisis": crisis,
            "quality_ok": (quality or {}).get("ok", True), "vix_high": (latest.get("flags") or {}).get("vix_high", 0),
            "current_effective": eff}


def evaluate(root: Path, now: dt.datetime, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config(root)
    c = coord_cfg(cfg)
    reps = latest_agg_reports(root, now, c["report_max_age_h"])
    current, cur_errors, _ = control.normalize(_load(root / control.CONTROL_PATH) or {}, None)
    hh = build_history(root, now, int(c["health_window_days"]))
    hsd = build_history(root, now, int(c["side_window_days"]))
    hist = {a: {"health": hh.get(a) or {}, "side": hsd.get(a) or {}} for a in sorted(set(hh) | set(hsd))}
    events = calendar_events.load_events(root / cfg["calendar"]["file"])
    res = decide(reps, hist, current, now, c, _load(root / "data" / "latest.json") or {}, events,
                 _load(root / "data" / "quality.json") or {})
    res["errors"] = res["errors"] + [f"current control.json invalid: {e}" for e in cur_errors]
    return res


def _cell(h: dict, key: str):
    v = h.get(key)
    return "-" if v is None else v


def render(res: dict, day: dt.date) -> str:
    lines = [f"# Coordinator suggestion {day.isoformat()}", "",
             "Machine-generated from pre-registered rules (`config/settings.json` > `coordinator`). "
             "Claude reviews and decides; nothing here is applied automatically.", "",
             f"* as of: {res['as_of']}; accounts: {', '.join(res['accounts']) or 'none (no recent aggregate report)'}",
             f"* EA phase(s) now: {', '.join(res['phases_now']) or '-'}; max drawdown (week/MTD): {res['dd_max']} %",
             f"* data quality ok: {res['quality_ok']}; VIX high flag: {res['vix_high']}",
             f"* current control: {res['current_effective']['reason']}; EA crisis block reported: {res.get('crisis', False)}", ""]
    if not res["quality_ok"]:
        lines += ["**R06**: data quality problem; treat macro context with care.", ""]
    lines += ["## Rule hits", ""]
    if res["hits"]:
        lines += ["| rule | action | code | phase | n | last-20 n | last-20 win% | streak | avg R | win% | shrunk win% | Wilson 90% | window |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for h in res["hits"]:
            wil = f"{h['lb_pct']}-{h['ub_pct']}" if "lb_pct" in h else (f"<{h['ub_pct']}" if "ub_pct" in h else "-")
            lines.append(f"| {h['rule']} | {h['action']} | {_cell(h, 'code')} | {_cell(h, 'phase')} | {_cell(h, 'n')} | "
                         f"{_cell(h, 'l20_n')} | {_cell(h, 'l20_win_pct')} | {_cell(h, 'cur_ls')} | {_cell(h, 'avg_r')} | "
                         f"{_cell(h, 'win_pct')} | {_cell(h, 'shrunk_pct')} | {wil} | {_cell(h, 'src')} |")
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
    vault.write_text(root / control.CONTROL_PATH, json.dumps(raw, indent=2, ensure_ascii=False) + "\n", root)
    return control.cmd_normalize(root, now, "HEAD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Suggest control.json changes from aggregates (pre-registered rules)")
    ap.add_argument("--root")
    ap.add_argument("--now")
    ap.add_argument("--apply", action="store_true", help="write the proposal to control/control.json")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    try:
        res = evaluate(root, now)
        day = now.date()
        out_dir = root / "reports" / "coord"
        vault.write_text(out_dir / f"{day.isoformat()}.md", render(res, day), root)
        vault.write_text(out_dir / f"{day.isoformat()}.json", dumps_pretty(
            {k: res[k] for k in ("as_of", "accounts", "hits", "changes", "restored", "proposal", "errors")}), root)
    except vault.VaultError as exc:
        print(f"error: vault: {exc}", file=sys.stderr)
        return 1
    line = f"wrote reports/coord/{day.isoformat()}.md: {len(res['hits'])} rule hit(s), {len(res['changes'])} change(s)"
    print(vault.public_log(root, line, f"wrote reports/coord/{day.isoformat()}.md"))
    if args.apply:
        return apply(root, res, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
