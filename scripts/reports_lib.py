"""EA report payloads: validation/sanitising, raw storage paths, deal journal, positions.

Privacy rules enforced here (defence in depth; the EA already follows them):
* the account is identified only by a hash ('acct'); a clear login is hashed and dropped;
* money is stored only as % of balance: absolute amounts (balance, equity, profit...) are dropped;
* every string is passed through mask_secrets() and truncated.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

import vault
from zumi_common import UTC, dumps_pretty, mask_secrets

HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")
FLAG_KEY_RE = re.compile(r"[^a-z0-9_:.-]")
FORBIDDEN_KEYS = {"login", "account", "account_number", "accountnumber", "acc", "email", "name", "server",
                  "balance", "equity", "margin", "free_margin", "profit", "pl", "money", "token", "password"}
TRADE_KEYS = {"deal": int, "pos": int, "sym": str, "side": str, "vol": float, "vol_in": float, "t_in": int,
              "t_out": int, "in": float, "out": float, "sl": float, "tp": float, "r": float, "pl_pct": float,
              "risk_pct": float, "cmt": str, "magic": int, "phase": str, "reason": str, "mae_r": float,
              "mfe_r": float}
EQ_KEYS = {"day_pl_pct", "eq_day_pct", "float_pct", "margin_level", "dd_day_pct", "week_pl_pct"}
MKT_KEYS = {"sym": str, "bid": float, "pc": float, "d1_open": float, "atr": float, "move": float,
            "move_atr": float, "spread": float}


class PayloadError(ValueError):
    pass


# ---------------------------------------------------------------- event / payload
def payload_from_event(event: dict) -> dict:
    """repository_dispatch -> client_payload; workflow_dispatch -> inputs.payload (JSON string)."""
    if isinstance(event.get("client_payload"), dict):
        return event["client_payload"]
    raw = (event.get("inputs") or {}).get("payload")
    if raw:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"inputs.payload is not valid JSON: {exc}") from None
        if isinstance(obj, dict) and isinstance(obj.get("client_payload"), dict):
            obj = obj["client_payload"]
        if isinstance(obj, dict):
            return obj
    raise PayloadError("no client_payload / inputs.payload in the event")


def account_hash(clear: str, salt: str = "zumi") -> str:
    return hashlib.sha256(f"{salt}:{clear}".encode()).hexdigest()[:16]


def _num(value, typ):
    if isinstance(value, bool):
        return None
    try:
        out = typ(value)
    except (TypeError, ValueError):
        return None
    if isinstance(out, float) and not math.isfinite(out):
        return None
    return out


def _str(value, max_len: int) -> str:
    return mask_secrets(str(value)).replace("\r", " ")[:max_len]


def _strip_forbidden(obj):
    if isinstance(obj, dict):
        return {k: _strip_forbidden(v) for k, v in obj.items() if str(k).lower() not in FORBIDDEN_KEYS}
    if isinstance(obj, list):
        return [_strip_forbidden(v) for v in obj]
    return obj


def _acct_of(payload: dict) -> str:
    acct = str(payload.get("acct", "")).lower()
    clear = next((payload[k] for k in ("login", "account", "account_number") if payload.get(k)), None)
    if not HASH_RE.match(acct):
        acct = account_hash(str(clear)) if clear else "unknown"
    return acct[:16]


def is_per_trade(payload: dict) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("kind", "ea_report") == "ea_report" or bool(data.get("trades"))


def sanitize_any(payload: dict, rcfg: dict, now: dt.datetime) -> tuple[dict, list[str]]:
    """Entry point. v2 aggregates ('ea_agg') are always accepted (whitelist-validated).
    v1 per-trade reports ('ea_report') are REJECTED while reports.public_mode is true."""
    import agg
    if not isinstance(payload, dict):
        raise PayloadError("payload must be an object")
    kind = payload.get("kind")
    if kind == "ea_agg" or (kind is None and payload.get("v") == 2):
        try:
            return agg.sanitize_v2(payload, rcfg, now, _acct_of(payload))
        except agg.AggError as exc:
            raise PayloadError(str(exc)) from None
    if kind not in (None, "ea_report"):
        raise PayloadError(f"unsupported kind {kind!r}")
    if rcfg.get("public_mode", True):
        raise PayloadError("per-trade report (v1 'ea_report') rejected: the repository is public. "
                           "Use aggregate reports (EA input InpGhReportMode=0), or set "
                           "reports.public_mode=false only for a PRIVATE repository")
    return sanitize(payload, rcfg, now), []


def sanitize(payload: dict, rcfg: dict, now: dt.datetime) -> dict:
    """v1 per-trade report (detail mode, private repositories only)."""
    if not isinstance(payload, dict):
        raise PayloadError("payload must be an object")
    kind = payload.get("kind", "ea_report")
    if kind != "ea_report":
        raise PayloadError(f"unsupported kind {kind!r}")
    max_len = int(rcfg.get("max_str_len", 400))
    acct = _acct_of(payload)

    ts = _num(payload.get("ts"), int) or int(now.timestamp())
    # Reject timestamps far in the future / past (clock bugs) by clamping to "now".
    if abs(ts - now.timestamp()) > 400 * 86400:
        ts = int(now.timestamp())
    part = max(1, _num(payload.get("part"), int) or 1)
    parts = max(part, _num(payload.get("parts"), int) or 1)
    rid = re.sub(r"[^A-Za-z0-9_.:-]", "", str(payload.get("rid", "")))[:64] or f"{acct}-{ts}"

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise PayloadError("data must be an object")
    data = _strip_forbidden(data)

    trades = []
    for t in (data.get("trades") or [])[: int(rcfg.get("max_trades_per_part", 500))]:
        if not isinstance(t, dict):
            continue
        clean = {}
        for key, typ in TRADE_KEYS.items():
            if key in t and t[key] is not None:
                val = _str(t[key], 64 if key != "cmt" else 96) if typ is str else _num(t[key], typ)
                if val is not None:
                    clean[key] = val
        if "deal" in clean and "pos" in clean:
            if clean.get("side") not in ("buy", "sell"):
                clean.pop("side", None)
            trades.append(clean)

    logs = []
    for item in (data.get("log") or [])[-int(rcfg.get("max_log_lines", 400)):]:
        if isinstance(item, dict) and "m" in item:
            logs.append({"t": _num(item.get("t"), int) or ts, "m": _str(item["m"], max_len)})
        elif isinstance(item, str):
            logs.append({"t": ts, "m": _str(item, max_len)})

    flags = []
    for item in (data.get("flags") or [])[: int(rcfg.get("max_flags", 50))]:
        if isinstance(item, dict) and item.get("msg"):
            key = FLAG_KEY_RE.sub("", str(item.get("key", "ea")).lower())[:48] or "ea"
            flags.append({"key": key, "msg": _str(item["msg"], 300), "t": _num(item.get("t"), int) or ts})

    eq = {k: _num(v, float) for k, v in (data.get("eq") or {}).items() if k in EQ_KEYS}
    eq = {k: v for k, v in eq.items() if v is not None}
    mkt = {}
    for key, typ in MKT_KEYS.items():
        val = (data.get("mkt") or {}).get(key)
        if val is not None:
            val = _str(val, 32) if typ is str else _num(val, typ)
            if val is not None:
                mkt[key] = val
    if isinstance(data.get("mkt"), dict) and data["mkt"].get("ctx") is not None:
        import agg   # same whitelist as the aggregate reports (bridge 2.4 market context)
        ctx = agg.sanitize_ctx(data["mkt"]["ctx"], set())
        if ctx:
            mkt["ctx"] = ctx

    out_data = {"trades": trades, "log": logs, "flags": flags}
    for key in ("ea", "ver", "reason", "phase"):
        if data.get(key) is not None:
            out_data[key] = _str(data[key], 64)
    if eq:
        out_data["eq"] = eq
    if mkt:
        out_data["mkt"] = mkt
    return {"v": 1, "kind": "ea_report", "acct": acct, "rid": rid, "part": part, "parts": parts,
            "ts": ts, "data": out_data}


# ---------------------------------------------------------------- raw storage
def report_path(root: Path, rep: dict) -> Path:
    t = dt.datetime.fromtimestamp(rep["ts"], UTC)
    name = f"{rep['acct']}_{t.strftime('%Y%m%dT%H%M%SZ')}"
    if rep["parts"] > 1:
        name += f"_p{rep['part']}of{rep['parts']}"
    return root / "reports" / t.strftime("%Y/%m/%d") / f"{name}.json"


def store_raw(root: Path, rep: dict) -> Path:
    """Idempotent: same content -> same file; a different report with the same name gets a suffix."""
    path = report_path(root, rep)
    text = dumps_pretty(rep)
    n = 2
    while vault.exists(path, root) and vault.read_text(path, root) != text:
        path = path.with_name(f"{report_path(root, rep).stem}-{n}.json")
        n += 1
    vault.write_text(path, text, root)   # .zv when the vault is on; identical report -> no change on disk
    return path


def iter_report_files(root: Path, first: dt.date, last: dt.date):
    day = first
    while day <= last:
        folder = root / "reports" / day.strftime("%Y/%m/%d")
        if folder.is_dir():
            for p in vault.glob(folder, "*.json"):   # logical paths: x.json for x.json and x.json.zv
                yield p
        day += dt.timedelta(days=1)


def load_reports(root: Path, first: dt.date, last: dt.date) -> list[dict]:
    """Raw reports of the days [first, last]. Invalid JSON is skipped; an encrypted file without a valid ZUMI_KEY
    raises vault.VaultError (never silently treated as 'no reports')."""
    out = []
    for p in iter_report_files(root, first, last):
        try:
            out.append(json.loads(vault.read_text(p, root)))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda r: (r.get("ts", 0), r.get("part", 1)))
    return out


# ---------------------------------------------------------------- deal journal
def journal_dir(root: Path, acct: str) -> Path:
    return root / "reports" / "journal" / acct


def read_deals(root: Path, acct: str) -> dict[int, dict]:
    path = journal_dir(root, acct) / "deals.jsonl"
    deals: dict[int, dict] = {}
    if vault.exists(path, root):
        for line in vault.read_text(path, root).splitlines():
            if line.strip():
                d = json.loads(line)
                deals[int(d["deal"])] = d
    return deals


def upsert_deals(root: Path, acct: str, trades: list[dict]) -> bool:
    """Upsert exit deals keyed by deal ticket (idempotent: re-sent reports change nothing)."""
    deals = read_deals(root, acct)
    before = json.dumps(deals, sort_keys=True)
    for t in trades:
        merged = dict(deals.get(int(t["deal"]), {}))
        merged.update(t)
        deals[int(t["deal"])] = merged
    if json.dumps(deals, sort_keys=True) == before:
        return False
    ordered = sorted(deals.values(), key=lambda d: (d.get("t_out", 0), d["deal"]))
    text = "".join(json.dumps(d, sort_keys=True, ensure_ascii=True) + "\n" for d in ordered)
    vault.write_text(journal_dir(root, acct) / "deals.jsonl", text, root)
    return True


def list_accounts(root: Path) -> list[str]:
    base = root / "reports" / "journal"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


# ---------------------------------------------------------------- positions
def build_positions(deals: dict[int, dict] | list[dict]) -> list[dict]:
    """Group exit deals by position id. Partial closes give a volume-weighted exit price;
    R uses the SL at entry: r = sign * (exit_vw - entry) / |entry - sl| * closed_vol / vol_in."""
    items = deals.values() if isinstance(deals, dict) else deals
    groups: dict[int, list[dict]] = {}
    for d in items:
        groups.setdefault(int(d["pos"]), []).append(d)
    positions = []
    for pos, ds in groups.items():
        ds.sort(key=lambda d: (d.get("t_out", 0), d["deal"]))
        first = ds[0]
        vol_in = max((d.get("vol_in") or 0.0) for d in ds) or sum(d.get("vol", 0.0) for d in ds)
        vol_out = sum(d.get("vol", 0.0) for d in ds)
        exit_vw = (sum(d.get("out", 0.0) * d.get("vol", 0.0) for d in ds) / vol_out) if vol_out else None
        side = first.get("side")
        entry = first.get("in")
        sl = first.get("sl")
        sign = 1 if side == "buy" else -1 if side == "sell" else 0
        r = None
        if sl and entry and exit_vw is not None and sign and abs(entry - sl) > 0:
            r = sign * (exit_vw - entry) / abs(entry - sl) * (vol_out / vol_in if vol_in else 1.0)
        elif all(d.get("r") is not None for d in ds) and vol_in:
            r = sum(d["r"] * d.get("vol", 0.0) for d in ds) / vol_in
        pl = [d.get("pl_pct") for d in ds]
        pl_pct = sum(p for p in pl if p is not None) if any(p is not None for p in pl) else None
        maes = [d["mae_r"] for d in ds if d.get("mae_r") is not None]
        mfes = [d["mfe_r"] for d in ds if d.get("mfe_r") is not None]
        positions.append({
            "pos": pos, "sym": first.get("sym", ""), "side": side or "", "vol_in": round(vol_in, 4),
            "vol_out": round(vol_out, 4), "t_in": first.get("t_in"), "t_out": ds[-1].get("t_out"),
            "entry": entry, "exit": round(exit_vw, 5) if exit_vw is not None else None,
            "sl": sl, "tp": first.get("tp"), "r": round(r, 3) if r is not None else None,
            "pl_pct": round(pl_pct, 4) if pl_pct is not None else None,
            "risk_pct": first.get("risk_pct"), "method": (first.get("cmt") or "").strip(),
            "magic": first.get("magic"), "phase": (first.get("phase") or "").strip(),
            "reason": ds[-1].get("reason", ""), "n_exits": len(ds),
            "closed": bool(vol_in and vol_out >= vol_in * (1 - 1e-6)),
            "mae_r": min(maes) if maes else None, "mfe_r": max(mfes) if mfes else None,
        })
    positions.sort(key=lambda p: (p["t_out"] or 0, p["pos"]))
    return positions


POSITION_COLUMNS = ["pos", "sym", "side", "vol_in", "vol_out", "t_in", "t_out", "entry", "exit", "sl", "tp",
                    "r", "pl_pct", "risk_pct", "method", "magic", "phase", "reason", "n_exits", "closed",
                    "mae_r", "mfe_r"]


def write_positions_csv(root: Path, acct: str, positions: list[dict]) -> bool:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(POSITION_COLUMNS + ["open_utc", "close_utc"])
    for p in positions:
        row = ["" if p.get(c) is None else p.get(c) for c in POSITION_COLUMNS]
        row += [_iso(p.get("t_in")), _iso(p.get("t_out"))]
        w.writerow(row)
    return vault.write_text(journal_dir(root, acct) / "positions.csv", buf.getvalue(), root)


def _iso(ts) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def outcome(p: dict) -> str:
    v = p.get("pl_pct")
    if v is None:
        v = p.get("r")
    if v is None or v == 0:
        return "be"
    return "win" if v > 0 else "loss"
