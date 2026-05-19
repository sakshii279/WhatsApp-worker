"""
main.py
=======
Scheduler for multi-account WhatsApp webhook worker.

Behaviour:
    1. Read whatsapp_accounts from config.yaml
    2. Spawn one worker subprocess per account (each on its own port)
    3. Monitor workers — restart any that crash
    4. On Ctrl+C or SIGTERM: kill all workers cleanly

Unlike the email scheduler (spawn → wait → sleep → repeat),
WhatsApp workers are long-running HTTP servers.
So main.py monitors and restarts crashed workers instead of cycling them.

Works both as a plain Python script and as a PyInstaller exe.
"""

import os
import sys
import signal
import subprocess
import time
from datetime import datetime, timezone, timedelta

# ── Path bootstrap ────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

os.chdir(_BASE)
sys.path.insert(0, _BASE)

import kv_store
from config_manager import ConfigManager

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Logging ───────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{ts}] [main] {msg}")


# ── Config ────────────────────────────────────────────────────

def load_config() -> dict:
    ConfigManager.init(os.path.join(_BASE, "config.yaml"))
    ConfigManager.load()
    return {
        "whatsapp_accounts": ConfigManager.getProperty("whatsapp_accounts"),
        "monitor"          : ConfigManager.getProperty("monitor", {}),
        "kv"               : ConfigManager.getProperty("kv", {}),
    }


# ── KV helpers ────────────────────────────────────────────────

def kv_key(name: str) -> str:
    return f"wa_worker_{name}"


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _to_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def register_worker(name: str, pid: int, port: int) -> None:
    now = _now_ts()
    kv_store.set(kv_key(name), {
        "name"      : name,
        "status"    : 1,
        "started_at": _to_ist(now),
        "last_seen" : _to_ist(now),
        "pid"       : pid,
        "port"      : port,
    })


def mark_stopped(name: str) -> None:
    entry = kv_store.get(kv_key(name)) or {}
    entry["status"]    = 0
    entry["last_seen"] = _to_ist(_now_ts())
    kv_store.set(kv_key(name), entry)


def clear_worker(name: str) -> None:
    kv_store.delete(kv_key(name))


# ── Worker lifecycle ──────────────────────────────────────────

def _worker_cmd(name: str) -> list:
    """Build the command to launch worker.py for one account."""
    config_path = os.path.join(_BASE, "config.yaml")
    if getattr(sys, "frozen", False):
        exe = os.path.join(_BASE, "worker.exe" if sys.platform == "win32" else "worker")
        return [exe, "--name", name, "--config", config_path]
    worker_path = os.path.join(_BASE, "worker.py")
    return [sys.executable, worker_path, "--name", name, "--config", config_path]


def spawn_one(account: dict) -> subprocess.Popen:
    """Spawn a single worker process for one account."""
    name = account["name"]
    port = int(account["port"])
    clear_worker(name)
    proc = subprocess.Popen(
        _worker_cmd(name),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    register_worker(name, proc.pid, port)
    _log(f"spawned worker for {name} on port {port} (pid {proc.pid})")
    return proc


def spawn_all(accounts: list) -> dict:
    """Spawn one worker per account. Returns {name: Popen}."""
    return {a["name"]: spawn_one(a) for a in accounts}


# ── Monitor loop ──────────────────────────────────────────────

def monitor_loop(accounts: list, procs: dict, check_interval: int, shutting_down: list) -> None:
    """
    Periodically check all workers. Restart any that have died.
    Runs until shutting_down[0] is True.
    """
    account_map = {a["name"]: a for a in accounts}
    while not shutting_down[0]:
        time.sleep(check_interval)
        if shutting_down[0]:
            break
        for name, proc in list(procs.items()):
            ret = proc.poll()
            if ret is not None:
                _log(f"worker crashed: {name} (exit {ret}) — restarting")
                procs[name] = spawn_one(account_map[name])


# ── Shutdown handler ──────────────────────────────────────────

def _make_shutdown(procs_ref: list) -> tuple:
    shutting_down = [False]

    def handler(sig, frame):
        if shutting_down[0]:
            return
        shutting_down[0] = True
        _log("shutdown signal received — stopping workers ...")
        procs = procs_ref[0] if procs_ref else {}
        for name, proc in procs.items():
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            mark_stopped(name)
        _log("all workers stopped — exiting")
        sys.exit(0)

    return handler, shutting_down


# ── Main ──────────────────────────────────────────────────────

def main():
    _log("starting WhatsApp webhook scheduler")
    cfg = load_config()
    kv_store.init(cfg["kv"])

    accounts       = cfg["whatsapp_accounts"]
    check_interval = int(cfg["monitor"].get("check_interval_seconds", 30))
    procs_ref      = [{}]

    handler, shutting_down = _make_shutdown(procs_ref)
    signal.signal(signal.SIGINT,  handler)
    signal.signal(signal.SIGTERM, handler)

    _log(f"accounts: {[a['name'] for a in accounts]}")
    _log(f"ports   : {[a['port'] for a in accounts]}")

    procs = spawn_all(accounts)
    procs_ref[0] = procs

    _log("all workers spawned — entering monitor loop")
    monitor_loop(accounts, procs, check_interval, shutting_down)
    _log("scheduler exited")


if __name__ == "__main__":
    main()