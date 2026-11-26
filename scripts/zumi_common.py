"""Shared helpers for the sakura / Zumi GitHub data bot.

Only the Python standard library is used here. Network code lives in the
fetch modules and takes an injectable ``session`` so tests run offline.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc


def repo_root(arg: str | None = None) -> Path:
    return Path(arg).resolve() if arg else ROOT


def load_config(root: Path) -> dict:
    with open(root / "config" / "settings.json", encoding="utf-8") as fh:
        return json.load(fh)


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def parse_utc(text: str) -> dt.datetime:
    """Parse '2026-11-26T10:00:00Z' (or an ISO string with offset) to aware UTC."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text[:10])


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; keep normal repo file mode
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dumps_compact(obj: Any) -> str:
    # ensure_ascii keeps the file pure ASCII, which keeps the MQL5 parser simple.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


def dumps_pretty(obj: Any) -> str:
    """Pretty, key-sorted, ASCII JSON: stable output gives small, readable git diffs."""
    return json.dumps(obj, indent=2, ensure_ascii=True, sort_keys=True) + "\n"


# Secrets that must never reach a public file, a log line or a commit message.
_SECRET_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[^\s\"']+"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s\"']+"),
]


def mask_secrets(text: Any) -> Any:
    """Replace anything that looks like a token or webhook URL with '***'."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + "***", out)
        else:
            out = pat.sub("***", out)
    return out


def rnd(value: float | None, nd: int = 4) -> float | None:
    if value is None:
        return None
    out = round(float(value), nd)
    return 0.0 if out == 0 else out  # avoid "-0.0"


def set_github_output(name: str, value: str) -> None:
    """Write a step output when running inside GitHub Actions (no-op otherwise)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")
