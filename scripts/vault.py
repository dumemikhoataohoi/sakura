#!/usr/bin/env python3
"""Vault: file encryption for the PUBLIC sakura repository (format ZV2).

The repository stays public, but its data and documents are stored encrypted, so outsiders see only file names,
sizes, timing and the (generic) commit messages. Standard library only: the stdlib has no AES, so ZV2 is a PRF stream
cipher with encrypt-then-MAC built only on HMAC-SHA256 (MQL5 reproduces it with CryptEncode(CRYPT_HASH_SHA256) and a
manual HMAC, see mql5/ZumiGitHubBridge.mqh, bridge 2.8).

  K   master key: 32 bytes = 64 hex characters in env ZUMI_KEY (Actions: repo secret ZUMI_KEY)
  Ke  = HMAC(K, "zumi-enc-v1")          Km = HMAC(K, "zumi-mac-v1")
  N   = HMAC(Km, "nonce" || u16be(len(path)) || path || pt)[:16]   deterministic (SIV-style): same plaintext -> same
                                        file (quiet diffs); the length-prefixed path binds the file name unambiguously
  ks  = block i: HMAC(Ke, N || uint32be(i)), 32 bytes per block;   ct = pt XOR ks
  tag = HMAC(Km, "tag" || N || u16be(len(path)) || path || ct)[:32], checked in constant time BEFORE decrypting,
        then N is re-derived from the plaintext and compared too
  file: one text line "ZV2:" + base64url(N || tag || ct) (no padding), stored as <original name>.zv

``path`` is the repo-relative POSIX path of the PLAINTEXT file (e.g. "control/control.json"), UTF-8, <= 65535 bytes.
Fail closed: a missing or malformed key, a bad tag or a bad nonce raises VaultError and never yields plaintext.
VaultError is not an OSError / ValueError, so readers that skip unreadable files still stop loudly on it.

Safety rules (see docs/workflows.md):
* a path that has a .zv is always written encrypted, whatever the config says (no key -> VaultError, never plaintext);
  only `vault.py dec` (explicit and confirmed) turns a .zv back into plaintext;
* downgrade protection: while the vault is active (enabled, or key_fp set by a migration), a path of the vault set is
  read only from its .zv; a plaintext copy alone raises VaultError;
* public text (Actions logs, commit messages) is generic while the vault is active or any .zv exists.

Config (config/settings.json > vault): enabled, key_fp (8-hex fingerprint written by migrate; also the latch that
keeps the vault active), phase, ea_bridge_min, paths (phase 1 globs), phase2_paths (control.json, latest.json: only
when phase >= 2 AND ea_bridge_min >= 2.8, because older EA bridges read them in plaintext), plain_exceptions,
generic_commit_messages.

CLI:
  python scripts/vault.py cat FILE...        print decrypted content (FILE with or without .zv)
  python scripts/vault.py enc FILE...        encrypt in place (writes FILE.zv, removes FILE)
  python scripts/vault.py dec FILE...        decrypt in place to PLAINTEXT (asks for confirmation; --yes-plaintext)
  python scripts/vault.py check [FILE...]    key / fingerprint, every .zv decrypts, nothing left in plaintext
  python scripts/vault.py migrate --phase 1|2 [--dry-run]   encrypt the phase files in place (guarded)
  python scripts/vault.py doctor             key yes/no + fingerprint, pending files, reader self-check
  python scripts/vault.py alerts < comment   decrypt the ZV2 blobs of an alerts-PR comment
  python scripts/vault.py kat                print the known-answer vectors (dummy key) for MQL5
The key is never printed; only its fingerprint HMAC(K, "fp")[:4] (8 hex characters).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zumi_common import ROOT  # noqa: E402

MAGIC = "ZV2:"
SUFFIX = ".zv"
ENV_KEY = "ZUMI_KEY"
NONCE_LEN, TAG_LEN, BLOCK = 16, 32, 32
LABEL_ENC, LABEL_MAC = b"zumi-enc-v1", b"zumi-mac-v1"
PHASE2_BRIDGE_MIN = (2, 8)
# Never encrypted, whatever the config says: the tooling and the agent entry point must stay runnable / readable.
NEVER = ("AGENTS.md", "requirements.txt", "config/**", ".github/**", "scripts/**", "tests/**", "mql5/**", "alerts/**",
         "**.zv", ".git/**")
DEFAULTS = {"enabled": False, "key_fp": "", "phase": 1, "ea_bridge_min": "2.3", "paths": [], "phase2_paths": [],
            "plain_exceptions": [], "generic_commit_messages": True}
_B64_RE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FP_RE = re.compile(r"^[0-9a-f]{8}$")

# Known-answer vectors with a DUMMY key (bytes 0x00..0x1f). Duplicated in the ZGB_VAULT_KAT block of
# mql5/ZumiGitHubBridge.mqh (tests/test_mql5_static.py checks both against this implementation).
KAT_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KAT_CASES = (
    ("control/control.json", b'{"v":1}'),
    ("data/latest.json", b""),
    ("data/latest.json", b'{"schema":"zumi.latest","v":1,"note":"kat multi-block 0123456789 abcdefghij klmnopqrst"}'),
)
# RFC 4231 test case 2 (isolates the HMAC from the vault layer when debugging MQL5)
RFC4231_KEY, RFC4231_MSG = b"Jefe", b"what do ya want for nothing?"
RFC4231_MAC = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


class VaultError(RuntimeError):
    """Missing / malformed key or a file that fails authentication. Never carries key material or plaintext."""


# ---------------------------------------------------------------- crypto
def _hmac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def parse_key(text: str | None) -> bytes:
    value = (text or "").strip()
    if not value:
        raise VaultError(f"{ENV_KEY} is not set: encrypted files cannot be read or written "
                         f"(export {ENV_KEY}=<64 hex characters>, or the repo secret {ENV_KEY} in Actions)")
    if not _HEX_RE.match(value):
        raise VaultError(f"{ENV_KEY} must be exactly 64 hex characters (32 bytes)")
    return bytes.fromhex(value)


def load_key() -> bytes:
    return parse_key(os.environ.get(ENV_KEY))


def has_key() -> bool:
    try:
        load_key()
        return True
    except VaultError:
        return False


def subkeys(key: bytes) -> tuple[bytes, bytes]:
    if len(key) != 32:
        raise VaultError("the vault key must be 32 bytes")
    return _hmac(key, LABEL_ENC), _hmac(key, LABEL_MAC)


def fingerprint(key: bytes | None = None) -> str:
    """First 8 hex characters of HMAC(K, "fp"): identifies the key without revealing it (also printed by the EA)."""
    return _hmac(load_key() if key is None else key, b"fp").hex()[:8]


def logical(path) -> str:
    """Repo-relative POSIX path of the plaintext file ('.zv' removed): the value bound into N and the tag."""
    s = str(path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s.endswith(SUFFIX):
        s = s[:-len(SUFFIX)]
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:/", s) or ".." in s.split("/"):
        raise VaultError("the vault path must be repo-relative")
    return s


def _lp(path) -> bytes:
    """u16be(len(path)) || path: the length prefix makes (path, data) unambiguous in N and the tag."""
    p = logical(path).encode("utf-8")
    if len(p) > 0xFFFF:
        raise VaultError("the vault path is too long")
    return len(p).to_bytes(2, "big") + p


def _keystream(ke: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += _hmac(ke, nonce, i.to_bytes(4, "big"))
        i += 1
    return bytes(out[:n])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def encrypt(path, data: bytes, key: bytes | None = None) -> str:
    """ZV2 text (one line, no newline) of ``data`` stored at repo path ``path``."""
    k = load_key() if key is None else key
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("encrypt() takes bytes")
    lp = _lp(path)
    ke, km = subkeys(k)
    nonce = _hmac(km, b"nonce", lp, bytes(data))[:NONCE_LEN]
    ct = _xor(bytes(data), _keystream(ke, nonce, len(data)))
    tag = _hmac(km, b"tag", nonce, lp, ct)[:TAG_LEN]
    return MAGIC + base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii").rstrip("=")


def is_zv(text) -> bool:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:8]).decode("ascii", "replace")
    return isinstance(text, str) and text.lstrip().startswith(MAGIC)


def decrypt(path, text, key: bytes | None = None) -> bytes:
    """Plaintext bytes of a ZV2 text; VaultError on any problem (never partial plaintext)."""
    k = load_key() if key is None else key
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("ascii", "replace")
    t = (text or "").strip()
    rel = logical(path)
    if not t.startswith(MAGIC):
        raise VaultError(f"{rel}: not a ZV2 file")
    body = t[len(MAGIC):]
    if not _B64_RE.match(body):
        raise VaultError(f"{rel}: malformed ZV2 data")
    try:
        blob = base64.urlsafe_b64decode(body.rstrip("=") + "=" * (-len(body.rstrip("=")) % 4))
    except (ValueError, TypeError):
        raise VaultError(f"{rel}: malformed ZV2 data") from None
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise VaultError(f"{rel}: truncated ZV2 data")
    nonce, tag, ct = blob[:NONCE_LEN], blob[NONCE_LEN:NONCE_LEN + TAG_LEN], blob[NONCE_LEN + TAG_LEN:]
    lp = _lp(rel)
    ke, km = subkeys(k)
    if not hmac.compare_digest(_hmac(km, b"tag", nonce, lp, ct)[:TAG_LEN], tag):
        raise VaultError(f"{rel}: authentication failed (wrong key, wrong path or tampered file)")
    pt = _xor(ct, _keystream(ke, nonce, len(ct)))
    if not hmac.compare_digest(_hmac(km, b"nonce", lp, pt)[:NONCE_LEN], nonce):
        raise VaultError(f"{rel}: authentication failed (nonce mismatch)")
    return pt


def kat_vectors() -> dict:
    key = bytes.fromhex(KAT_KEY_HEX)
    ke, km = subkeys(key)
    return {"key": KAT_KEY_HEX, "ke": ke.hex(), "km": km.hex(), "fp": fingerprint(key),
            "rfc4231": RFC4231_MAC,
            "cases": [{"path": p, "pt_hex": pt.hex(), "zv": encrypt(p, pt, key)} for p, pt in KAT_CASES]}


def self_test(key: bytes | None = None) -> list[str]:
    """Implementation check (KAT with the dummy key) plus a round trip / tamper check with ``key`` when given."""
    problems = []
    if _hmac(RFC4231_KEY, RFC4231_MSG).hex() != RFC4231_MAC:
        problems.append("HMAC-SHA256 RFC 4231 vector mismatch")
    dummy = bytes.fromhex(KAT_KEY_HEX)
    for p, pt in KAT_CASES:
        try:
            if decrypt(p, encrypt(p, pt, dummy), dummy) != pt:
                problems.append(f"KAT round trip failed for {p}")
        except VaultError as exc:
            problems.append(f"KAT: {exc}")
    if key is not None:
        sample = b'{"vault":"round-trip check"}\n'
        z = encrypt("tests/vault_check.json", sample, key)
        try:
            ok = decrypt("tests/vault_check.json", z, key) == sample
        except VaultError:
            ok = False
        if not ok:
            problems.append("round trip with the configured key failed")
        bad = z[:-2] + ("A" if z[-2] != "A" else "B") + z[-1]
        for path, text in (("tests/vault_check.json", bad), ("tests/other.json", z)):
            try:
                decrypt(path, text, key)
                problems.append("a tampered or swapped file was accepted")
            except VaultError:
                pass
    return problems


# ---------------------------------------------------------------- config
def _glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def match_any(rel: str, patterns) -> bool:
    return any(_glob_re(str(p)).match(rel) for p in patterns or [])


def find_root(path: Path) -> Path | None:
    """Nearest parent holding config/settings.json (the repo root, or a test's temp root); None outside any repo."""
    p = Path(path).resolve()
    for d in [p] + list(p.parents):
        if (d / "config" / "settings.json").is_file():
            return d
    return None


_SETTINGS_CACHE: dict[Path, tuple[int, dict]] = {}


def settings(root_or_cfg=None) -> dict:
    if isinstance(root_or_cfg, dict):
        raw = root_or_cfg.get("vault") or {}
    else:
        path = (Path(root_or_cfg) if root_or_cfg else ROOT) / "config" / "settings.json"
        try:
            mtime = path.stat().st_mtime_ns
            hit = _SETTINGS_CACHE.get(path)
            if hit and hit[0] == mtime:
                raw = hit[1]
            else:
                raw = json.loads(path.read_text(encoding="utf-8")).get("vault") or {}
                _SETTINGS_CACHE[path] = (mtime, raw)
        except (OSError, json.JSONDecodeError):
            raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if not k.startswith("_")})
    return out


def active(vcfg: dict) -> bool:
    """The vault is active when enabled, or once a migration recorded key_fp (latch: a config flip alone does not
    downgrade the repository to plaintext)."""
    return bool(vcfg.get("enabled")) or bool(_FP_RE.match(str(vcfg.get("key_fp") or "")))


def enabled(root_or_cfg=None) -> bool:
    return active(settings(root_or_cfg))


def _ver(text) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def phase2_active(vcfg: dict) -> bool:
    """control.json / latest.json are encrypted only once the config says phase 2 AND the EA bridge reads ZV2."""
    return int(vcfg.get("phase", 1) or 1) >= 2 and _ver(vcfg.get("ea_bridge_min")) >= PHASE2_BRIDGE_MIN


def in_scope(rel: str, vcfg: dict, phase: int | None = None) -> bool:
    """True when the repo path belongs to the active phase (or to ``phase`` exactly), ignoring the on/off state."""
    rel = logical(rel)
    if match_any(rel, NEVER) or match_any(rel, vcfg.get("plain_exceptions")):
        return False
    if phase == 2 or (phase is None and phase2_active(vcfg)):
        if match_any(rel, vcfg.get("phase2_paths")):
            return True
    if phase == 2:
        return False
    if match_any(rel, vcfg.get("phase2_paths")):
        return False          # phase-2 paths never leak into phase 1 through a broad glob
    return match_any(rel, vcfg.get("paths"))


def should_encrypt(rel: str, vcfg: dict, phase: int | None = None) -> bool:
    """True when the vault is active and the repo path belongs to the active phase (or to ``phase`` exactly)."""
    return active(vcfg) and in_scope(rel, vcfg, phase)


# ---------------------------------------------------------------- file helpers (every reader / writer uses these)
def _abs(path, root=None) -> tuple[Path, Path | None]:
    p = Path(path)
    if not p.is_absolute():
        p = (Path(root) if root else Path.cwd()) / p
    p = p.resolve()
    if p.name.endswith(SUFFIX):
        p = p.with_name(p.name[:-len(SUFFIX)])
    r = Path(root).resolve() if root else find_root(p.parent)
    return p, r


def _in_repo(p: Path, r: Path | None) -> bool:
    if r is None:
        return False
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


def zv_path(p: Path) -> Path:
    return p.with_name(p.name + SUFFIX)


def rel_of(path, root=None) -> str:
    p, r = _abs(path, root)
    if not _in_repo(p, r):
        raise VaultError(f"{p.name}: outside the repository")
    return p.relative_to(r).as_posix()


def exists(path, root=None) -> bool:
    p, _ = _abs(path, root)
    return p.is_file() or zv_path(p).is_file()


def read_any(path, root=None, key: bytes | None = None) -> bytes:
    """Bytes of ``path``: from ``path.zv`` (decrypted, VaultError without a valid key) when it exists, else the
    plaintext file. Downgrade protection: while the vault is active, a path of the vault set is never read from
    plaintext (VaultError). FileNotFoundError when neither form exists."""
    p, r = _abs(path, root)
    z = zv_path(p)
    if z.is_file():
        return decrypt(rel_of(p, r), z.read_text(encoding="ascii", errors="replace"), key)
    if p.is_file() and _in_repo(p, r):
        rel = rel_of(p, r)
        if should_encrypt(rel, settings(r)):
            raise VaultError(f"{rel}: plaintext refused, {rel}{SUFFIX} expected (vault active; "
                             f"run `python scripts/vault.py enc {rel}` if this file is yours)")
    return p.read_bytes()


def read_text(path, root=None, key: bytes | None = None) -> str:
    return read_any(path, root, key).decode("utf-8")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_any(path, data: bytes, encrypted: bool, root=None, key: bytes | None = None,
              allow_decrypt: bool = False) -> bool:
    """Write ``data`` as ``path.zv`` (encrypted) or ``path`` (plaintext) and remove the other variant, so readers never
    see a stale copy. Returns True when anything on disk changed (deterministic ZV2: same data -> no change).
    Replacing an existing .zv with plaintext needs ``allow_decrypt`` (only `vault.py dec` passes it)."""
    p, r = _abs(path, root)
    z = zv_path(p)
    if not encrypted and z.is_file() and not allow_decrypt:
        raise VaultError(f"{p.name}: refusing to replace {p.name}{SUFFIX} with plaintext "
                         f"(only `python scripts/vault.py dec` decrypts)")
    target, other = (z, p) if encrypted else (p, z)
    payload = (encrypt(rel_of(p, r), bytes(data), key) + "\n").encode("ascii") if encrypted else bytes(data)
    changed = False
    if not (target.is_file() and target.read_bytes() == payload):
        _write_bytes_atomic(target, payload)
        changed = True
    if other.is_file():
        other.unlink()
        changed = True
    return changed


def write_text(path, text: str, root=None, cfg: dict | None = None, key: bytes | None = None) -> bool:
    """Writer entry point: encrypts when the active vault selects this path, and ALWAYS when a .zv already exists
    (whatever the config says). An encrypted write without a valid key raises VaultError; nothing is written."""
    p, r = _abs(path, root)
    data = text.encode("utf-8")
    if not _in_repo(p, r):       # not a repository file (never published): plain write
        return write_any(p, data, False, r, key)
    vcfg = settings(cfg if cfg is not None else r)
    encrypted = zv_path(p).is_file() or should_encrypt(rel_of(p, r), vcfg)
    return write_any(p, data, encrypted, r, key)


def glob(folder, pattern: str) -> list[Path]:
    """Logical paths (without .zv) of the files matching ``pattern`` in plaintext or encrypted form, sorted."""
    folder = Path(folder)
    found = {p for p in folder.glob(pattern) if p.is_file() and not p.name.endswith(SUFFIX)}
    found |= {p.with_name(p.name[:-len(SUFFIX)]) for p in folder.glob(pattern + SUFFIX) if p.is_file()}
    return sorted(found)


# ---------------------------------------------------------------- public text (commit messages, Actions logs)
_ANY_ZV: set[Path] = set()


def any_zv(root) -> bool:
    """True when the repository holds at least one .zv file (cached once true)."""
    r = Path(root).resolve()
    if r in _ANY_ZV:
        return True
    for _, rel in _repo_files(r):
        if rel.endswith(SUFFIX):
            _ANY_ZV.add(r)
            return True
    return False


def guarded(root_or_cfg) -> bool:
    """True while public text must stay generic: vault active, or (given a repo root) any .zv in the repository."""
    if isinstance(root_or_cfg, dict):
        return active(settings(root_or_cfg))
    root = Path(root_or_cfg) if root_or_cfg else ROOT
    return active(settings(root)) or any_zv(root)


def commit_message(root_or_cfg, detailed: str, area: str) -> str:
    """Commit messages are public: generic while the vault is active or any .zv exists."""
    v = settings(root_or_cfg)
    if guarded(root_or_cfg) and v.get("generic_commit_messages", True):
        return f"{area}: update"
    return detailed


def public_log(root_or_cfg, detailed: str, generic: str) -> str:
    """Actions logs of a public repository are public: the generic line there while the vault is active or any .zv
    exists (pass the repo root so the .zv scan works)."""
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" and guarded(root_or_cfg):
        return generic
    return detailed


def alert_id(alert_key: str, key: bytes | None = None) -> str:
    """Opaque, stable id of an alert key (de-duplication on the public alerts PR without revealing the key)."""
    _, km = subkeys(load_key() if key is None else key)
    return "z" + _hmac(km, b"alert", alert_key.encode("utf-8")).hex()[:12]


# ---------------------------------------------------------------- repository scans
def _repo_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for name in filenames:
            p = Path(dirpath) / name
            yield p, p.relative_to(root).as_posix()


def encrypted_files(root: Path) -> list[str]:
    return sorted(rel[:-len(SUFFIX)] for _, rel in _repo_files(root) if rel.endswith(SUFFIX))


def pending_files(root: Path, vcfg: dict, phase: int | None = None) -> list[str]:
    """Plaintext files that the (given or active) phase would encrypt."""
    out = []
    for p, rel in _repo_files(root):
        if rel.endswith(SUFFIX) or p.name.startswith(".tmp_"):
            continue
        if in_scope(rel, vcfg, phase):
            out.append(rel)
    return sorted(out)


def key_problems(root: Path, key: bytes | None) -> list[str]:
    """Key missing while needed, or a key whose fingerprint differs from config vault.key_fp (typo / wrong key)."""
    v = settings(root)
    need = active(v) or bool(encrypted_files(root))
    if key is None:
        return [f"{ENV_KEY} is not set (needed: vault active or .zv files present)"] if need else []
    fp, want = fingerprint(key), str(v.get("key_fp") or "")
    if want and fp != want:
        return [f"key fingerprint mismatch: {ENV_KEY} gives {fp}, config vault.key_fp is {want} (wrong key or typo)"]
    return []


def check(root: Path, files=None, key: bytes | None = None) -> list[str]:
    """Problems (empty = OK): every .zv decrypts, and no file exists in both forms."""
    problems = []
    rels = [logical(rel_of(f, root)) for f in files] if files else encrypted_files(root)
    k = key
    for rel in rels:
        z = zv_path(root / rel)
        if not z.is_file():
            problems.append(f"{rel}: no {rel}{SUFFIX}")
            continue
        if (root / rel).is_file():
            problems.append(f"{rel}: both plaintext and {SUFFIX} exist (readers use {SUFFIX}; remove one)")
        try:
            if k is None:
                k = load_key()
            decrypt(rel, z.read_text(encoding="ascii", errors="replace"), k)
        except VaultError as exc:
            problems.append(str(exc))
    return problems


def reader_checks(root: Path) -> list[tuple[str, str]]:
    """Run each reader entry point on the repo as it is: (reader, "OK" | error). VaultError = cannot decrypt."""
    import datetime as dt

    import calendar_events
    import control
    import reports_lib as rl
    cfg = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8"))
    out = []

    def run(name, fn):
        try:
            fn()
            out.append((name, "OK"))
        except FileNotFoundError:
            out.append((name, "OK (file absent)"))
        except VaultError as exc:
            out.append((name, f"FAIL: {exc}"))
        except Exception as exc:  # noqa: BLE001 - report, do not crash the doctor
            out.append((name, f"FAIL: {type(exc).__name__}"))
    run("calendar_events.load_events", lambda: calendar_events.load_events(root / cfg["calendar"]["file"]))
    run("reports_lib.load_reports", lambda: rl.load_reports(root, dt.date(2000, 1, 1), dt.date.today() + dt.timedelta(days=1)))
    run("control.load_json (control.json)", lambda: control.load_json(root / control.CONTROL_PATH))
    run("control CHANGELOG", lambda: read_text(root / control.CHANGELOG_PATH, root))
    run("data/latest.json", lambda: json.loads(read_text(root / "data" / "latest.json", root)))
    run("data/quality.json", lambda: json.loads(read_text(root / "data" / "quality.json", root)))
    run("reports/coord pages", lambda: [read_text(p, root) for p in glob(root / "reports" / "coord", "*")])
    run("reports/agents pages", lambda: [read_text(p, root) for p in glob(root / "reports" / "agents", "*")])
    run("docs", lambda: [read_text(p, root) for p in glob(root / "docs", "**/*")])
    return out


def _set_key_fp(root: Path, fp: str) -> None:
    """Record the key fingerprint in config/settings.json > vault.key_fp (keeps the file's key order)."""
    path = root / "config" / "settings.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("vault", {})["key_fp"] = fp
    _write_bytes_atomic(path, (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- CLI
def _files(args_files) -> list[Path]:
    return [Path(f) for f in args_files]


def cmd_cat(files) -> int:
    for f in _files(files):
        try:
            sys.stdout.write(read_text(f))
        except FileNotFoundError:
            print(f"error: {f}: not found", file=sys.stderr)
            return 1
        except VaultError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_enc(files) -> int:
    for f in _files(files):
        try:
            p, r = _abs(f)
            data = read_any(p, r) if zv_path(p).is_file() else p.read_bytes()   # a plaintext file being encrypted
            write_any(p, data, True, r)
            print(f"encrypted {rel_of(p, r)}")
        except (FileNotFoundError, VaultError) as exc:
            print(f"error: {f}: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_dec(files, yes: bool) -> int:
    """The only way back to plaintext: explicit, and confirmed (interactive prompt, or --yes-plaintext)."""
    if not yes:
        if not sys.stdin.isatty():
            print("refused: `dec` writes PLAINTEXT that the next commit publishes; confirm in a terminal or pass "
                  "--yes-plaintext", file=sys.stderr)
            return 2
        answer = input(f"Decrypt {len(files)} file(s) to PLAINTEXT (published by the next commit)? Type 'yes': ")
        if answer.strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 2
    for f in _files(files):
        try:
            p, r = _abs(f)
            z = zv_path(p)
            data = decrypt(rel_of(p, r), z.read_text(encoding="ascii", errors="replace"))
            write_any(p, data, False, r, allow_decrypt=True)
            print(f"decrypted {rel_of(p, r)} (PLAINTEXT: re-encrypt with `vault.py enc` before committing)")
        except (FileNotFoundError, VaultError) as exc:
            print(f"error: {f}: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_check(root: Path, files) -> int:
    """CI gate (first step of every workflow that reads vault files): fail fast on a missing / wrong key, an
    undecryptable .zv, or a vault-set file left in plaintext. Vault off and no .zv: OK without a key."""
    key = load_key() if has_key() else None
    problems = []
    if os.environ.get(ENV_KEY, "").strip() and key is None:
        try:
            load_key()
        except VaultError as exc:
            problems.append(str(exc))
    problems += key_problems(root, key)
    if not problems:
        problems += check(root, [root / f if not Path(f).is_absolute() else Path(f) for f in files] if files else None,
                          key)
    v = settings(root)
    if active(v):
        problems += [f"{rel}: should be encrypted (run `python scripts/vault.py migrate --phase 1`)"
                     for rel in pending_files(root, v)]
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1
    print(f"vault check OK: {len(encrypted_files(root))} encrypted file(s), vault "
          f"{'active' if active(v) else 'off'}" + (f", key fp {fingerprint(key)}" if key else ""))
    return 0


def guard_migrate(root: Path, phase: int) -> list[str]:
    """Reasons to refuse a migration (empty = go)."""
    v = settings(root)
    try:
        key = load_key()
    except VaultError as exc:
        return [str(exc)]
    reasons = [f"self-test: {p}" for p in self_test(key)]
    reasons += key_problems(root, key)
    reasons += [f"existing encrypted files: {p}" for p in check(root, key=key)]
    if not v.get("enabled"):
        reasons.append("config vault.enabled is false: set it to true first")
    if phase == 2:
        if _ver(v.get("ea_bridge_min")) < PHASE2_BRIDGE_MIN:
            reasons.append('phase 2 needs config vault.ea_bridge_min "2.8" (every live EA on bridge 2.8 with the '
                           "vault key file); older bridges read control.json / latest.json in plaintext")
        if int(v.get("phase", 1) or 1) < 2:
            reasons.append("phase 2 needs config vault.phase 2 (otherwise the writers keep these files in plaintext)")
    return reasons


def cmd_migrate(root: Path, phase: int, dry_run: bool) -> int:
    reasons = guard_migrate(root, phase)
    if reasons:
        for r in reasons:
            print(f"refused: {r}", file=sys.stderr)
        return 2
    key = load_key()
    todo = pending_files(root, settings(root), phase)
    for rel in todo:
        if dry_run:
            print(f"would encrypt {rel}")
            continue
        src = root / rel
        data = src.read_bytes()
        z = zv_path(src)
        _write_bytes_atomic(z, (encrypt(rel, data, key) + "\n").encode("ascii"))
        try:   # verify what is ON DISK before the plaintext goes
            ok = decrypt(rel, z.read_text(encoding="ascii"), key) == data
        except VaultError:
            ok = False
        if not ok:
            z.unlink()
            print(f"error: verification of {rel}{SUFFIX} failed; plaintext kept, migration stopped", file=sys.stderr)
            return 1
        src.unlink()
        print(f"encrypted {rel}")
    if dry_run:
        print(f"{len(todo)} file(s) would be encrypted (phase {phase})")
        return 0
    fp = fingerprint(key)
    if not settings(root).get("key_fp"):
        _set_key_fp(root, fp)
        print(f"recorded vault.key_fp {fp} in config/settings.json")
    problems = check(root, key=key)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    print(f"migrated {len(todo)} file(s) to {SUFFIX} (phase {phase}); key fingerprint {fp}")
    return 1 if problems else 0


def cmd_doctor(root: Path) -> int:
    v = settings(root)
    print(f"key present: {'yes' if has_key() else 'no'} (env {ENV_KEY})")
    key = load_key() if has_key() else None
    want = str(v.get("key_fp") or "")
    if key is not None:
        fp = fingerprint(key)
        state = "not recorded yet" if not want else ("match" if fp == want else "MISMATCH (wrong key or typo)")
        print(f"key fingerprint: {fp}  (config vault.key_fp: {want or '-'}, {state}; the EA logs the same)")
    st = self_test(key)
    print("self-test: " + ("OK" if not st else "FAIL: " + "; ".join(st)))
    print(f"config: enabled={str(v['enabled']).lower()} active={str(active(v)).lower()} phase={v['phase']} "
          f"ea_bridge_min={v['ea_bridge_min']} phase2_active={str(phase2_active(v)).lower()}")
    enc = encrypted_files(root)
    print(f"encrypted now: {len(enc)} file(s)")
    for ph in (1, 2):
        todo = pending_files(root, v, ph)
        print(f"phase {ph}: {len(todo)} plaintext file(s) would be encrypted")
        for rel in todo:
            print(f"  {rel}")
    problems = key_problems(root, key) + (check(root, key=key) if enc and key else [])
    print("encrypted files decrypt: " + ("OK" if not problems else "FAIL"))
    for p in problems:
        print(f"  {p}")
    bad = 0
    for name, status in reader_checks(root):
        print(f"reader {name}: {status}")
        bad += status.startswith("FAIL")
    return 1 if (st or problems or bad) else 0


ALERT_RE = re.compile(r"`(z[0-9a-f]{12})`\s+(ZV2:[A-Za-z0-9_-]+)")


def cmd_alerts(text: str) -> int:
    found = ALERT_RE.findall(text)
    if not found:
        print("no encrypted alerts found")
        return 1
    for aid, blob in found:
        try:
            print(f"{aid}: {decrypt(f'alerts/{aid}', blob).decode('utf-8')}")
        except VaultError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZV2 file encryption for the public sakura repository")
    ap.add_argument("command", choices=["cat", "enc", "dec", "check", "migrate", "doctor", "alerts", "kat"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--root")
    ap.add_argument("--phase", type=int, choices=[1, 2])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes-plaintext", action="store_true", help="dec: confirm writing plaintext without a prompt")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else ROOT
    if args.command == "cat":
        return cmd_cat(args.files)
    if args.command == "enc":
        return cmd_enc(args.files)
    if args.command == "dec":
        return cmd_dec(args.files, args.yes_plaintext)
    if args.command == "check":
        return cmd_check(root, args.files)
    if args.command == "migrate":
        if args.phase is None:
            ap.error("migrate needs --phase 1 or --phase 2")
        return cmd_migrate(root, args.phase, args.dry_run)
    if args.command == "doctor":
        return cmd_doctor(root)
    if args.command == "alerts":
        return cmd_alerts(sys.stdin.read())
    print(json.dumps(kat_vectors(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
