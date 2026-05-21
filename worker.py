"""
worker.py
=========
Single Flask app handling ALL WhatsApp accounts on one port.

Local:
    python worker.py --config config.yaml

Azure:
    gunicorn worker:app
    Account config read from environment variables:
        WHATSAPP_PHONE_NUMBER_ID_1, WHATSAPP_ACCESS_TOKEN_1, WHATSAPP_VERIFY_TOKEN_1
        WHATSAPP_PHONE_NUMBER_ID_2, WHATSAPP_ACCESS_TOKEN_2, WHATSAPP_VERIFY_TOKEN_2
        ... and so on

Routing:
    Meta sends phone_number_id in every payload.
    worker looks up matching account and processes accordingly.
    One port, one process, all accounts.

Endpoints:
    GET  /webhook  → Meta verification
    POST /webhook  → incoming messages routed by phone_number_id
    GET  /health   → Azure health check
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

# ── module-level state ────────────────────────────────────────
_log      = None
_conn     = None
_accounts = {}   # phone_number_id → account dict
_cfg      = {}


# ── Account loading ───────────────────────────────────────────

def _load_accounts_from_env() -> dict:
    """
    Read account config from environment variables.
    Scans for WHATSAPP_PHONE_NUMBER_ID_1, _2, _3 ... until one is missing.
    Returns phone_number_id → account dict.
    """
    accounts = {}
    i = 1
    while True:
        phone_number_id = os.environ.get(f"WHATSAPP_PHONE_NUMBER_ID_{i}")
        if not phone_number_id:
            break
        access_token  = os.environ.get(f"WHATSAPP_ACCESS_TOKEN_{i}", "")
        verify_token  = os.environ.get(f"WHATSAPP_VERIFY_TOKEN_{i}", "")
        app_secret    = os.environ.get(f"WHATSAPP_APP_SECRET_{i}", "")
        name          = os.environ.get(f"WHATSAPP_ACCOUNT_NAME_{i}", f"account_{i}")
        accounts[str(phone_number_id)] = {
            "name"           : name,
            "phone_number_id": phone_number_id,
            "access_token"   : access_token,
            "verify_token"   : verify_token,
            "app_secret"     : app_secret,
        }
        i += 1
    return accounts


def _load_accounts_from_config(config_accounts: list) -> dict:
    """Build phone_number_id → account dict from config.yaml accounts list."""
    return {str(a["phone_number_id"]): a for a in config_accounts}


def _resolve_accounts(config_accounts: list) -> dict:
    """
    Use env vars on Azure, config.yaml locally.
    If env vars are present they take priority.
    """
    env_accounts = _load_accounts_from_env()
    if env_accounts:
        return env_accounts
    return _load_accounts_from_config(config_accounts)


# ── Config ────────────────────────────────────────────────────

def _resolve_config_path() -> str:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    args, _ = p.parse_known_args()
    raw = args.config or os.environ.get("WHATSAPP_CONFIG_PATH", "config.yaml")
    # resolve relative to the directory this file lives in, not cwd
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.dirname(os.path.abspath(__file__)), raw)
    return raw


def _load_cfg() -> dict:
    """
    Load infrastructure config (storage, kv, rabbitmq).
    On Azure without config.yaml, fall back to env vars for storage paths.
    """
    config_path = os.path.abspath(_resolve_config_path())
    if os.path.isfile(config_path):
        ConfigManager.init(config_path)
        ConfigManager.load()
        return {
            "whatsapp_accounts": ConfigManager.getProperty("whatsapp_accounts", []),
            "storage"          : ConfigManager.getProperty("storage"),
            "polling"          : ConfigManager.getProperty("polling", {}),
            "kv"               : ConfigManager.getProperty("kv", {}),
            "worker"           : ConfigManager.getProperty("worker", {}),
        }
    # Azure fallback — no config.yaml, read storage paths from env
    return {
        "whatsapp_accounts": [],
        "storage": {
            "attachments_dir": os.environ.get("WHATSAPP_ATTACHMENTS_DIR", "/tmp/attachments"),
            "checkpoints_dir": os.environ.get("WHATSAPP_CHECKPOINTS_DIR", "/tmp/checkpoints"),
            "logs_dir"       : os.environ.get("WHATSAPP_LOGS_DIR", "/tmp/logs"),
        },
        "polling": {"interval_seconds": 60},
        "kv"     : {
            "host"   : os.environ.get("KV_HOST", "192.168.100.231"),
            "port"   : int(os.environ.get("KV_PORT", 5322)),
            "dbnum"  : int(os.environ.get("KV_DBNUM", 2)),
            "timeout": int(os.environ.get("KV_TIMEOUT", 5)),
        },
        "worker": {
            "whatsapp_connector": os.environ.get("WHATSAPP_CONNECTOR", "rabbitmq")
        },
    }


# ── KV Helpers ────────────────────────────────────────────────

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
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode != "subscribe":
            abort(403)

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
        sig     = request.headers.get("X-Hub-Signature-256", "")
        payload = request.get_data()
        data    = request.get_json(silent=True) or {}

        phone_number_id = None
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                phone_number_id = change.get("value", {}).get("metadata", {}).get("phone_number_id")
                if phone_number_id:
                    break

        account = _accounts.get(str(phone_number_id)) if phone_number_id else None
        if not account:
            _log.warn("worker", f"No account for phone_number_id={phone_number_id}")
            return jsonify({"status": "ok"}), 200

        if not _verify_signature(payload, sig, account.get("app_secret", "")):
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
        return jsonify({
            "status"  : "ok",
            "accounts": [
                {"name": a["name"], "phone_number_id": a["phone_number_id"]}
                for a in _accounts.values()
            ],
        }), 200

    return flask_app


# ── Heartbeat thread ──────────────────────────────────────────

def _heartbeat_loop() -> None:
    while True:
        time.sleep(30)
        for account in _accounts.values():
            try:
                update_heartbeat(account["name"])
            except Exception:
                pass


# ── Init ──────────────────────────────────────────────────────

def _init() -> Flask:
    global _log, _conn, _accounts, _cfg

    _cfg      = _load_cfg()
    kv_store.init(_cfg["kv"])
    _log      = LoggerFactory.get()
    _conn     = ConnectorFactory.get(_cfg["worker"].get("whatsapp_connector", "rabbitmq"))
    _accounts = _resolve_accounts(_cfg["whatsapp_accounts"])

    if not _accounts:
        raise ValueError(
            "No WhatsApp accounts found. "
            "Set WHATSAPP_PHONE_NUMBER_ID_1, WHATSAPP_ACCESS_TOKEN_1, WHATSAPP_VERIFY_TOKEN_1 "
            "env vars or add accounts to config.yaml."
        )

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