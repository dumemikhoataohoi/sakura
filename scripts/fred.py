"""FRED daily series: fetch (public CSV endpoint, no API key), history merge, D-1 summary."""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

import vault
from zumi_common import rnd

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

Row = tuple[str, float]  # (YYYY-MM-DD, value)


def fetch_series_csv(series_id: str, start: str, session, timeout: float = 30) -> str:
    """Download one series. ``session`` is a requests.Session (or a test double)."""
    resp = session.get(
        FRED_CSV_URL,
        params={"id": series_id, "cosd": start},
        timeout=timeout,
        headers={"User-Agent": "sakura-data-bot/1.0"},
    )
    resp.raise_for_status()
    text = resp.text
    if not text.lstrip().lower().startswith(("observation_date", "date")):
        raise ValueError(f"{series_id}: unexpected FRED response (not CSV)")
    return text


def parse_fred_csv(text: str, series_id: str) -> list[Row]:
    """Parse FRED CSV. Blank cells and '.' mean 'no observation' (holidays) and are skipped."""
    rows: list[Row] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ValueError(f"{series_id}: empty or malformed CSV")
    for rec in reader:
        if len(rec) < 2:
            continue
        date_s, val_s = rec[0].strip(), rec[1].strip()
        if not date_s or val_s in ("", "."):
            continue
        try:
            dt.date.fromisoformat(date_s)
            rows.append((date_s, float(val_s)))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def read_history(path: Path) -> list[Row]:
    if not vault.exists(path):
        return []
    rows: list[Row] = []
    reader = csv.reader(io.StringIO(vault.read_text(path)))   # plaintext unless a vault glob selects data/history
    next(reader, None)
    for rec in reader:
        if len(rec) >= 2 and rec[1] not in ("", "."):
            rows.append((rec[0], float(rec[1])))
    rows.sort(key=lambda r: r[0])
    return rows


def merge_rows(old: list[Row], new: list[Row]) -> list[Row]:
    """New values win for the same date (FRED revisions); old-only dates are kept."""
    merged = dict(old)
    merged.update(dict(new))
    return sorted(merged.items(), key=lambda r: r[0])


def format_history(rows: list[Row]) -> str:
    out = ["date,value"]
    out.extend(f"{d},{_fmt(v)}" for d, v in rows)
    return "\n".join(out) + "\n"


def _fmt(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def write_history_if_changed(path: Path, rows: list[Row]) -> bool:
    return vault.write_text(path, format_history(rows))


def summarize(rows: list[Row], asof: dt.date) -> dict | None:
    """Causal summary: only observations dated strictly before ``asof`` (the 'D-1' rule).

    d    observation date of the value used
    v    value, p previous observation
    c1   v - p, c1p percent change vs p, c5 change vs 5 observations back
    age  calendar days between d and asof (1 = yesterday)
    """
    cutoff = asof.isoformat()
    usable = [r for r in rows if r[0] < cutoff]
    if not usable:
        return None
    d, v = usable[-1]
    out: dict = {"d": d, "v": rnd(v)}
    if len(usable) >= 2:
        p = usable[-2][1]
        out["p"] = rnd(p)
        out["c1"] = rnd(v - p)
        out["c1p"] = rnd((v / p - 1.0) * 100.0, 3) if p else None
    if len(usable) >= 6:
        out["c5"] = rnd(v - usable[-6][1])
    out["age"] = (asof - dt.date.fromisoformat(d)).days
    return out
