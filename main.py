"""
main.py
=======
Entry point for local development.
Starts the single WhatsApp worker Flask app that handles all accounts.

On Azure: gunicorn starts worker:app directly — main.py is not used.
Locally : python main.py starts the Flask dev server.

On crash: restarts the worker process automatically.
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

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Logging ───────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{ts}] [main] {msg}")


# ── Worker command ────────────────────────────────────────────

def _worker_cmd() -> list:
    config_path = os.path.join(_BASE, "config.yaml")
    if getattr(sys, "frozen", False):
        exe = os.path.join(_BASE, "worker.exe" if sys.platform == "win32" else "worker")
        return [exe, "--config", config_path]
    worker_path = os.path.join(_BASE, "worker.py")
    return [sys.executable, worker_path, "--config", config_path]


# ── Shutdown handler ──────────────────────────────────────────

def _make_shutdown(proc_ref: list) -> tuple:
    shutting_down = [False]

    def handler(sig, frame):
        if shutting_down[0]:
            return
        shutting_down[0] = True
        _log("shutdown signal — stopping worker ...")
        proc = proc_ref[0]
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        _log("worker stopped — exiting")
        sys.exit(0)

    return handler, shutting_down


# ── Main ──────────────────────────────────────────────────────

def main():
    _log("starting WhatsApp worker")
    proc_ref      = [None]
    handler, shutting_down = _make_shutdown(proc_ref)
    signal.signal(signal.SIGINT,  handler)
    signal.signal(signal.SIGTERM, handler)

    while not shutting_down[0]:
        _log("spawning worker process")
        proc        = subprocess.Popen(_worker_cmd(), stdout=sys.stdout, stderr=sys.stderr)
        proc_ref[0] = proc
        proc.wait()

        if shutting_down[0]:
            break

        _log(f"worker exited (code {proc.returncode}) — restarting in 5s")
        time.sleep(5)

    _log("main exited")


if __name__ == "__main__":
    main()