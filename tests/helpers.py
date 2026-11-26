"""Test helpers: an isolated temp repo root and fake HTTP sessions (no network)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(REPO / "scripts"))


def make_root(events: str = "events_small.csv") -> Path:
    root = Path(tempfile.mkdtemp(prefix="sakura_test_"))
    shutil.copytree(REPO / "config", root / "config")
    (root / "data" / "calendar").mkdir(parents=True)
    shutil.copy(FIXTURES / events, root / "data" / "calendar" / "events_us.csv")
    return root


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


class FakeResponse:
    def __init__(self, status: int = 200, text: str = "", payload=None, links=None):
        self.status_code = status
        self.text = text if payload is None else json.dumps(payload)
        self._payload = payload
        self.links = links or {}

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def synthetic_fred(series_id: str, start_value: float, n: int = 30, step: float = 0.01,
                   last_date: str = "2026-11-25") -> str:
    """Business-day series ending at last_date."""
    import datetime as dt
    end = dt.date.fromisoformat(last_date)
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    days.reverse()
    lines = [f"observation_date,{series_id}"]
    for i, day in enumerate(days):
        lines.append(f"{day.isoformat()},{start_value + i * step:.4f}")
    return "\n".join(lines) + "\n"


class FakeDataSession:
    """Serves synthetic FRED CSVs by series id; stooq fails unless gold_text is given."""

    BASE = {"DGS10": 4.5, "DGS2": 4.0, "DFII10": 2.0, "T10YIE": 2.2, "DTWEXBGS": 120.0,
            "VIXCLS": 18.0, "DCOILWTICO": 75.0, "SP500": 6500.0}

    def __init__(self, overrides: dict | None = None, gold_text: str | None = None, fail: set | None = None):
        self.overrides = overrides or {}
        self.gold_text = gold_text
        self.fail = fail or set()
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        if "stooq" in url:
            if self.gold_text is None:
                raise ConnectionError("stooq unreachable (test)")
            return FakeResponse(200, self.gold_text)
        sid = params["id"]
        if sid in self.fail:
            raise ConnectionError(f"{sid} down (test)")
        if sid in self.overrides:
            return FakeResponse(200, self.overrides[sid])
        return FakeResponse(200, synthetic_fred(sid, self.BASE[sid]))
