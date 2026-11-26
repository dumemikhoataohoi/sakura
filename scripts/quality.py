"""Data-quality guards for latest.json.

Levels:
* error -> the run must not overwrite data/latest.json (nor the history of that series);
           data/quality.json records the problem and alerts.yml posts it.
* warn  -> latest.json is still written (values stay causal, the EA checks 'age'),
           but the issue is reported.
"""
from __future__ import annotations

REQUIRED_TOP = {"schema": str, "v": int, "day": str, "generated_utc": str, "generated_unix": int,
                "asof": str, "stale_after_h": (int, float), "series": dict, "events": dict}
REQUIRED_SERIES = {"d": str, "v": (int, float), "age": int}


def _issue(sid: str, kind: str, level: str, msg: str) -> dict:
    return {"sid": sid, "kind": kind, "level": level, "msg": msg}


def check_series(sid: str, summary: dict | None, qcfg: dict) -> list[dict]:
    if summary is None:
        return [_issue(sid, "missing", "warn", f"{sid}: no observation before the as-of day")]
    issues = []
    max_age = qcfg.get("max_age_days", {})
    limit = max_age.get(sid, max_age.get("default", 7))
    if summary.get("age", 0) > limit:
        issues.append(_issue(sid, "stale", "warn", f"{sid}: last observation {summary['d']} is {summary['age']} days old (> {limit})"))
    rng = qcfg.get("range", {}).get(sid)
    v = summary.get("v")
    if rng and v is not None and not (rng[0] <= v <= rng[1]):
        issues.append(_issue(sid, "range", "error", f"{sid}: value {v} outside [{rng[0]}, {rng[1]}]"))
    c1 = summary.get("c1")
    lim_abs = qcfg.get("max_abs_c1", {}).get(sid)
    if lim_abs is not None and c1 is not None and abs(c1) > lim_abs:
        issues.append(_issue(sid, "spike", "error", f"{sid}: 1-observation change {c1} exceeds +-{lim_abs}"))
    c1p = summary.get("c1p")
    lim_pct = qcfg.get("max_abs_c1p", {}).get(sid)
    if lim_pct is not None and c1p is not None and abs(c1p) > lim_pct:
        issues.append(_issue(sid, "spike", "error", f"{sid}: 1-observation change {c1p}% exceeds +-{lim_pct}%"))
    return issues


def check_schema(latest: dict) -> list[dict]:
    issues = []
    for key, typ in REQUIRED_TOP.items():
        if key not in latest:
            issues.append(_issue("*", "schema", "error", f"missing top-level key '{key}'"))
        elif not isinstance(latest[key], typ):
            issues.append(_issue("*", "schema", "error", f"key '{key}' has wrong type"))
    for sid, s in (latest.get("series") or {}).items():
        for key, typ in REQUIRED_SERIES.items():
            if not isinstance(s.get(key), typ):
                issues.append(_issue(sid, "schema", "error", f"series {sid}: bad or missing '{key}'"))
    ev = latest.get("events") or {}
    if not isinstance(ev.get("next"), list):
        issues.append(_issue("*", "schema", "error", "events.next must be a list"))
    return issues


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)
