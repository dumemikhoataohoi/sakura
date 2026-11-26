"""Optional gold (XAUUSD) daily context from stooq's free CSV download.

Failure is never fatal: the bot then keeps using data/history/XAUUSD.csv as it is.
Note: stooq availability from GitHub runners is not guaranteed (it was not reachable
from the build sandbox), so treat this block as best effort.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from zumi_common import rnd, write_text_atomic

STOOQ_URL = "https://stooq.com/q/d/l/"

Bar = tuple[str, float, float, float, float]  # date, open, high, low, close


def fetch_stooq_csv(symbol: str, start: str, session, timeout: float = 20) -> str:
    params = {"s": symbol, "i": "d", "d1": start.replace("-", "")}
    resp = session.get(STOOQ_URL, params=params, timeout=timeout,
                       headers={"User-Agent": "sakura-data-bot/1.0"})
    resp.raise_for_status()
    text = resp.text
    if not text.lstrip().lower().startswith("date"):
        raise ValueError("stooq: unexpected response (not CSV)")
    return text


def parse_ohlc_csv(text: str) -> list[Bar]:
    bars: list[Bar] = []
    reader = csv.DictReader(io.StringIO(text))
    for rec in reader:
        rec = {k.strip().lower(): (v or "").strip() for k, v in rec.items() if k}
        try:
            d = rec["date"]
            dt.date.fromisoformat(d)
            bars.append((d, float(rec["open"]), float(rec["high"]), float(rec["low"]), float(rec["close"])))
        except (KeyError, ValueError):
            continue
    bars.sort(key=lambda b: b[0])
    return bars


def read_bars(path: Path) -> list[Bar]:
    if not path.exists():
        return []
    return parse_ohlc_csv(path.read_text(encoding="utf-8"))


def merge_bars(old: list[Bar], new: list[Bar]) -> list[Bar]:
    merged = {b[0]: b for b in old}
    merged.update({b[0]: b for b in new})
    return [merged[k] for k in sorted(merged)]


def write_bars_if_changed(path: Path, bars: list[Bar]) -> bool:
    lines = ["date,open,high,low,close"]
    lines += [f"{d},{o:g},{h:g},{lo:g},{c:g}" for d, o, h, lo, c in bars]
    text = "\n".join(lines) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    write_text_atomic(path, text)
    return True


def atr(bars: list[Bar], n: int = 14) -> float | None:
    """Simple mean of the last ``n`` true ranges of ``bars``."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        _, _, h, lo, _ = bars[i]
        pc = bars[i - 1][4]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs) / n


def summarize(bars: list[Bar], asof: dt.date) -> dict | None:
    """D-1 gold context. move_atr = last daily change / ATR14 known *before* that day."""
    usable = [b for b in bars if b[0] < asof.isoformat()]
    if len(usable) < 2:
        return None
    d, _, h, lo, c = usable[-1]
    pc = usable[-2][4]
    out = {"src": "stooq", "d": d, "c": rnd(c, 2), "c1": rnd(c - pc, 2),
           "c1p": rnd((c / pc - 1) * 100, 3), "rng": rnd(h - lo, 2),
           "age": (asof - dt.date.fromisoformat(d)).days}
    a_prev = atr(usable[:-1])
    if a_prev:
        out["atr14"] = rnd(a_prev, 2)
        out["move_atr"] = rnd((c - pc) / a_prev, 3)
    return out
