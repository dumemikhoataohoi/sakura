"""US high-impact event calendar (NFP / CPI / FOMC) maintained by hand in a CSV.

File format (data/calendar/events_us.csv), lines starting with '#' are comments:

    date,event,time_et,flag
    2026-12-04,NFP,08:30,uncertain

* date     release date (YYYY-MM-DD), America/New_York calendar day
* event    short code: NFP, CPI, FOMC (any other code is passed through)
* time_et  release time in America/New_York (DST handled here, not in the file)
* flag     empty = confirmed against the official schedule, 'uncertain' = not yet confirmed
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from zumi_common import UTC, iso_z

NY = ZoneInfo("America/New_York")


def load_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    for rec in reader:
        date_s = (rec.get("date") or "").strip()
        ev = (rec.get("event") or "").strip().upper()
        time_s = (rec.get("time_et") or "").strip() or "08:30"
        if not date_s or not ev:
            continue
        local = dt.datetime.fromisoformat(f"{date_s}T{time_s}").replace(tzinfo=NY)
        t_utc = local.astimezone(UTC)
        events.append(
            {
                "date": date_s,
                "ev": ev,
                "time_et": time_s,
                "t_utc": t_utc,
                "unc": 1 if (rec.get("flag") or "").strip().lower() == "uncertain" else 0,
            }
        )
    events.sort(key=lambda e: e["t_utc"])
    return events


def _public(e: dict) -> dict:
    return {"ev": e["ev"], "utc": iso_z(e["t_utc"]), "t": int(e["t_utc"].timestamp()), "unc": e["unc"]}


def calendar_block(events: list[dict], now: dt.datetime, next_n: int = 6, recent_h: float = 24) -> dict:
    """'next' = upcoming releases after ``now``; 'recent' = releases in the last ``recent_h`` hours.

    Only absolute UTC times are written, so the block changes only when an event passes,
    which keeps commits rare. The EA compares ``t`` (unix seconds, UTC) with TimeGMT().
    """
    upcoming = [e for e in events if e["t_utc"] > now][:next_n]
    since = now - dt.timedelta(hours=recent_h)
    recent = [e for e in events if since <= e["t_utc"] <= now]
    last_date = events[-1]["date"] if events else None
    return {
        "next": [_public(e) for e in upcoming],
        "recent": [_public(e) for e in recent],
        "covered_until": last_date,
    }
