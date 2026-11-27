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


# ---------------------------------------------------------------- multi-timeframe daily/weekly context (best effort)
def _ema(vals: list[float], n: int) -> list[float]:
    k, out = 2.0 / (n + 1), []
    for v in vals:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def _rsi(vals: list[float], n: int = 14) -> float | None:
    if len(vals) < n + 1:
        return None
    up = dn = 0.0
    for i in range(1, n + 1):                       # Wilder seed
        d = vals[i] - vals[i - 1]
        up += max(d, 0.0)
        dn += max(-d, 0.0)
    up, dn = up / n, dn / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        up = (up * (n - 1) + max(d, 0.0)) / n
        dn = (dn * (n - 1) + max(-d, 0.0)) / n
    if dn == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / dn)


def _weekly(bars: list[Bar]) -> list[Bar]:
    weeks: dict[tuple[int, int], list[Bar]] = {}
    for b in bars:
        y, w, _ = dt.date.fromisoformat(b[0]).isocalendar()
        weeks.setdefault((y, w), []).append(b)
    out = []
    for key in sorted(weeks):
        wb = weeks[key]
        out.append((wb[0][0], wb[0][1], max(x[2] for x in wb), min(x[3] for x in wb), wb[-1][4]))
    return out


def candle_code(prev: Bar, cur: Bar) -> str:
    """Pattern of the last closed candle vs the one before (fixed vocabulary)."""
    _, po, ph, pl, pc = prev
    _, o, h, lo, c = cur
    rng, body = h - lo, abs(c - o)
    if rng <= 0:
        return "NONE"
    up_w, dn_w = h - max(o, c), min(o, c) - lo
    if h > ph and c < ph and up_w >= 0.5 * rng:
        return "SWEEP_HI"
    if lo < pl and c > pl and dn_w >= 0.5 * rng:
        return "SWEEP_LO"
    if dn_w >= 2 * max(body, 1e-9) and dn_w >= 0.6 * rng:
        return "PIN_BULL"
    if up_w >= 2 * max(body, 1e-9) and up_w >= 0.6 * rng:
        return "PIN_BEAR"
    if c > o and pc < po and c >= po and o <= pc:
        return "ENGULF_BULL"
    if c < o and pc > po and c <= po and o >= pc:
        return "ENGULF_BEAR"
    if h <= ph and lo >= pl:
        return "INSIDE"
    if h > ph and lo < pl:
        return "OUTSIDE"
    if body <= 0.1 * rng:
        return "DOJI"
    return "BULL" if c > o else "BEAR"


def _tf_block(bars: list[Bar]) -> dict | None:
    if len(bars) < 60:
        return None
    closes = [b[4] for b in bars]
    e20, e50 = _ema(closes, 20)[-1], _ema(closes, 50)[-1]
    a = atr(bars)
    c = closes[-1]
    hi20, lo20 = max(b[2] for b in bars[-20:]), min(b[3] for b in bars[-20:])
    out = {"dir": 1 if c > e20 > e50 else (-1 if c < e20 < e50 else 0),
           "e20": 1 if c > e20 else -1, "e50": 1 if c > e50 else -1,
           "rsi": int(round(_rsi(closes) or 50)), "pat": candle_code(bars[-2], bars[-1])}
    if a:
        out["hi20_atr"] = rnd((hi20 - c) / a, 2)
        out["lo20_atr"] = rnd((c - lo20) / a, 2)
    return out


def context(bars: list[Bar], asof: dt.date) -> dict | None:
    """D1 and W1 trend / RSI / candle pattern / distance to 20-bar range, closed bars before ``asof`` only."""
    usable = [b for b in bars if b[0] < asof.isoformat()]
    d1 = _tf_block(usable)
    if d1 is None:
        return None
    out = {"d1": d1}
    w = _weekly(usable)
    cur_week = asof.isocalendar()[:2]
    if w and dt.date.fromisoformat(w[-1][0]).isocalendar()[:2] == cur_week:
        w = w[:-1]                                  # current week not closed
    w1 = _tf_block(w)
    if w1:
        out["w1"] = w1
    return out
