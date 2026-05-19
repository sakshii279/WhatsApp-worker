"""
kv_store.py
===========
Cross-platform REST client for the Spring Boot KV Datastore (port 5322).

Platform behaviour:
    Windows  → uses requests (works natively)
    macOS    → uses /usr/bin/curl via subprocess (macOS sandboxes Python sockets)
    Linux    → uses requests (works natively)

All endpoints are POST with JSON body:
    POST /datastore/api/kv/set      {dbnum, keyin, valuein}
    POST /datastore/api/kv/get      {dbnum, keyin}
    POST /datastore/api/kv/delete   {dbnum, keyin}
    POST /datastore/api/kv/exists   {dbnum, keyin}
    POST /datastore/api/kv/allkeys  {dbnum}

API response shapes:
    set     → {"status":2, "keyret":"k", "valueret":null}
    get     → {"status":1, "keyret":"k", "valueret":"<json string>"}
    allkeys → {"status":1, "count":N, "keylist":["k1","k2"]}

Worker status codes:  1=running | 0=stopped | -1=error
"""

import json
import logging
import os
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Module state ──────────────────────────────────────────────

_base_url     : str  = ""
_dbnum        : int  = 1
_TIMEOUT      : int  = 5
_use_fallback : bool = False
_IS_MAC       : bool = platform.system() == "Darwin"
_FALLBACK_PATH = Path(__file__).parent / "kv_fallback.json"


# ── Fallback helpers ──────────────────────────────────────────

def _load_fb() -> dict:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(_FALLBACK_PATH.read_text())
    except Exception:
        return {}


def _save_fb(store: dict) -> None:
    _FALLBACK_PATH.write_text(json.dumps(store, indent=2))


# ── HTTP: requests (Windows / Linux) ─────────────────────────

def _post_requests(path: str, body: dict):
    import requests
    r = requests.post(
        f"{_base_url}{path}",
        json=body,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() if r.text.strip() else None


# ── HTTP: curl subprocess (macOS) ─────────────────────────────

def _post_curl(path: str, body: dict):
    result = subprocess.run(
        [
            "/usr/bin/curl", "-s",
            "-X", "POST",
            f"{_base_url}{path}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body),
            "--max-time", str(_TIMEOUT),
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT + 2,
    )
    if result.returncode != 0:
        raise ConnectionError(f"curl failed (code {result.returncode}): {result.stderr.strip()}")
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


# ── Unified POST dispatcher ───────────────────────────────────

def _post(path: str, body: dict):
    """Route to curl on macOS, requests on Windows/Linux."""
    if _IS_MAC:
        return _post_curl(path, body)
    return _post_requests(path, body)


# ── Fallback switch ───────────────────────────────────────────

def _go_fallback(reason: str) -> None:
    global _use_fallback
    _use_fallback = True
    logger.warning("kv_store: fallback — %s (data → %s)", reason, _FALLBACK_PATH)


# ── init ──────────────────────────────────────────────────────

def _health_check() -> None:
    _post("/kv/allkeys", {"dbnum": _dbnum})


def init(cfg: dict) -> None:
    """
    Call once at startup: kv_store.init(ConfigManager.getProperty("kv", {}))
    Falls back to local JSON silently on any failure.
    Once fallback is set it stays until next app restart.
    """
    global _base_url, _dbnum, _use_fallback, _TIMEOUT
    if os.environ.get("KV_FORCE_FALLBACK") == "1":
        return _go_fallback("KV_FORCE_FALLBACK=1")
    if not cfg.get("host"):
        return _go_fallback("no host in config")
    _base_url = f"http://{cfg['host']}:{cfg.get('port', 5322)}/datastore/api"
    _dbnum    = int(cfg.get("dbnum", 1))
    _TIMEOUT  = int(cfg.get("timeout", 5))
    try:
        _health_check()
        _use_fallback = False
        logger.info(
            "kv_store: connected via %s → %s (dbnum=%s)",
            "curl" if _IS_MAC else "requests",
            _base_url, _dbnum,
        )
    except Exception as exc:
        _go_fallback(str(exc))


# ── set ───────────────────────────────────────────────────────

def set(key: str, value: dict) -> None:
    """Store value under key. Once in fallback, stays in fallback."""
    if _use_fallback:
        store = _load_fb()
        store[key] = value
        return _save_fb(store)
    try:
        _post("/kv/set", {"dbnum": _dbnum, "keyin": key, "valuein": json.dumps(value)})
    except Exception as exc:
        _go_fallback(str(exc))
        store = _load_fb()
        store[key] = value
        _save_fb(store)


# ── get ───────────────────────────────────────────────────────

def _parse_get(res) -> dict | None:
    if not isinstance(res, dict):
        return res
    raw = res.get("valueret")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def get(key: str) -> dict | None:
    """Return stored dict for key, or None if absent."""
    if _use_fallback:
        return _load_fb().get(key)
    try:
        return _parse_get(_post("/kv/get", {"dbnum": _dbnum, "keyin": key}))
    except Exception as exc:
        _go_fallback(str(exc))
        return _load_fb().get(key)


# ── delete ────────────────────────────────────────────────────

def delete(key: str) -> None:
    """Remove key. No-op if absent."""
    if _use_fallback:
        store = _load_fb()
        store.pop(key, None)
        return _save_fb(store)
    try:
        _post("/kv/delete", {"dbnum": _dbnum, "keyin": key})
    except Exception as exc:
        _go_fallback(str(exc))
        store = _load_fb()
        store.pop(key, None)
        _save_fb(store)


# ── exists ────────────────────────────────────────────────────

def exists(key: str) -> bool:
    """Return True if key is present."""
    if _use_fallback:
        return key in _load_fb()
    try:
        res = _post("/kv/exists", {"dbnum": _dbnum, "keyin": key})
        return bool(res and res.get("status") == 1)
    except Exception as exc:
        _go_fallback(str(exc))
        return key in _load_fb()


# ── all_keys ──────────────────────────────────────────────────

def all_keys() -> list[str]:
    """Return all keys in the current DB partition."""
    if _use_fallback:
        return list(_load_fb().keys())
    try:
        res = _post("/kv/allkeys", {"dbnum": _dbnum})
        return res.get("keylist", []) if isinstance(res, dict) else []
    except Exception as exc:
        _go_fallback(str(exc))
        return list(_load_fb().keys())


# ── all_entries ───────────────────────────────────────────────

def _build_entries(keys: list[str]) -> dict:
    return {k: v for k in keys if (v := get(k)) is not None}


def all_entries() -> dict:
    """Return all key-value pairs in the current DB partition."""
    if _use_fallback:
        return dict(_load_fb())
    try:
        return _build_entries(all_keys())
    except Exception as exc:
        _go_fallback(str(exc))
        return dict(_load_fb())