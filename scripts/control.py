#!/usr/bin/env python3
"""Control channel: control/control.json (schema v1) written by Claude, read by the EA.

Directives can pause, reduce, boost or (re-)enable method CODES (M###, never names), scale risk
globally, add news blocks and, when the EA allows it, add short news-allow windows. Hard limits
live in the EA and are never overridable from here: the EA clamps every multiplier to its own
input InpGhMaxRiskMult (default 1.0), keeps its max risk per trade, its daily/weekly loss stops
and its crisis layer, never opens/closes trades and never touches SL/TP.

Safety rules enforced here (and again in the EA):
* every directive expires: default 48 h after updated_utc, at most 7 days;
* a missing, invalid, expired or stale file (updated_utc older than 72 h) means NEUTRAL;
* seq must increase with every change;
* public text only: codes, no method names, no prices.

Usage:
  python scripts/control.py normalize [--now ISO]   # after editing: clamp, fill defaults, check seq, log CHANGELOG
  python scripts/control.py check [--prev-ref REF] [--alert-on-fail]   # used by control.yml
  python scripts/control.py effective [--now ISO]   # what the EA applies right now
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zumi_common import dumps_pretty, iso_z, mask_secrets, parse_utc, repo_root, utc_now, write_text_atomic  # noqa: E402

VERSION = 1
CONTROL_PATH = "control/control.json"
CHANGELOG_PATH = "control/CHANGELOG.md"
MAX_MULT = 2.0                    # schema bound; the EA clamps further to InpGhMaxRiskMult
DEFAULT_TTL = dt.timedelta(hours=48)
MAX_TTL = dt.timedelta(days=7)
STALE_AFTER = dt.timedelta(hours=72)
MAX_FUTURE_SKEW = dt.timedelta(minutes=10)
MAX_BLOCK = dt.timedelta(hours=24)
MAX_ALLOW = dt.timedelta(hours=4)
MAX_METHODS, MAX_WINDOWS, MAX_PHASES = 100, 50, 10
MODES = ("on", "pause", "reduce", "boost")
REASON_CODES = {
    "R00": "neutral / no specific reason",
    "R01": "method losing streak",
    "R02": "phase mismatch / weak record in this phase",
    "R03": "news event",
    "R04": "volatility shock",
    "R05": "drawdown guard",
    "R06": "data quality",
    "R07": "opportunity / strong phase fit",
    "R08": "method re-enable after recovery",
    "R09": "direction bias from multi-timeframe confluence",
}
METHOD_RE = re.compile(r"^M\d{3}$")
PHASE_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
PRICE_RE = re.compile(r"\d+[.,]\d+|\b(?!20\d\d\b)\d{4,}\b")
TOP_KEYS = {"v", "author", "seq", "updated_utc", "updated_unix", "global", "methods", "news_blocks", "notes_vi"}
GLOBAL_KEYS = {"risk_mult", "pause_all", "open_risk_pct", "side", "expires_utc", "expires_unix", "reason_code"}
SIDES = ("both", "buy", "sell", "auto")   # global.side: entry side the EA may open (restrict only); auto = EA multi-TF confluence decides per bar
OPEN_RISK_MIN, OPEN_RISK_MAX = 1.0, 20.0   # % of balance; the EA applies min(its local cap, this)
METHOD_KEYS = {"mode", "risk_mult", "phases", "reason_code", "expires_utc", "expires_unix"}
WINDOW_KEYS = {"kind", "start_utc", "start_unix", "end_utc", "end_unix", "reason_code"}


class ControlError(ValueError):
    pass


def neutral_file(now: dt.datetime, seq: int = 1, note: str = "Trạng thái trung tính: không có chỉ thị.") -> dict:
    return normalize({"v": VERSION, "author": "claude", "seq": seq, "updated_utc": iso_z(now),
                      "global": {"risk_mult": 1.0, "pause_all": False}, "methods": {}, "news_blocks": [],
                      "notes_vi": note})[0]


def _time(value, field: str, errors: list) -> dt.datetime | None:
    try:
        return parse_utc(str(value))
    except (TypeError, ValueError):
        errors.append(f"{field}: not an ISO-8601 UTC time")
        return None


def _mult(value, field: str, errors: list, warnings: list) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field}: must be a number")
        return None
    v = float(value)
    if v < 0 or v > MAX_MULT:
        warnings.append(f"{field}: {v} clamped to [0, {MAX_MULT}]")
        v = min(max(v, 0.0), MAX_MULT)
    return round(v, 3)


def _expiry(raw, base: dt.datetime, field: str, errors: list, warnings: list) -> dt.datetime | None:
    if raw is None:
        return base + DEFAULT_TTL
    t = _time(raw, field, errors)
    if t is None:
        return None
    if t <= base:
        errors.append(f"{field}: must be after updated_utc")
        return None
    if t > base + MAX_TTL:
        warnings.append(f"{field}: clamped to 7 days after updated_utc")
        t = base + MAX_TTL
    return t


def _reason(raw, field: str, errors: list, required: bool = True) -> str:
    code = str(raw or ("" if required else "R00"))
    if code not in REASON_CODES:
        errors.append(f"{field}: unknown reason code {code!r} (see control/reason_codes.md)")
    return code


def normalize(obj, now: dt.datetime | None = None) -> tuple[dict | None, list[str], list[str]]:
    """Validate and clamp. Returns (normalized or None, errors, warnings). Any error = invalid = NEUTRAL."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(obj, dict):
        return None, ["control file must be a JSON object"], warnings
    for k in obj:
        if k not in TOP_KEYS:
            errors.append(f"unknown top-level key {k!r}")
    if obj.get("v") != VERSION:
        errors.append(f"v must be {VERSION}")
    if obj.get("author") != "claude":
        errors.append("author must be 'claude'")
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        errors.append("seq must be an integer >= 1")
    updated = _time(obj.get("updated_utc"), "updated_utc", errors)
    if updated and now and updated > now + MAX_FUTURE_SKEW:
        errors.append("updated_utc is in the future")
    if updated is None:
        return None, errors, warnings
    out: dict = {"v": VERSION, "author": "claude", "seq": seq, "updated_utc": iso_z(updated),
                 "updated_unix": int(updated.timestamp())}

    g = obj.get("global", {})
    if not isinstance(g, dict):
        errors.append("global must be an object")
        g = {}
    for k in g:
        if k not in GLOBAL_KEYS:
            errors.append(f"global: unknown key {k!r}")
    gm = _mult(g.get("risk_mult", 1.0), "global.risk_mult", errors, warnings)
    pause_all = g.get("pause_all", False)
    if not isinstance(pause_all, bool):
        errors.append("global.pause_all must be true/false")
    orp = g.get("open_risk_pct")
    if orp is not None:
        if isinstance(orp, bool) or not isinstance(orp, (int, float)) or not OPEN_RISK_MIN <= orp <= OPEN_RISK_MAX:
            errors.append(f"global.open_risk_pct must be a number in [{OPEN_RISK_MIN}, {OPEN_RISK_MAX}]")
            orp = None
        else:
            orp = round(float(orp), 2)
    side = g.get("side", "both")
    if side not in SIDES:
        errors.append(f"global.side must be one of {', '.join(SIDES)}")
        side = "both"
    gexp = _expiry(g.get("expires_utc"), updated, "global.expires_utc", errors, warnings)
    non_neutral = (gm is not None and gm != 1.0) or pause_all is True or orp is not None or side != "both"
    greason = _reason(g.get("reason_code"), "global.reason_code", errors, required=non_neutral)
    out["global"] = {"risk_mult": gm, "pause_all": pause_all is True, "reason_code": greason,
                     "expires_utc": iso_z(gexp) if gexp else None,
                     "expires_unix": int(gexp.timestamp()) if gexp else None}
    if orp is not None:
        out["global"]["open_risk_pct"] = orp
    if side != "both":
        out["global"]["side"] = side

    methods = obj.get("methods", {})
    if not isinstance(methods, dict):
        errors.append("methods must be an object keyed by method code")
        methods = {}
    if len(methods) > MAX_METHODS:
        errors.append(f"at most {MAX_METHODS} method directives")
    out["methods"] = {}
    for code in sorted(methods):
        d = methods[code]
        f = f"methods.{code}"
        if not METHOD_RE.match(str(code)):
            errors.append(f"{f}: keys must be method codes M### (never method names)")
            continue
        if not isinstance(d, dict):
            errors.append(f"{f}: must be an object")
            continue
        for k in d:
            if k not in METHOD_KEYS:
                errors.append(f"{f}: unknown key {k!r}")
        mode = d.get("mode")
        if mode not in MODES:
            errors.append(f"{f}.mode must be one of {', '.join(MODES)}")
            continue
        m = _mult(d.get("risk_mult", {"on": 1.0, "pause": 0.0, "reduce": 0.5, "boost": 1.25}[mode]),
                  f"{f}.risk_mult", errors, warnings)
        if m is not None:
            if mode == "pause":
                m = 0.0
            elif mode == "on":
                m = 1.0
            elif mode == "reduce" and not m < 1.0:
                errors.append(f"{f}: reduce needs risk_mult < 1")
            elif mode == "boost" and not m > 1.0:
                errors.append(f"{f}: boost needs risk_mult > 1")
        phases = d.get("phases", [])
        if not isinstance(phases, list) or len(phases) > MAX_PHASES or not all(
                isinstance(p, str) and PHASE_RE.match(p) for p in phases):
            errors.append(f"{f}.phases must be a list of up to {MAX_PHASES} labels [A-Za-z0-9_-]")
            phases = []
        exp = _expiry(d.get("expires_utc"), updated, f"{f}.expires_utc", errors, warnings)
        reason = _reason(d.get("reason_code"), f"{f}.reason_code", errors)
        out["methods"][code] = {"mode": mode, "risk_mult": m, "phases": sorted(set(phases)), "reason_code": reason,
                                "expires_utc": iso_z(exp) if exp else None,
                                "expires_unix": int(exp.timestamp()) if exp else None}

    wins = obj.get("news_blocks", [])
    if not isinstance(wins, list) or len(wins) > MAX_WINDOWS:
        errors.append(f"news_blocks must be a list of at most {MAX_WINDOWS} windows")
        wins = []
    out["news_blocks"] = []
    for i, w in enumerate(wins):
        f = f"news_blocks[{i}]"
        if not isinstance(w, dict):
            errors.append(f"{f}: must be an object")
            continue
        for k in w:
            if k not in WINDOW_KEYS:
                errors.append(f"{f}: unknown key {k!r}")
        kind = w.get("kind", "block")
        if kind not in ("block", "allow"):
            errors.append(f"{f}.kind must be 'block' or 'allow'")
            continue
        s, e = _time(w.get("start_utc"), f"{f}.start_utc", errors), _time(w.get("end_utc"), f"{f}.end_utc", errors)
        reason = _reason(w.get("reason_code"), f"{f}.reason_code", errors)
        if s is None or e is None:
            continue
        if e <= s:
            errors.append(f"{f}: end_utc must be after start_utc")
            continue
        if e - s > (MAX_ALLOW if kind == "allow" else MAX_BLOCK):
            errors.append(f"{f}: a {kind} window may last at most {4 if kind == 'allow' else 24} h")
        if e > updated + MAX_TTL:
            errors.append(f"{f}: ends more than 7 days after updated_utc")
        if kind == "allow" and reason not in ("R03", "R07"):
            errors.append(f"{f}: an allow window needs reason R03 or R07")
        out["news_blocks"].append({"kind": kind, "start_utc": iso_z(s), "start_unix": int(s.timestamp()),
                                   "end_utc": iso_z(e), "end_unix": int(e.timestamp()), "reason_code": reason})
    out["news_blocks"].sort(key=lambda x: (x["start_unix"], x["kind"]))

    note = obj.get("notes_vi", "")
    if not isinstance(note, str) or len(note) > 300:
        errors.append("notes_vi must be a string of at most 300 characters")
        note = ""
    if PRICE_RE.search(note):
        errors.append("notes_vi must not contain prices or long numbers (public text)")
    out["notes_vi"] = mask_secrets(note).replace("\n", " ").strip()
    if errors:
        return None, errors, warnings
    return out, errors, warnings


# ---------------------------------------------------------------- effective state (what the EA applies)
def effective(norm: dict | None, now: dt.datetime) -> dict:
    """NEUTRAL when missing/invalid/stale; expired directives are dropped individually."""
    neutral = {"neutral": True, "reason": "", "global": {"risk_mult": 1.0, "pause_all": False},
               "methods": {}, "news_blocks": []}
    if norm is None:
        return dict(neutral, reason="missing or invalid")
    if now - parse_utc(norm["updated_utc"]) > STALE_AFTER:
        return dict(neutral, reason="stale (updated_utc older than 72 h)")
    ts = now.timestamp()
    out = copy.deepcopy(neutral)
    out["reason"] = "active"
    g = norm["global"]
    if g["expires_unix"] and ts < g["expires_unix"]:
        out["global"] = {"risk_mult": g["risk_mult"], "pause_all": g["pause_all"]}
        if g.get("open_risk_pct") is not None:
            out["global"]["open_risk_pct"] = g["open_risk_pct"]
        if g.get("side", "both") != "both":
            out["global"]["side"] = g["side"]
    out["methods"] = {c: d for c, d in norm["methods"].items() if ts < d["expires_unix"]}
    out["news_blocks"] = [w for w in norm["news_blocks"] if ts < w["end_unix"]]
    out["neutral"] = (out["global"] == neutral["global"] and not out["methods"] and not out["news_blocks"])
    if out["neutral"]:
        out["reason"] = "no active directive"
    return out


def risk_mult(eff: dict, code: str, phase: str, cap: float = 1.0) -> float:
    """Reference implementation of the EA's ZGB_RiskMult: global x method, clamped to [0, cap]."""
    if eff["global"]["pause_all"]:
        return 0.0
    m = eff["global"]["risk_mult"]
    d = eff["methods"].get(code)
    if d and (not d["phases"] or phase in d["phases"]):
        m *= d["risk_mult"]
    return max(0.0, min(m, cap, MAX_MULT))


# ---------------------------------------------------------------- change log
def _fmt_dir(code: str, d: dict) -> str:
    ph = f" [{','.join(d['phases'])}]" if d["phases"] else ""
    return f"{code} {d['mode']} x{d['risk_mult']:.2f}{ph} until {d['expires_utc'][:16]}Z ({d['reason_code']})"


def changes(prev: dict | None, new: dict) -> list[str]:
    out = [] if prev is not None else ["initial file"]
    pg = (prev or {}).get("global") or {"risk_mult": 1.0, "pause_all": False}
    ng = new["global"]
    if pg.get("risk_mult") != ng["risk_mult"]:
        out.append(f"global risk_mult {pg.get('risk_mult', 1.0):.2f}->{ng['risk_mult']:.2f} ({ng['reason_code']})")
    if pg.get("pause_all") != ng["pause_all"]:
        out.append(f"global pause_all {str(ng['pause_all']).lower()} ({ng['reason_code']})")
    if pg.get("side", "both") != ng.get("side", "both"):
        out.append(f"global side {ng.get('side', 'both')} ({ng['reason_code']})")
    if pg.get("open_risk_pct") != ng.get("open_risk_pct"):
        v = ng.get("open_risk_pct")
        out.append(f"global open_risk_pct {'EA default' if v is None else f'{v:.2f}%'} ({ng['reason_code']})")
    pm = (prev or {}).get("methods") or {}
    for code, d in new["methods"].items():
        if code not in pm or {k: v for k, v in pm[code].items()} != d:
            out.append(_fmt_dir(code, d))
    for code in pm:
        if code not in new["methods"]:
            out.append(f"{code} directive removed (restored to the EA default)")
    pw = {(w["kind"], w["start_unix"], w["end_unix"]) for w in (prev or {}).get("news_blocks", [])}
    nw = {(w["kind"], w["start_unix"], w["end_unix"]): w for w in new["news_blocks"]}
    for key, w in nw.items():
        if key not in pw:
            out.append(f"news {w['kind']} {w['start_utc'][:16]}Z..{w['end_utc'][:16]}Z ({w['reason_code']})")
    for key in pw:
        if key not in nw:
            out.append(f"news {key[0]} window removed")
    if (prev or {}).get("notes_vi") != new["notes_vi"]:
        out.append("note updated")
    return out


def changelog_line(prev: dict | None, new: dict, now: dt.datetime) -> str:
    ch = changes(prev, new) or ["no directive change"]
    return f"- {now.strftime('%Y-%m-%dT%H:%MZ')} seq {new['seq']}: " + "; ".join(ch)


# ---------------------------------------------------------------- I/O helpers
def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def prev_from_git(root: Path, ref: str) -> dict | None:
    if not ref or set(ref) == {"0"}:
        return None
    try:
        text = subprocess.run(["git", "show", f"{ref}:{CONTROL_PATH}"], cwd=root, capture_output=True,
                              text=True, check=True).stdout
        norm, _, _ = normalize(json.loads(text))
        return norm
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return None


def check_seq(prev: dict | None, new: dict) -> list[str]:
    if prev is None:
        return []
    if new["seq"] < prev["seq"]:
        return [f"seq went backwards ({prev['seq']} -> {new['seq']})"]
    if new["seq"] == prev["seq"] and changes(prev, new):
        return [f"seq must increase when directives change (still {new['seq']})"]
    return []


def cmd_normalize(root: Path, now: dt.datetime, prev_ref: str) -> int:
    path = root / CONTROL_PATH
    norm, errors, warnings = normalize(load_json(path), now)
    prev = prev_from_git(root, prev_ref)
    if norm:
        errors += check_seq(prev, norm)
    for w in warnings:
        print(f"warning: {w}")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    write_text_atomic(path, dumps_pretty(norm))
    ch = changes(prev, norm)
    if prev is None or ch or prev["seq"] != norm["seq"]:
        log = root / CHANGELOG_PATH
        text = log.read_text(encoding="utf-8") if log.exists() else "# Control changelog\n\n"
        write_text_atomic(log, text.rstrip("\n") + "\n" + changelog_line(prev, norm, now) + "\n")
    print("normalized control.json, seq", norm["seq"], "|", "; ".join(ch) or "no directive change")
    return 0


def cmd_check(root: Path, now: dt.datetime, prev_ref: str, alert: bool) -> int:
    path = root / CONTROL_PATH
    errors, warnings = [], []
    try:
        raw = load_json(path)
        norm, errors, warnings = normalize(raw, now)
        if norm:
            errors += check_seq(prev_from_git(root, prev_ref), norm)
            if raw != norm:
                errors.append("file is not normalized: run `python scripts/control.py normalize` before committing")
    except (OSError, json.JSONDecodeError) as exc:
        errors = [f"cannot read {CONTROL_PATH}: {exc}"]
    for w in warnings:
        print(f"warning: {w}")
    if not errors:
        print("control.json valid")
        return 0
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if alert:
        post_invalid_alert(root, now, errors)
    return 1


def post_invalid_alert(root: Path, now: dt.datetime, errors: list[str]) -> None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY: alert not posted", file=sys.stderr)
        return
    import alerts
    from gh_api import GitHub
    from zumi_common import load_config
    msg = mask_secrets("; ".join(errors))[:600]
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    al = alerts.alert(f"control_invalid:{sha or now.date()}", "high",
                      f"control.json KHÔNG hợp lệ (EA dùng trạng thái TRUNG TÍNH): {msg}",
                      f"control.json is INVALID (the EA falls back to NEUTRAL): {msg}")
    alerts.post_alerts(GitHub(token, repo), load_config(root), [al], now, os.environ.get("DEFAULT_BRANCH", "main"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate / normalize control/control.json")
    ap.add_argument("command", choices=["normalize", "check", "effective"])
    ap.add_argument("--root")
    ap.add_argument("--now")
    ap.add_argument("--prev-ref", default="HEAD", help="git ref holding the previous control.json (seq check)")
    ap.add_argument("--alert-on-fail", action="store_true")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    now = parse_utc(args.now) if args.now else utc_now()
    if args.command == "normalize":
        return cmd_normalize(root, now, args.prev_ref)
    if args.command == "check":
        return cmd_check(root, now, args.prev_ref, args.alert_on_fail)
    try:
        norm, errors, _ = normalize(load_json(root / CONTROL_PATH), now)
    except (OSError, json.JSONDecodeError):
        norm, errors = None, ["unreadable"]
    out = effective(norm, now)
    if errors:
        out["errors"] = errors
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
