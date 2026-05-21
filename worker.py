"""
worker.py
=========
Single Flask app handling ALL WhatsApp accounts on one port.

Local:
    python worker.py --config config.yaml

Azure:
    gunicorn worker:app
    WHATSAPP_CONFIG_PATH env var for config location (optional)

Routing:
    Meta sends phone_number_id in every payload.
    worker looks up matching account in config and processes accordingly.
    One port, one process, all accounts.

Endpoints:
    GET  /webhook          → Meta verification (uses verify_token per account)
    POST /webhook          → incoming messages (routed by phone_number_id)
    GET  /health           → Azure health check
"""

import argparse
import hashlib
import hmac
import os
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
from exception_handler import handle_success

_IST = timezone(timedelta(hours=5, minutes=30))

# ── module-level state (set at init) ──────────────────────────
_log      = None
_conn     = None
_accounts = {}   # phone_number_id → account dict
_cfg      = {}


# ── Config ────────────────────────────────────────────────────

def _resolve_config_path() -> str:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    args, _ = p.parse_known_args()
    return args.config or os.environ.get("WHATSAPP_CONFIG_PATH", "config.yaml")


def _build_account_map(accounts: list) -> dict:
    """Map phone_number_id → account dict for fast lookup."""
    return {str(a["phone_number_id"]): a for a in accounts}


# ── KV Status Helpers ─────────────────────────────────────────

def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _to_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _kv_key(name: str) -> str:
    return f"wa_worker_{name}"


def set_status(name: str, status: int) -> None:
    entry = kv_store.get(_kv_key(name)) or {}
    entry.setdefault("name", name)
    entry["status"]    = status
    entry["last_seen"] = _to_ist(_now())
    entry.setdefault("started_at", _to_ist(_now()))
    kv_store.set(_kv_key(name), entry)


def update_heartbeat(name: str) -> None:
    entry = kv_store.get(_kv_key(name)) or {}
    entry.setdefault("name", name)
    entry.setdefault("status", 1)
    entry.setdefault("started_at", _to_ist(_now()))
    entry["last_seen"] = _to_ist(_now())
    kv_store.set(_kv_key(name), entry)


# ── Signature Verification ────────────────────────────────────

def _verify_signature(payload: bytes, signature: str, app_secret: str) -> bool:
    if not signature or not app_secret:
        return True
    expected = "sha256=" + hmac.new(
        app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Process One Message ───────────────────────────────────────

def process_one(msg: dict, account: dict) -> int:
    name       = account["name"]
    sender     = wa_parser.get_sender(msg)
    msg_id     = wa_parser.get_message_id(msg)
    short      = wa_parser.msg_short(msg_id)
    epoch      = _now()
    media_list = wa_parser.collect_media(msg)
    att_dir    = _cfg["storage"]["attachments_dir"]

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

    checkpoint.save(_cfg["storage"]["checkpoints_dir"], name, msg_id)
    return handle_success(name, f"Processed message {short}")


# ── Flask App ─────────────────────────────────────────────────

def make_app() -> Flask:
    flask_app = Flask(__name__)

    @flask_app.get("/webhook")
    def verify():
        """
        Meta verification handshake.
        Meta sends verify_token — we match it against the correct account
        by looking up phone_number_id if provided, else check all accounts.
        """
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode != "subscribe":
            abort(403)

        # find the account whose verify_token matches
        matched = next(
            (a for a in _accounts.values() if a["verify_token"] == token),
            None
        )
        if matched:
            _log.info(matched["name"], "Webhook verified by Meta")
            return challenge, 200

        _log.warn("worker", "Webhook verification failed — no matching verify_token")
        abort(403)

    @flask_app.post("/webhook")
    def receive():
        """
        Receive incoming messages from Meta.
        Route to correct account by phone_number_id in payload.
        """
        sig     = request.headers.get("X-Hub-Signature-256", "")
        payload = request.get_data()
        data    = request.get_json(silent=True) or {}

        # extract phone_number_id from payload metadata
        phone_number_id = None
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                phone_number_id = change.get("value", {}).get("metadata", {}).get("phone_number_id")
                if phone_number_id:
                    break

        account = _accounts.get(str(phone_number_id)) if phone_number_id else None
        if not account:
            _log.warn("worker", f"No account found for phone_number_id={phone_number_id}")
            return jsonify({"status": "ok"}), 200

        app_secret = account.get("app_secret", "")
        if not _verify_signature(payload, sig, app_secret):
            _log.warn(account["name"], "Invalid signature — rejecting payload")
            abort(401)

        msgs = wa_parser.parse_payload(data)
        if not msgs:
            return jsonify({"status": "ok"}), 200

        _log.info(account["name"], f"Received {len(msgs)} message(s)")
        for msg in msgs:
            try:
                process_one(msg, account)
            except Exception as exc:
                _log.error(account["name"], f"Failed to process message", exc=exc)

        update_heartbeat(account["name"])
        return jsonify({"status": "ok"}), 200

    @flask_app.get("/health")
    def health():
        """Health check for Azure App Service."""
        return jsonify({
            "status"  : "ok",
            "accounts": list(_accounts.keys()),
        }), 200

    return flask_app


# ── Heartbeat thread ──────────────────────────────────────────

def _heartbeat_loop() -> None:
    """Send heartbeat for all accounts every 30 seconds."""
    while True:
        time.sleep(30)
        for account in _accounts.values():
            try:
                update_heartbeat(account["name"])
            except Exception:
                pass


# ── Init (runs once — at import time for gunicorn) ────────────

def _init() -> Flask:
    global _log, _conn, _accounts, _cfg

    config_path = _resolve_config_path()
    ConfigManager.init(config_path)
    ConfigManager.load()

    _cfg = {
        "whatsapp_accounts": ConfigManager.getProperty("whatsapp_accounts"),
        "storage"          : ConfigManager.getProperty("storage"),
        "polling"          : ConfigManager.getProperty("polling"),
    }

    kv_store.init(ConfigManager.getProperty("kv", {}))
    _log      = LoggerFactory.get()
    _conn     = ConnectorFactory.get(
        ConfigManager.getProperty("worker", {}).get("whatsapp_connector", "rabbitmq")
    )
    _accounts = _build_account_map(_cfg["whatsapp_accounts"])

    for account in _accounts.values():
        set_status(account["name"], 1)
        _log.info(account["name"], "Account registered")

    Thread(target=_heartbeat_loop, daemon=True).start()

    _log.info("worker", f"Started — handling {len(_accounts)} account(s)")
    return make_app()


# ── Module-level app for gunicorn ─────────────────────────────
app = _init()


# ── Local run ─────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100, debug=False, use_reloader=False)