#!/usr/bin/env python3
"""Offline walk-forward replay of the coordinator rules (coordinator_report.decide) on a trade stream.

The stream is a CSV file kept OUTSIDE the public repository (research data). Columns (header names):
  t0, t1          entry / exit time, unix seconds UTC (or t0_utc / t1_utc "YYYY-MM-DD HH:MM")
  code            method code: M### or any one letter + 3 digits (X### is mapped to M###, as the EA's report would)
  phase           EA phase label at entry (e.g. SUPBH); anything else -> "none"
  side            BUY / SELL (or 1 / -1)
  r  or r_<COST>  outcome in R of one full-size trade (choose the column with --cost)
  size, risk_mult optional: the EA's own sizing; baseline weight = size x risk_mult (default 1)
  family          optional, reported only
  auto_ok         optional 1/0: would global.side "auto" (the EA's CDS) allow this trade; without it "auto" is not applied

Walk-forward: decisions at 00:00, 06:47 and 12:47 UTC. At each decision only trades CLOSED before it are
visible, exactly as the EA's aggregate report would show them (rolling 30 days, week, month to date, daily
history; signals blocked by the control are only visible as shadow counts). The proposal then governs the
trades entered until the next decision: pause = not taken (shadow CTRL), news block = not taken (NEWS),
side = not taken (SIDE), otherwise weight = baseline x min(global x method, --cap). Trades are filtered
after the EA's own execution (no substitution of freed slots: an approximation).

Report per period: win%, trades/month, net R, closed-equity DD (R), max losing streak, losing trades, worst
day (R, exit day UTC); change in points and relative %; random control (same number of trades kept per month,
drawn uniformly from the uncoordinated stream) with a one-sided p-value for the win%.

Usage:
  python scripts/coord_replay.py STREAM.csv [--periods D7,C41P,CLEAN] [--cost STD] [--cap 1.0]
         [--risk-pct 0.5] [--set key=JSON ...] [--reps 1000] [--seed 7] [--json OUT.json]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import heapq
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_events  # noqa: E402
import control  # noqa: E402
import coordinator_report as cr  # noqa: E402
from zumi_common import load_config, repo_root  # noqa: E402

UTC = dt.timezone.utc
DAY = 86400
DECISIONS = (0, 6 * 3600 + 47 * 60, 12 * 3600 + 47 * 60)   # 00:00, 06:47, 12:47 UTC
CODE_RE = re.compile(r"^[A-Z](\d{3})$")
PHASE_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def months(a: str, b: str) -> list[str]:
    y, m = int(a[:4]), int(a[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


PERIODS = {
    "D7": months("2025-01", "2026-11"),
    "C41P": months("2023-01", "2024-06"),
    "HOLD": months("2024-07", "2024-12"),
    "C41": months("2023-01", "2026-11"),
    "CLEAN": months("2018-01", "2018-06") + months("2019-07", "2020-12"),
}


def period_months(name: str) -> list[str]:
    if name in PERIODS:
        return PERIODS[name]
    a, _, b = name.partition(":")
    return months(a, b or a)


def _unix(rec: dict, key: str) -> int:
    v = (rec.get(key) or "").strip()
    if v:
        return int(float(v))
    s = (rec.get(f"{key}_utc") or "").strip()
    return int(dt.datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=UTC).timestamp())


def load_stream(path: Path, cost: str = "STD") -> tuple[list[dict], int]:
    """-> (trades sorted by entry, number of rows skipped for a missing outcome)."""
    trades, skipped = [], 0
    with open(path, encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            rv = (rec.get(f"r_{cost}") if f"r_{cost}" in rec else rec.get("r")) or ""
            if not rv.strip():
                skipped += 1
                continue
            m = CODE_RE.match((rec.get("code") or "").strip())
            side = (rec.get("side") or "").strip().upper()
            phase = (rec.get("phase") or "").strip()
            t0, t1 = _unix(rec, "t0"), _unix(rec, "t1")
            ok = (rec.get("auto_ok") or "").strip()
            trades.append({
                "t0": t0, "t1": max(t1, t0), "r": float(rv), "code": f"M{m.group(1)}" if m else "M000",
                "phase": phase if PHASE_RE.match(phase) else "none",
                "side": 1 if side in ("BUY", "1", "+1", "B") else -1,
                "base": float(rec.get("size") or 1.0) * float(rec.get("risk_mult") or 1.0),
                "fam": (rec.get("family") or "").strip(), "auto_ok": None if ok == "" else ok in ("1", "true", "True"),
                "mo": dt.datetime.fromtimestamp(t0, UTC).strftime("%Y-%m")})
    trades.sort(key=lambda t: (t["t0"], t["code"], t["t1"]))
    return trades, skipped


# ---------------------------------------------------------------- synthetic aggregate report (what the EA would send)
def _group_stats(rows: list[dict], key) -> dict:
    out: dict[str, dict] = {}
    for t in rows:                                   # rows are in exit order
        g = out.setdefault(key(t), {"n": 0, "wins": 0, "net_r": 0.0, "cur_ls": 0, "_seq": []})
        win = t["r"] > 0
        g["n"] += 1
        g["wins"] += int(win)
        g["net_r"] += t["r"]
        g["cur_ls"] = 0 if win else g["cur_ls"] + 1
        g["_seq"].append(int(win))
    for g in out.values():
        last = g.pop("_seq")[-20:]
        g["l20_n"], g["l20_wins"] = len(last), sum(last)
        g["net_r"] = round(g["net_r"], 4)
        g["win_pct"] = round(100.0 * g["wins"] / g["n"], 1)
    return out


def _dd_pct(rows: list[dict], risk_pct: float) -> float:
    cum = peak = dd = 0.0
    for t in rows:
        cum += t["r"] * t["w"] * risk_pct
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 3)


class Book:
    """Closed trades in exit order, with windows by exit time."""

    def __init__(self):
        self.rows: list[dict] = []
        self.t1: list[int] = []
        self._heap: list = []
        self._k = 0

    def add(self, t: dict) -> None:
        heapq.heappush(self._heap, (t["t1"], self._k, t))
        self._k += 1

    def close_until(self, ts: int) -> None:
        while self._heap and self._heap[0][0] <= ts:
            t1, _, t = heapq.heappop(self._heap)
            self.rows.append(t)
            self.t1.append(t1)

    def since(self, ts: int) -> list[dict]:
        return self.rows[bisect.bisect_right(self.t1, ts):]


def synth_report(taken: Book, shadow: Book, now: int, risk_pct: float) -> dict:
    d = dt.datetime.fromtimestamp(now, UTC)
    r30, week = taken.since(now - 30 * DAY), taken.since(now - 7 * DAY)
    mtd = taken.since(int(d.replace(day=1, hour=0, minute=0, second=0).timestamp()) - 1)

    def block(rows):
        return {"n": len(rows), "wins": sum(t["r"] > 0 for t in rows), "net_r": round(sum(t["r"] for t in rows), 4),
                "dd_pct": _dd_pct(rows, risk_pct),
                "by_method_phase": _group_stats(rows, lambda t: f"{t['code']}:{t['phase']}"),
                "by_method": _group_stats(rows, lambda t: t["code"])}
    sh = _group_stats(shadow.since(now - 30 * DAY), lambda t: f"{t['code']}:{t['why']}")
    data = {"day": d.date().isoformat(), "periods": {"r30": block(r30), "week": block(week), "mtd": block(mtd)},
            "shadow": {"by_method": {k: {"n": g["n"], "wins": g["wins"], "net_r": g["net_r"]} for k, g in sh.items()},
                       "periods": {}}, "flags": []}
    return {"v": 2, "kind": "ea_agg", "acct": "replay", "ts": now, "data": data}


HIST_KEYS = {"by_method_phase": lambda t: f"{t['code']}:{t['phase']}", "by_method": lambda t: t["code"],
             "by_side": lambda t: "buy" if t["side"] > 0 else "sell"}


class Window:
    """Sliding sum of the closed trades with exit in (now - days, now]: the history the coordinator would build
    from the EA's daily blocks. Incremental (each trade is added and removed once)."""

    def __init__(self, days: int):
        self.days, self.lo, self.hi = days, 0, 0
        self.tot = [0, 0, 0.0]
        self.g = {k: {} for k in HIST_KEYS}

    def _apply(self, t: dict, sign: int) -> None:
        win = int(t["r"] > 0)
        self.tot[0] += sign
        self.tot[1] += sign * win
        self.tot[2] += sign * t["r"]
        for fld, key in HIST_KEYS.items():
            c = self.g[fld].setdefault(key(t), [0, 0, 0.0])
            c[0] += sign
            c[1] += sign * win
            c[2] += sign * t["r"]
            if c[0] == 0:
                del self.g[fld][key(t)]

    def update(self, book: Book, now: int) -> dict:
        while self.hi < len(book.rows):
            self._apply(book.rows[self.hi], 1)
            self.hi += 1
        while self.lo < self.hi and book.t1[self.lo] <= now - self.days * DAY:
            self._apply(book.rows[self.lo], -1)
            self.lo += 1
        out = {"days": self.days, "n": self.tot[0], "wins": self.tot[1], "net_r": round(self.tot[2], 4)}
        for fld in HIST_KEYS:
            out[fld] = {k: {"n": c[0], "wins": c[1], "net_r": round(c[2], 4)} for k, c in sorted(self.g[fld].items())}
        return out


def synth_history(taken: Book, now: int, days: int) -> dict:
    """One-off history (non-incremental use)."""
    return Window(days).update(taken, now)


# ---------------------------------------------------------------- replay
def decision_times(first: int, last: int) -> list[int]:
    day = first // DAY * DAY
    out = []
    while day <= last + DAY:
        out += [day + s for s in DECISIONS]
        day += DAY
    return out


def replay(trades: list[dict], c: dict, events=(), cap: float = 1.0, risk_pct: float = 0.5,
           neutral: bool = False) -> tuple[list[dict], dict]:
    """-> (trades with 'w' (0 = not taken) and 'why', summary). neutral=True: no coordination at all."""
    out = [dict(t, w=t["base"], why="") for t in trades]
    if neutral or not out:
        return out, {"decisions": 0, "hits": {}}
    taken, shadow = Book(), Book()
    w_health, w_side = Window(int(c["health_window_days"])), Window(int(c["side_window_days"]))
    current = None
    times = decision_times(out[0]["t0"], out[-1]["t0"])
    hits: dict[str, int] = {}
    side_counts = {"auto": 0, "both": 0}
    i = 0
    for k, d in enumerate(times):
        nxt = times[k + 1] if k + 1 < len(times) else d + DAY
        if i < len(out) and out[i]["t0"] >= nxt:
            continue
        taken.close_until(d)
        shadow.close_until(d)
        rep = synth_report(taken, shadow, d, risk_pct)
        hist = {"replay": {"health": w_health.update(taken, d), "side": w_side.update(taken, d)}}
        now = dt.datetime.fromtimestamp(d, UTC)
        res = cr.decide({"replay": rep}, hist, current, now, c, {}, events, {})
        if res["proposal"] is None:
            raise RuntimeError(f"invalid proposal at {now}: {res['errors']}")
        current = res["proposal"]
        for h in res["hits"]:
            key = f"{h['rule']} {h['action'].split(' ')[0]}"
            hits[key] = hits.get(key, 0) + 1
        eff = control.effective(current, now)
        side = eff["global"].get("side", "both")
        side_counts["auto" if side == "auto" else "both"] += 1
        blocks = [(w["start_unix"], w["end_unix"]) for w in eff["news_blocks"] if w["kind"] == "block"]
        while i < len(out) and out[i]["t0"] < nxt:
            t = out[i]
            i += 1
            if t["t0"] < d:          # entered before the first decision of the replay: uncoordinated
                taken.add(t)
                continue
            m = control.risk_mult(eff, t["code"], t["phase"], cap)
            if any(a <= t["t0"] < b for a, b in blocks):
                t["w"], t["why"] = 0.0, "NEWS"
            elif m <= 0:
                t["w"], t["why"] = 0.0, "CTRL"
            elif (side == "buy" and t["side"] < 0) or (side == "sell" and t["side"] > 0) or (
                    side == "auto" and t["auto_ok"] is False):
                t["w"], t["why"] = 0.0, "SIDE"
            else:
                t["w"] = t["base"] * m
            (taken if t["w"] > 0 else shadow).add(t)
    return out, {"decisions": len(times), "hits": dict(sorted(hits.items())), "side_decisions": side_counts}


# ---------------------------------------------------------------- metrics
def metrics(rows: list[dict], n_months: int, weight: str = "w") -> dict:
    rows = sorted((t for t in rows if t[weight] > 0), key=lambda t: (t["t1"], t["t0"]))
    n = len(rows)
    wins = sum(t["r"] > 0 for t in rows)
    cum = peak = dd = 0.0
    ls = mls = 0
    days: dict[int, float] = {}
    for t in rows:
        x = t["r"] * t[weight]
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        ls = ls + 1 if t["r"] < 0 else 0
        mls = max(mls, ls)
        days[t["t1"] // DAY] = days.get(t["t1"] // DAY, 0.0) + x
    return {"n": n, "per_month": round(n / max(n_months, 1), 1), "win": round(100.0 * wins / n, 2) if n else 0.0,
            "net": round(cum, 1), "dd": round(dd, 1), "maxls": mls, "losers": sum(t["r"] < 0 for t in rows),
            "worst_day": round(min(days.values()), 2) if days else 0.0}


def random_control(base_rows: list[dict], kept_per_month: dict, n_months: int, reps: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_m: dict[str, list] = {}
    for t in base_rows:
        by_m.setdefault(t["mo"], []).append(t)
    sims = []
    for _ in range(reps):
        pick = []
        for mo, k in sorted(kept_per_month.items()):
            pool = by_m.get(mo, [])
            pick += rng.sample(pool, min(k, len(pool)))
        sims.append(metrics([dict(t, b=t["base"]) for t in pick], n_months, "b"))
    return sims


HIGHER = {"win": True, "per_month": True, "net": True, "dd": False, "maxls": False, "losers": False, "worst_day": True}


def compare(before: dict, after: dict, sims: list[dict]) -> list[dict]:
    rows = []
    for k, up in HIGHER.items():
        b, a = before[k], after[k]
        delta = a - b
        rel = None if b == 0 else 100.0 * delta / abs(b)
        better = rel is not None and ((rel > 0) if up else (rel < 0))
        ctrl = sum(s[k] for s in sims) / len(sims) if sims else None
        row = {"metric": k, "before": b, "after": a, "delta": round(delta, 2), "rel_pct": None if rel is None else round(rel, 1),
               "better": better, "ge10_rel": bool(rel is not None and abs(rel) >= 10 and better),
               "ctrl_mean": None if ctrl is None else round(ctrl, 2)}
        if k == "win" and sims:
            row["p"] = round(sum(s["win"] >= a - 1e-9 for s in sims) / len(sims), 4)
            row["lift_vs_ctrl"] = round(a - ctrl, 2)
        rows.append(row)
    return rows


def evaluate_periods(out: list[dict], periods: list[str], reps: int, seed: int) -> dict:
    res = {}
    for name in periods:
        ms = set(period_months(name))
        rows = [t for t in out if t["mo"] in ms]
        if not rows:
            continue
        nm = len(ms)
        before = metrics([dict(t, b=t["base"]) for t in rows], nm, "b")
        after = metrics(rows, nm, "w")
        kept: dict[str, int] = {}
        for t in rows:
            if t["w"] > 0:
                kept[t["mo"]] = kept.get(t["mo"], 0) + 1
        sims = random_control(rows, kept, nm, reps, seed) if reps else []
        why: dict[str, int] = {}
        for t in rows:
            if t["why"]:
                why[t["why"]] = why.get(t["why"], 0) + 1
        res[name] = {"months": nm, "before": before, "after": after, "blocked": dict(sorted(why.items())),
                     "reduced": sum(1 for t in rows if 0 < t["w"] < t["base"] - 1e-12),
                     "table": compare(before, after, sims)}
    return res


def render(res: dict, label: str) -> str:
    lines = [f"== {label}"]
    for name, r in res.items():
        b, a = r["before"], r["after"]
        lines.append(f"-- {name} ({r['months']} months): trades {b['n']} -> {a['n']} ({b['per_month']} -> {a['per_month']}/month), "
                     f"blocked {r['blocked']}, reduced {r['reduced']}")
        lines.append(f"   {'metric':<10}{'before':>9}{'after':>9}{'delta':>9}{'rel %':>8}{'ctrl':>9}  note")
        for x in r["table"]:
            note = ("better" if x["better"] else "worse/same") + (", >=10% rel" if x["ge10_rel"] else "")
            if x["metric"] == "win" and "p" in x:
                note += f", vs control {x['lift_vs_ctrl']:+.2f} pts p={x['p']}"
            rel = "-" if x["rel_pct"] is None else f"{x['rel_pct']:+.1f}"
            lines.append(f"   {x['metric']:<10}{x['before']:>9}{x['after']:>9}{x['delta']:>+9.2f}{rel:>8}"
                         f"{'-' if x['ctrl_mean'] is None else x['ctrl_mean']:>9}  {note}")
    return "\n".join(lines)


def parse_sets(items) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Walk-forward replay of the coordinator rules on a trade stream")
    ap.add_argument("stream")
    ap.add_argument("--root")
    ap.add_argument("--periods", default="D7,C41P,CLEAN")
    ap.add_argument("--cost", default="STD")
    ap.add_argument("--cap", type=float, default=1.0, help="the user's local InpGhMaxRiskMult")
    ap.add_argument("--risk-pct", type=float, default=0.5, help="%% of balance per 1R (drawdown ladder input)")
    ap.add_argument("--set", action="append", help="override a coordinator setting, e.g. alloc_enable=true")
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    cfg = load_config(root)
    c = cr.coord_cfg(cfg)
    c.update(parse_sets(args.set))
    events = calendar_events.load_events(root / cfg["calendar"]["file"])
    trades, skipped = load_stream(Path(args.stream), args.cost)
    out, summ = replay(trades, c, events, args.cap, args.risk_pct)
    res = evaluate_periods(out, [p for p in args.periods.split(",") if p], args.reps, args.seed)
    label = f"{Path(args.stream).name} cost {args.cost} cap {args.cap} ({len(trades)} trades, {skipped} skipped)"
    print(render(res, label))
    print(f"rule hits (decisions {summ['decisions']}): {summ['hits']}; side decisions {summ.get('side_decisions')}")
    if args.json:
        Path(args.json).write_text(json.dumps({"label": label, "settings": {k: c[k] for k in sorted(c) if not k.startswith('_')},
                                               "summary": summ, "periods": res}, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
