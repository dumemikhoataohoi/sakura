"""Trade-journal analytics on reconstructed positions (pure functions, no I/O).

Money is always in % of balance (pl_pct); R uses the stop-loss at entry.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict

from reports_lib import outcome
from zumi_common import UTC

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _utc(ts: int | None) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(int(ts), UTC) if ts else None


def close_date(p: dict) -> dt.date | None:
    t = _utc(p.get("t_out"))
    return t.date() if t else None


def streaks(positions: list[dict]) -> tuple[int, int]:
    """(max consecutive losses, losses in a row at the end of the list). BE breaks a streak."""
    best = cur = 0
    for p in positions:
        if outcome(p) == "loss":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best, cur


def metrics(positions: list[dict]) -> dict:
    n = len(positions)
    outs = [outcome(p) for p in positions]
    wins, losses, be = outs.count("win"), outs.count("loss"), outs.count("be")
    pls = [p["pl_pct"] for p in positions if p.get("pl_pct") is not None]
    rs = [p["r"] for p in positions if p.get("r") is not None]
    gross_win = sum(v for v in pls if v > 0)
    gross_loss = -sum(v for v in pls if v < 0)
    win_rs = [p["r"] for p in positions if p.get("r") is not None and outcome(p) == "win"]
    loss_rs = [p["r"] for p in positions if p.get("r") is not None and outcome(p) == "loss"]
    max_ls, cur_ls = streaks(positions)
    decided = wins + losses
    return {
        "n": n, "wins": wins, "losses": losses, "be": be,
        "win_pct": round(100.0 * wins / decided, 1) if decided else None,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not gross_win else float("inf")),
        "exp_r": round(sum(rs) / len(rs), 3) if rs else None,
        "exp_pct": round(sum(pls) / len(pls), 3) if pls else None,
        "sum_r": round(sum(rs), 2) if rs else None,
        "sum_pct": round(sum(pls), 3) if pls else None,
        "avg_win_r": round(sum(win_rs) / len(win_rs), 2) if win_rs else None,
        "avg_loss_r": round(sum(loss_rs) / len(loss_rs), 2) if loss_rs else None,
        "no_r": n - len(rs),
        "max_loss_streak": max_ls, "end_loss_streak": cur_ls,
    }


def drawdown_episodes(positions: list[dict], min_depth_pct: float = 1.0) -> list[dict]:
    """Peak-to-trough episodes of the additive pl_pct curve (closed positions in order)."""
    eps, cum, peak, peak_t = [], 0.0, 0.0, None
    trough, trough_t, in_dd = 0.0, None, False
    for p in positions:
        if p.get("pl_pct") is None:
            continue
        cum += p["pl_pct"]
        t = p.get("t_out")
        if cum >= peak:
            if in_dd and peak - trough >= min_depth_pct:
                eps.append({"start": peak_t, "trough": trough_t, "end": t, "depth_pct": round(peak - trough, 3)})
            in_dd, peak, peak_t, trough = False, cum, t, cum
        else:
            if not in_dd or cum < trough:
                trough, trough_t = cum, t
            in_dd = True
    if in_dd and peak - trough >= min_depth_pct:
        eps.append({"start": peak_t, "trough": trough_t, "end": None, "depth_pct": round(peak - trough, 3)})
    return eps


def group_metrics(positions: list[dict], key) -> "OrderedDict[str, dict]":
    groups: dict[str, list[dict]] = {}
    for p in positions:
        groups.setdefault(key(p) or "(none)", []).append(p)
    return OrderedDict(sorted(((k, metrics(v)) for k, v in groups.items()), key=lambda kv: -kv[1]["n"]))


def heatmap(positions: list[dict]) -> dict:
    """weekday x hour (UTC, entry time) -> {'n', 'sum_r', 'wins'}."""
    cells: dict[tuple[int, int], dict] = {}
    for p in positions:
        t = _utc(p.get("t_in") or p.get("t_out"))
        if not t:
            continue
        c = cells.setdefault((t.weekday(), t.hour), {"n": 0, "sum_r": 0.0, "wins": 0})
        c["n"] += 1
        c["sum_r"] += p.get("r") or 0.0
        c["wins"] += outcome(p) == "win"
    return cells


# ---------------------------------------------------------------- rule engine
def rule_breaks(positions: list[dict], rules: dict, events: list[dict] | None = None) -> list[dict]:
    """Returns flags: no SL, risk above cap, entry inside a news block, daily/weekly loss caps."""
    out = []
    before = dt.timedelta(minutes=rules.get("news_block_before_min", 15))
    after = dt.timedelta(minutes=rules.get("news_block_after_min", 15))
    news_types = set(rules.get("news_events", ["NFP", "CPI", "FOMC"]))
    news = [e for e in (events or []) if e["ev"] in news_types]
    cap = rules.get("risk_cap_pct")
    for p in positions:
        day = (close_date(p) or dt.date.min).isoformat()
        if not p.get("sl"):
            out.append(_flag("no_sl", p, day, "Trade without stop-loss", "Lệnh không có stop-loss"))
        if cap is not None and p.get("risk_pct") is not None and p["risk_pct"] > cap + 1e-9:
            out.append(_flag("risk_cap", p, day, f"Risk {p['risk_pct']:.2f}% > cap {cap}%",
                             f"Rủi ro {p['risk_pct']:.2f}% > giới hạn {cap}%"))
        t_in = _utc(p.get("t_in"))
        if t_in:
            for e in news:
                if e["t_utc"] - before <= t_in <= e["t_utc"] + after:
                    out.append(_flag("news", p, day, f"Entry inside {e['ev']} news block ({e['date']})",
                                     f"Vào lệnh trong vùng tin {e['ev']} ({e['date']})"))
                    break
    daily: dict[str, float] = {}
    weekly: dict[str, float] = {}
    for p in positions:
        d = close_date(p)
        if d is None or p.get("pl_pct") is None:
            continue
        daily[d.isoformat()] = daily.get(d.isoformat(), 0.0) + p["pl_pct"]
        y, w, _ = d.isocalendar()
        wk = f"{y}-W{w:02d}"
        weekly[wk] = weekly.get(wk, 0.0) + p["pl_pct"]
    dcap, wcap = rules.get("daily_loss_cap_pct"), rules.get("weekly_loss_cap_pct")
    for day, v in sorted(daily.items()):
        if dcap is not None and v <= -dcap:
            out.append({"rule": "daily_cap", "key": f"daily_cap:{day}", "day": day, "pos": None,
                        "en": f"Closed P/L {v:+.2f}% on {day} breaks the daily cap -{dcap}%",
                        "vi": f"Lãi/lỗ đã chốt {v:+.2f}% ngày {day} vượt giới hạn ngày -{dcap}%"})
    for wk, v in sorted(weekly.items()):
        if wcap is not None and v <= -wcap:
            out.append({"rule": "weekly_cap", "key": f"weekly_cap:{wk}", "day": wk, "pos": None,
                        "en": f"Closed P/L {v:+.2f}% in {wk} breaks the weekly cap -{wcap}%",
                        "vi": f"Lãi/lỗ đã chốt {v:+.2f}% tuần {wk} vượt giới hạn tuần -{wcap}%"})
    return out


def _flag(rule: str, p: dict, day: str, en: str, vi: str) -> dict:
    return {"rule": rule, "key": f"{rule}:{p['pos']}", "day": day, "pos": p["pos"],
            "en": f"{en} (pos {p['pos']})", "vi": f"{vi} (pos {p['pos']})"}
