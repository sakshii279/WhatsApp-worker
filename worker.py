"""
worker.py
=========
Per-account WhatsApp webhook server. Spawned by main.py — one instance per account.

CLI args:
    --name        business_account_1
    --config      config.yaml

Each worker:
    1. Starts a Flask HTTP server on its assigned port
    2. Handles GET  /webhook  → Meta verification challenge
    3. Handles POST /webhook  → incoming messages
    4. For each message: parse → download attachments → build record → send to connector
    5. Checkpoints last processed message_id

Logging:
    KV store     → worker status (1=running, 0=stopped, -1=error)
    framework    → activity logs via LoggerFactory
    framework    → message records via ConnectorFactory
"""

import argparse
import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta
from threading import Thread

from flask import Flask, request, jsonify, abort

import kv_store
import checkpoint
import connection
import parser as wa_parser
import attachment_handler
import json_builder
from config_manager import ConfigManager
from framework import ConnectorFactory, LoggerFactory
from exception_handler import (
    handle_auth_error, handle_fetch_error,
    handle_parse_error, handle_stop, handle_success,
)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── module-level logger and connector (set at init) ───────────
_log  = None
_conn = None


# ── Args ──────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--name",   required=True)
    p.add_argument("--config", default="config.yaml")
    return p.parse_args()


def find_account(name: str, accounts: list) -> dict:
    return next((a for a in accounts if a["name"] == name), None)


# ── KV Status Helpers ─────────────────────────────────────────

def kv_key(name: str) -> str:
    return f"wa_worker_{name}"


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _to_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def update_heartbeat(name: str) -> None:
    entry = kv_store.get(kv_key(name)) or {}
    entry.setdefault("name", name)
    entry.setdefault("status", 1)
    entry.setdefault("started_at", _to_ist(_now()))
    entry["last_seen"] = _to_ist(_now())
    kv_store.set(kv_key(name), entry)


def set_status(name: str, status: int) -> None:
    entry = kv_store.get(kv_key(name)) or {}
    entry.setdefault("name", name)
    entry["status"]    = status
    entry["last_seen"] = _to_ist(_now())
    entry.setdefault("started_at", _to_ist(_now()))
    kv_store.set(kv_key(name), entry)


def should_stop(name: str) -> bool:
    entry = kv_store.get(kv_key(name))
    if not entry:
        return False
    if kv_store._use_fallback:
        return False
    return entry.get("status") == 0


# ── Signature Verification ────────────────────────────────────

def _verify_signature(payload: bytes, signature: str, app_secret: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta."""
    if not signature or not app_secret:
        return True   # skip if not configured
    expected = "sha256=" + hmac.new(
        app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Process One Message ───────────────────────────────────────

def process_one(msg: dict, account: dict, cfg: dict) -> int:
    name    = account["name"]
    sender  = wa_parser.get_sender(msg)
    msg_id  = wa_parser.get_message_id(msg)
    short   = wa_parser.msg_short(msg_id)
    epoch   = _now()

    media_list = wa_parser.collect_media(msg)
    att_dir    = cfg["storage"]["attachments_dir"]

    conn = connection.get_connection(account)
    try:
        saved = attachment_handler.save_all(media_list, sender, conn, att_dir, short, epoch)
    except Exception as exc:
        _log.error(name, f"Attachment download failed for {short}", exc=exc)
        saved = []
    finally:
        connection.close_connection(conn)

    record = json_builder.build_record(msg, name, saved)

    ok = _conn.send(record)
    if ok:
        _log.info(name, f"Sent record: sender={sender} type={msg.get('type')}")
    else:
        _log.warn(name, f"Connector unavailable — record in fallback: sender={sender}")

    checkpoint.save(cfg["storage"]["checkpoints_dir"], name, msg_id)
    return handle_success(name, f"Processed message {short}")


# ── Flask App Factory ─────────────────────────────────────────

def make_app(account: dict, cfg: dict) -> Flask:
    app        = Flask(__name__)
    name       = account["name"]
    verify_tok = account["verify_token"]
    app_secret = account.get("app_secret", "")

    @app.get("/webhook")
    def verify():
        """Meta webhook verification handshake."""
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_tok:
            _log.info(name, "Webhook verified by Meta")
            return challenge, 200
        _log.warn(name, "Webhook verification failed — token mismatch")
        abort(403)

    @app.post("/webhook")
    def receive():
        """Receive incoming WhatsApp messages from Meta."""
        sig     = request.headers.get("X-Hub-Signature-256", "")
        payload = request.get_data()

        if not _verify_signature(payload, sig, app_secret):
            _log.warn(name, "Invalid signature — rejecting payload")
            abort(401)

        data = request.get_json(silent=True) or {}
        msgs = wa_parser.parse_payload(data)

        if not msgs:
            return jsonify({"status": "ok"}), 200

        _log.info(name, f"Received {len(msgs)} message(s)")
        for msg in msgs:
            try:
                process_one(msg, account, cfg)
            except Exception as exc:
                _log.error(name, f"Failed to process message", exc=exc)

        update_heartbeat(name)
        return jsonify({"status": "ok"}), 200

    return app


# ── Heartbeat thread ──────────────────────────────────────────

def _heartbeat_loop(name: str) -> None:
    """Update KV heartbeat every 30 seconds while server is running."""
    while True:
        time.sleep(30)
        try:
            update_heartbeat(name)
        except Exception:
            pass


# ── Entry Point ───────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()
    ConfigManager.init(args.config)
    ConfigManager.load()

    cfg = {
        "whatsapp_accounts": ConfigManager.getProperty("whatsapp_accounts"),
        "storage"          : ConfigManager.getProperty("storage"),
        "polling"          : ConfigManager.getProperty("polling"),
    }

    kv_store.init(ConfigManager.getProperty("kv", {}))

    _log  = LoggerFactory.get()
    worker_cfg          = ConfigManager.getProperty("worker", {})
    whatsapp_connector  = worker_cfg.get("whatsapp_connector", "rabbitmq")
    _conn               = ConnectorFactory.get(whatsapp_connector)

    account = find_account(args.name, cfg["whatsapp_accounts"])
    if not account:
        raise ValueError(f"No account found for name: {args.name}")

    name = account["name"]
    port = int(account["port"])

    set_status(name, 1)
    _log.info(name, f"Worker started on port {port}")

    # start heartbeat thread
    t = Thread(target=_heartbeat_loop, args=(name,), daemon=True)
    t.start()

    app = make_app(account, cfg)
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as exc:
        set_status(name, -1)
        _log.error(name, "Server crashed", exc=exc)
        raise
    finally:
        set_status(name, 0)
        _log.info(name, "Worker stopped")