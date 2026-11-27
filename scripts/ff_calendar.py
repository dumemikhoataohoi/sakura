"""Best-effort weekly economic calendar (free public JSON feed) -> data/calendar/ff_week.json.

Keeps USD events of High/Medium impact (all events that typically move gold), in UTC, sorted, pretty-printed.
Failure is never fatal: the hand-maintained data/calendar/events_us.csv stays the authoritative NFP/CPI/FOMC list.

    python scripts/ff_calendar.py            # fetch and write if changed
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
KEEP_IMPACT = {"High", "Medium"}
KEEP_COUNTRY = {"USD"}


def parse(feed: list, now: dt.datetime) -> dict:
    out = []
    for ev in feed if isinstance(feed, list) else []:
        if not isinstance(ev, dict):
            continue
        if ev.get("country") not in KEEP_COUNTRY or ev.get("impact") not in KEEP_IMPACT:
            continue
        try:
            t = dt.datetime.fromisoformat(str(ev.get("date"))).astimezone(dt.timezone.utc)
        except ValueError:
            continue
        title = " ".join(str(ev.get("title", "")).split())[:80]
        if not title:
            continue
        out.append({"utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "t": int(t.timestamp()), "title": title,
                    "impact": ev["impact"], "forecast": str(ev.get("forecast") or "")[:20],
                    "previous": str(ev.get("previous") or "")[:20]})
    out.sort(key=lambda e: (e["t"], e["title"]))
    return {"src": "ff_calendar_thisweek", "fetched_day": now.date().isoformat(), "events": out}


def write_if_changed(path: Path, obj: dict) -> bool:
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("events") == obj.get("events"):
            return False                                   # same events: no commit noise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        import requests
        resp = requests.get(FEED_URL, timeout=20, headers={"User-Agent": "sakura-data-bot/1.0"})
        resp.raise_for_status()
        obj = parse(resp.json(), dt.datetime.now(dt.timezone.utc))
    except Exception as exc:  # network / format: keep the old file
        print(f"ff_calendar: fetch failed: {exc}"[:300])
        return 0
    changed = write_if_changed(root / "data" / "calendar" / "ff_week.json", obj)
    print(f"ff_calendar: {len(obj['events'])} USD high/medium events, changed={changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
