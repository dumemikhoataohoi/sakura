"""Aggregate EA reports (schema v2, kind "ea_agg"): validator / sanitiser and merge logic.

A v2 report carries ONLY aggregated statistics per period (day, rolling week, month to date):
no individual trades, no times, no prices, no SL/TP, no tickets. Method names are replaced by
codes (M001..M999) in the EA; anything that does not look like a code is folded into M000.

The validator is a whitelist: unknown keys are dropped and their names are returned so the
caller can log what was stripped (never the values).
"""
from __future__ import annotations

import datetime as dt
import math
import re

from zumi_common import mask_secrets

PERIODS = ("day", "week", "mtd")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
BUCKETS = ("00", "04", "08", "12", "16", "20")  # 4-hour UTC buckets of the entry time
MIN_CELL_N = 3                                    # grid cells below this count show no win data
METHOD_RE = re.compile(r"^M\d{3}$")
PHASE_RE = re.compile(r"[^A-Za-z0-9_-]")
FLAG_KEY_RE = re.compile(r"[^a-z0-9_:.-]")
RULES = ("no_sl", "risk_cap", "news", "daily_cap")

AGG_INT = ("n", "wins", "losses", "be", "max_ls", "cur_ls", "lead_ls")
AGG_FLOAT = ("win_pct", "net_r", "avg_r", "gross_win_r", "gross_loss_r", "pf", "net_pct", "dd_pct",
             "mae_r", "mfe_r", "n_r", "n_ex")
GROUP_KEYS = {"n": int, "wins": int, "win_pct": float, "net_r": float, "net_pct": float}
EQ_KEYS = ("day_pl_pct", "eq_day_pct", "float_pct")
# per-trade style keys: if any of these show up anywhere they are stripped (and reported)
PER_TRADE_KEYS = {"trades", "log", "deal", "deals", "pos", "position", "ticket", "order", "t", "t_in", "t_out",
                  "time", "open_time", "close_time", "in", "out", "price", "entry", "exit", "sl", "tp", "bid",
                  "ask", "pc", "d1_open", "atr", "move", "cmt", "comment", "magic", "login", "account", "balance",
                  "equity", "profit", "pl", "volume", "vol", "vol_in"}


class AggError(ValueError):
    pass


def _num(v, typ):
    if isinstance(v, bool) or v is None:
        return None
    try:
        out = typ(v)
    except (TypeError, ValueError):
        return None
    if isinstance(out, float) and not math.isfinite(out):
        return None
    return out


SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,24}$")


def _note(stripped: set, path: str, key) -> None:
    # Only report key names that look like identifiers: an arbitrary key could be a method name.
    key = str(key) if SAFE_KEY_RE.match(str(key)) else "<other>"
    stripped.add(f"{path}.{key}" if path else key)


def _group(obj, path: str, stripped: set) -> dict | None:
    if not isinstance(obj, dict):
        return None
    out = {}
    for k, v in obj.items():
        if k in GROUP_KEYS:
            val = _num(v, GROUP_KEYS[k])
            if val is not None:
                out[k] = val
        else:
            _note(stripped, path, k)
    if "n" not in out:
        return None
    if "wins" not in out and out.get("win_pct") is not None:
        out["wins"] = int(round(out["n"] * out["win_pct"] / 100.0))
    return out


def _merge_group(a: dict, b: dict) -> dict:
    out = {"n": a.get("n", 0) + b.get("n", 0), "wins": a.get("wins", 0) + b.get("wins", 0),
           "net_r": round(a.get("net_r", 0.0) + b.get("net_r", 0.0), 3)}
    if "net_pct" in a or "net_pct" in b:
        out["net_pct"] = round(a.get("net_pct", 0.0) + b.get("net_pct", 0.0), 4)
    out["win_pct"] = round(100.0 * out["wins"] / out["n"], 1) if out["n"] else None
    return out


def _groups(obj, path: str, stripped: set, key_ok) -> dict:
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    for raw_key, val in obj.items():
        key = key_ok(str(raw_key))
        g = _group(val, f"{path}.{key}", stripped)   # mapped key only: raw names never reach the log
        if g is None:
            continue
        if key != raw_key:
            stripped.add(f"{path}.<renamed>")
        out[key] = _merge_group(out[key], g) if key in out else g
    return out


def _method_key(k: str) -> str:
    return k if METHOD_RE.match(k) else "M000"   # raw method names are never kept


def _phase_key(k: str) -> str:
    return PHASE_RE.sub("", k)[:24] or "none"


def _grid(obj, path: str, stripped: set) -> dict:
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    for wd, row in obj.items():
        if wd not in WEEKDAYS or not isinstance(row, dict):
            _note(stripped, path, wd)
            continue
        for b, cell in row.items():
            if b not in BUCKETS or not isinstance(cell, dict):
                _note(stripped, f"{path}.{wd}", b)
                continue
            n = _num(cell.get("n"), int)
            if not n or n <= 0:
                continue
            c = {"n": n}
            wins = _num(cell.get("wins"), int)
            if wins is None and cell.get("win_pct") is not None:
                wins = int(round(n * float(cell["win_pct"]) / 100.0))
            if n >= MIN_CELL_N and wins is not None:   # small cells: count only
                c["wins"] = max(0, min(wins, n))
                c["win_pct"] = round(100.0 * c["wins"] / n, 1)
            out.setdefault(wd, {})[b] = c
    return out


def sanitize_agg_block(obj, path: str, stripped: set) -> dict | None:
    if not isinstance(obj, dict):
        return None
    out: dict = {}
    for k, v in obj.items():
        if k in AGG_INT:
            val = _num(v, int)
        elif k in AGG_FLOAT:
            val = _num(v, float)
        elif k in ("from", "to"):
            val = str(v)[:10] if re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)[:10]) else None
        elif k == "rule":
            val = {r: _num(c, int) for r, c in (v or {}).items() if r in RULES and _num(c, int) is not None} \
                if isinstance(v, dict) else None
        elif k == "by_method":
            val = _groups(v, f"{path}.by_method", stripped, _method_key)
        elif k == "by_phase":
            val = _groups(v, f"{path}.by_phase", stripped, _phase_key)
        elif k == "grid":
            val = _grid(v, f"{path}.grid", stripped)
        else:
            _note(stripped, path, k)
            continue
        if val is not None:
            out[k] = val
    if "n" not in out:
        out["n"] = 0
    return out


def sanitize_v2(payload: dict, rcfg: dict, now: dt.datetime, acct: str) -> tuple[dict, list[str]]:
    """Whitelist validator for kind 'ea_agg'. Returns (report, sorted list of stripped key paths)."""
    stripped: set = set()
    for k in payload:
        if k not in ("v", "kind", "acct", "rid", "part", "parts", "ts", "data"):
            _note(stripped, "", k)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise AggError("data must be an object")
    ts = _num(payload.get("ts"), int) or int(now.timestamp())
    if abs(ts - now.timestamp()) > 400 * 86400:
        ts = int(now.timestamp())
    day = str(data.get("day", ""))[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
    out: dict = {"day": day, "periods": {}, "flags": []}
    for k, v in data.items():
        if k in ("ea", "ver", "reason"):
            out[k] = mask_secrets(str(v))[:32]
        elif k == "phase":
            out[k] = _phase_key(str(v))
        elif k == "day":
            continue
        elif k == "eq" and isinstance(v, dict):
            eq = {}
            for ek, ev in v.items():
                if ek in EQ_KEYS and _num(ev, float) is not None:
                    eq[ek] = _num(ev, float)
                else:
                    _note(stripped, "data.eq", ek)
            out["eq"] = eq
        elif k == "mkt" and isinstance(v, dict):
            mkt = {}
            for mk, mv in v.items():
                if mk == "sym":
                    mkt["sym"] = re.sub(r"[^A-Za-z0-9._#-]", "", str(mv))[:16]
                elif mk == "move_atr" and _num(mv, float) is not None:
                    mkt["move_atr"] = round(_num(mv, float), 2)
                else:
                    _note(stripped, "data.mkt", mk)
            out["mkt"] = mkt
        elif k == "periods" and isinstance(v, dict):
            for pk, pv in v.items():
                if pk in PERIODS:
                    block = sanitize_agg_block(pv, f"data.periods.{pk}", stripped)
                    if block is not None:
                        out["periods"][pk] = block
                else:
                    _note(stripped, "data.periods", pk)
        elif k == "flags" and isinstance(v, list):
            for item in v[: int(rcfg.get("max_flags", 50))]:
                if isinstance(item, dict) and item.get("key"):
                    flag = {"key": FLAG_KEY_RE.sub("", str(item["key"]).lower())[:40] or "ea"}
                    if item.get("msg"):
                        flag["msg"] = mask_secrets(str(item["msg"]))[:160]
                    out["flags"].append(flag)
        else:
            _note(stripped, "data", k)
    rep = {"v": 2, "kind": "ea_agg", "acct": acct, "rid": f"{acct}-{ts}", "part": 1, "parts": 1, "ts": ts, "data": out}
    return rep, sorted(stripped)


# ---------------------------------------------------------------- merging
def empty_agg() -> dict:
    return {"n": 0, "wins": 0, "losses": 0, "be": 0, "net_r": 0.0, "gross_win_r": 0.0, "gross_loss_r": 0.0,
            "net_pct": 0.0, "max_ls": 0, "cur_ls": 0, "lead_ls": 0, "dd_pct": 0.0,
            "rule": {}, "by_method": {}, "by_phase": {}, "grid": {}}


def finish(a: dict) -> dict:
    n, wins, losses = a.get("n", 0), a.get("wins", 0), a.get("losses", 0)
    a["win_pct"] = round(100.0 * wins / (wins + losses), 1) if wins + losses else None
    a["avg_r"] = round(a.get("net_r", 0.0) / n, 3) if n else None
    gl = a.get("gross_loss_r") or 0.0
    a["pf"] = round((a.get("gross_win_r") or 0.0) / gl, 2) if gl > 0 else None
    return a


def merge_aggs(aggs: list[dict]) -> dict:
    """Combine chronologically ordered, non-overlapping period aggregates (e.g. the daily
    aggregates of a week). Counts and R add up exactly; streaks chain through lead_ls/cur_ls;
    the drawdown is a lower bound (max of each part and of the end-of-part P/L path)."""
    out = empty_agg()
    cum = peak = 0.0
    path_dd = 0.0
    mae_w = mfe_w = 0.0
    mae_n = mfe_n = 0
    for a in aggs:
        n = a.get("n", 0) or 0
        if not n:
            continue
        all_losses = a.get("lead_ls", 0) == n
        if out["n"] == 0:
            out["lead_ls"] = a.get("lead_ls", 0)
        elif out["lead_ls"] == out["n"]:
            out["lead_ls"] += a.get("lead_ls", 0)
        chained = out["cur_ls"] + a.get("lead_ls", 0)
        out["max_ls"] = max(out["max_ls"], a.get("max_ls", 0), chained)
        out["cur_ls"] = chained if all_losses else a.get("cur_ls", 0)
        for k in ("n", "wins", "losses", "be"):
            out[k] += a.get(k, 0) or 0
        for k in ("net_r", "gross_win_r", "gross_loss_r", "net_pct"):
            out[k] = round(out[k] + (a.get(k) or 0.0), 4)
        out["dd_pct"] = max(out["dd_pct"], a.get("dd_pct") or 0.0)
        cum += a.get("net_pct") or 0.0
        peak = max(peak, cum)
        path_dd = max(path_dd, peak - cum)
        for r, c in (a.get("rule") or {}).items():
            out["rule"][r] = out["rule"].get(r, 0) + c
        for fld in ("by_method", "by_phase"):
            for k, g in (a.get(fld) or {}).items():
                out[fld][k] = _merge_group(out[fld][k], g) if k in out[fld] else dict(g)
        for wd, row in (a.get("grid") or {}).items():
            for b, c in row.items():
                cur = out["grid"].setdefault(wd, {}).setdefault(b, {"n": 0, "wins": 0})
                cur["n"] += c.get("n", 0)
                # a part with a suppressed (small) cell makes the merged win count unknown
                cur["wins"] = None if cur["wins"] is None or "wins" not in c else cur["wins"] + c["wins"]
        if a.get("mae_r") is not None:
            mae_w += a["mae_r"] * n
            mae_n += n
        if a.get("mfe_r") is not None:
            mfe_w += a["mfe_r"] * n
            mfe_n += n
    out["dd_pct"] = round(max(out["dd_pct"], path_dd), 3)
    for row in out["grid"].values():
        for c in row.values():
            if c["n"] >= MIN_CELL_N and c["wins"] is not None:
                c["win_pct"] = round(100.0 * c["wins"] / c["n"], 1)
            else:
                c.pop("wins", None)
    if mae_n:
        out["mae_r"] = round(mae_w / mae_n, 3)
    if mfe_n:
        out["mfe_r"] = round(mfe_w / mfe_n, 3)
    return finish(out)
