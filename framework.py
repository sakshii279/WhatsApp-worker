"""
framework.py — Plug-and-Play Connector + Logger Framework
==========================================================
Drop into project root. Configure via config.yaml.
Existing files (kv_store.py, rabbit_logger.py, connection.py) untouched.

─── CONNECTOR USAGE ────────────────────────────────────────────
    from framework import ConnectorFactory
    conn = ConnectorFactory.get("rabbitmq")
    conn.send({"key": "value"})
    data = conn.receive()

─── LOGGER USAGE ───────────────────────────────────────────────
    from framework import LoggerFactory
    log = LoggerFactory.get()
    log.info("worker", "Poll cycle started")
    log.debug("worker", "Found 5 new emails")
    log.warn("worker", "RabbitMQ unavailable — using fallback")
    log.error("worker", "IMAP connection failed", exc=e)

─── CONFIG EXAMPLE ─────────────────────────────────────────────
    logger:
      level: debug           # debug | info | warn | error
      service: mail-fetcher  # global service tag on every log entry
      handlers:
        - type: console
        - type: file
          path: logs/app.log
        - type: kv
          connector: kv      # must match a connector name below
          key_prefix: log_
        - type: rabbitmq
          connector: rabbitmq
        - type: http
          connector: partner_api

    connectors:
      rabbitmq:
        type: rabbitmq
        host: 192.168.100.152
        api_port: 3333
        queue: gmail_data
        fallback_path: logs/rabbit_fallback.json
      kv:
        type: kv
        host: 192.168.100.231
        port: 5322
        dbnum: 1
        timeout: 5
        fallback_path: kv_fallback.json
      partner_api:
        type: http
        send_url: https://api.partner.com/ingest
        timeout: 10
"""

import json
import logging
import os
import subprocess
import platform
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
_pylog = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# SHARED UTIL
# ─────────────────────────────────────────────

def _is_mac():
    return platform.system() == "Darwin"


def _curl_post(url, payload, timeout=5):
    cmd = [
        "/usr/bin/curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
        "--max-time", str(timeout),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        return r.stdout
    except Exception:
        return None


def _http_post(url, payload, timeout=5):
    if _is_mac():
        return _curl_post(url, payload, timeout)
    try:
        import requests
        r = requests.post(url, json=payload, timeout=timeout)
        return r.text
    except Exception:
        return None


def _load_config():
    import sys, yaml
    # when frozen by PyInstaller use exe directory not temp extraction folder
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base, "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


# ─────────────────────────────────────────────
# CONNECTOR INTERFACE
# ─────────────────────────────────────────────

class MessageConnector(ABC):
    @abstractmethod
    def send(self, data: dict) -> bool:
        """Send data. Returns True on success, False on failure."""

    @abstractmethod
    def receive(self) -> list:
        """Fetch available records. Returns list of dicts."""


# ─────────────────────────────────────────────
# ADAPTER: RabbitMQ REST API
# ─────────────────────────────────────────────

class RabbitMQAdapter(MessageConnector):
    def __init__(self, cfg):
        self.host     = cfg["host"]
        self.port     = cfg["api_port"]
        self.queue    = cfg["queue"]
        self.fallback = cfg.get("fallback_path", "logs/rabbit_fallback.json")
        self._url     = f"http://{self.host}:{self.port}/rabbitmqresponse/{self.queue}"

    def send(self, data: dict) -> bool:
        payload  = {"InputDataJson": json.dumps(data)}
        response = _http_post(self._url, payload)
        if response:
            return True
        return self._write_fallback(data)

    def receive(self) -> list:
        return []

    def _write_fallback(self, data: dict) -> bool:
        try:
            os.makedirs(os.path.dirname(self.fallback), exist_ok=True)
            existing = []
            if os.path.exists(self.fallback):
                with open(self.fallback, "r") as f:
                    existing = json.load(f)
            existing.append(data)
            with open(self.fallback, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────
# ADAPTER: KV Store REST API
# ─────────────────────────────────────────────

class KVAdapter(MessageConnector):
    def __init__(self, cfg):
        self.dbnum    = cfg.get("dbnum", 1)
        self.timeout  = cfg.get("timeout", 5)
        self.fallback = cfg.get("fallback_path", "kv_fallback.json")
        self._base    = f"http://{cfg['host']}:{cfg['port']}/datastore/api"

    def _post(self, endpoint, body):
        return _http_post(f"{self._base}{endpoint}", body, self.timeout)

    def send(self, data: dict) -> bool:
        key    = data.get("key", f"framework_{int(time.time())}")
        result = self._post("/kv/set", {
            "dbnum": self.dbnum, "keyin": key, "valuein": json.dumps(data)
        })
        if result:
            return True
        return self._write_fallback(key, data)

    def receive(self) -> list:
        raw = self._post("/kv/allkeys", {"dbnum": self.dbnum})
        if not raw:
            return self._read_fallback()
        try:
            keys = json.loads(raw).get("keylist", [])
        except Exception:
            return []
        records = []
        for k in keys:
            resp = self._post("/kv/get", {"dbnum": self.dbnum, "keyin": k})
            if not resp:
                continue
            try:
                val = json.loads(json.loads(resp).get("valueret", "{}"))
                records.append(val)
            except Exception:
                continue
        return records

    def _write_fallback(self, key, data) -> bool:
        try:
            store = {}
            if os.path.exists(self.fallback):
                with open(self.fallback, "r") as f:
                    store = json.load(f)
            store[key] = data
            with open(self.fallback, "w") as f:
                json.dump(store, f, indent=2)
        except Exception:
            pass
        return False

    def _read_fallback(self) -> list:
        try:
            if not os.path.exists(self.fallback):
                return []
            with open(self.fallback, "r") as f:
                store = json.load(f)
            return list(store.values())
        except Exception:
            return []


# ─────────────────────────────────────────────
# ADAPTER: Generic HTTP REST
# ─────────────────────────────────────────────

class HTTPAdapter(MessageConnector):
    def __init__(self, cfg):
        self.send_url    = cfg.get("send_url", "")
        self.receive_url = cfg.get("receive_url", "")
        self.timeout     = cfg.get("timeout", 5)

    def send(self, data: dict) -> bool:
        if not self.send_url:
            return False
        result = _http_post(self.send_url, data, self.timeout)
        return result is not None

    def receive(self) -> list:
        if not self.receive_url:
            return []
        result = _http_post(self.receive_url, {}, self.timeout)
        if not result:
            return []
        try:
            parsed = json.loads(result)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []


# ─────────────────────────────────────────────
# ADAPTER: Azure Service Bus
# ─────────────────────────────────────────────

class ServiceBusAdapter(MessageConnector):
    def __init__(self, cfg):
        self.connection_str = cfg.get("connection_string", "")
        self.queue_name     = cfg.get("queue", "whatsapp_data")
        self.fallback       = cfg.get("fallback_path", "/tmp/servicebus_fallback.json")
        self._client        = None

    def _get_client(self):
        if not self._client:
            from azure.servicebus import ServiceBusClient
            self._client = ServiceBusClient.from_connection_string(self.connection_str)
        return self._client

    def send(self, data: dict) -> bool:
        try:
            from azure.servicebus import ServiceBusMessage
            client = self._get_client()
            sender = client.get_queue_sender(queue_name=self.queue_name)
            msg    = ServiceBusMessage(json.dumps(data))
            with sender:
                sender.send_messages(msg)
            return True
        except Exception as exc:
            _pylog.warning("ServiceBus send failed: %s", exc)
            return self._write_fallback(data)

    def receive(self) -> list:
        return []

    def _write_fallback(self, data: dict) -> bool:
        try:
            existing = []
            if os.path.exists(self.fallback):
                with open(self.fallback, "r") as f:
                    existing = json.load(f)
            existing.append(data)
            with open(self.fallback, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────
# CONNECTOR FACTORY
# ─────────────────────────────────────────────

_ADAPTER_MAP = {
    "rabbitmq"  : RabbitMQAdapter,
    "kv"        : KVAdapter,
    "http"      : HTTPAdapter,
    "servicebus": ServiceBusAdapter,
}

_connector_instances: dict = {}


class ConnectorFactory:
    @staticmethod
    def get(name: str) -> MessageConnector:
        if name in _connector_instances:
            return _connector_instances[name]
        cfg = ConnectorFactory._connector_cfg(name)
        cls = _ADAPTER_MAP.get(cfg.get("type", ""))
        if not cls:
            raise ValueError(
                f"Unknown connector type '{cfg.get('type')}' for '{name}'. "
                f"Valid types: {list(_ADAPTER_MAP.keys())}"
            )
        _connector_instances[name] = cls(cfg)
        return _connector_instances[name]

    @staticmethod
    def _connector_cfg(name: str) -> dict:
        config     = _load_config()
        connectors = config.get("connectors", {})
        if name not in connectors:
            raise KeyError(f"Connector '{name}' not found under connectors: in config.yaml")
        return connectors[name]

    @staticmethod
    def reset(name: str = None):
        if name:
            _connector_instances.pop(name, None)
        else:
            _connector_instances.clear()


# ═════════════════════════════════════════════
# LOGGER FRAMEWORK
# ═════════════════════════════════════════════

# Log levels — higher number = more severe
_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _make_entry(level: str, service: str, source: str, message: str, exc=None) -> dict:
    """Build a structured log entry dict."""
    entry = {
        "timestamp" : _now_ist(),
        "level"     : level.upper(),
        "service"   : service,
        "source"    : source,
        "message"   : message,
    }
    if exc is not None:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


# ─────────────────────────────────────────────
# LOG HANDLER INTERFACE
# ─────────────────────────────────────────────

class LogHandler(ABC):
    @abstractmethod
    def handle(self, entry: dict) -> None:
        """Receive and persist a log entry dict."""


# ─────────────────────────────────────────────
# HANDLER: Console
# ─────────────────────────────────────────────

class ConsoleHandler(LogHandler):
    """Prints log entries to Terminal with level-based formatting."""

    _ICONS = {"DEBUG": "🔍", "INFO": "ℹ️ ", "WARN": "⚠️ ", "ERROR": "❌"}

    def handle(self, entry: dict) -> None:
        icon = self._ICONS.get(entry["level"], "  ")
        line = (
            f"[{entry['timestamp']}] "
            f"{icon} [{entry['level']}] "
            f"[{entry['service']}:{entry['source']}] "
            f"{entry['message']}"
        )
        if "error" in entry:
            line += f" | {entry['error']}"
        print(line)


# ─────────────────────────────────────────────
# HANDLER: File
# ─────────────────────────────────────────────

class FileHandler(LogHandler):
    """Appends JSON log entries to a file. One entry per line."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def handle(self, entry: dict) -> None:
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


# ─────────────────────────────────────────────
# HANDLER: KV Store
# ─────────────────────────────────────────────

class KVLogHandler(LogHandler):
    """
    Writes log entries to KV store.
    Key format: {key_prefix}{level}_{timestamp_epoch}
    Uses an existing KVAdapter from ConnectorFactory.
    """

    def __init__(self, connector_name: str, key_prefix: str = "log_"):
        self.connector_name = connector_name
        self.key_prefix     = key_prefix

    def handle(self, entry: dict) -> None:
        try:
            conn = ConnectorFactory.get(self.connector_name)
            key  = f"{self.key_prefix}{entry['level'].lower()}_{int(time.time())}"
            conn.send({**entry, "key": key})
        except Exception:
            pass


# ─────────────────────────────────────────────
# HANDLER: RabbitMQ
# ─────────────────────────────────────────────

class RabbitMQLogHandler(LogHandler):
    """
    Sends log entries to RabbitMQ via RabbitMQAdapter.
    Uses an existing connector from ConnectorFactory.
    """

    def __init__(self, connector_name: str):
        self.connector_name = connector_name

    def handle(self, entry: dict) -> None:
        try:
            conn = ConnectorFactory.get(self.connector_name)
            conn.send(entry)
        except Exception:
            pass


# ─────────────────────────────────────────────
# HANDLER: HTTP
# ─────────────────────────────────────────────

class HTTPLogHandler(LogHandler):
    """
    POSTs log entries to any HTTP endpoint via HTTPAdapter.
    Uses an existing connector from ConnectorFactory.
    """

    def __init__(self, connector_name: str):
        self.connector_name = connector_name

    def handle(self, entry: dict) -> None:
        try:
            conn = ConnectorFactory.get(self.connector_name)
            conn.send(entry)
        except Exception:
            pass


# ─────────────────────────────────────────────
# LOGGER CORE
# ─────────────────────────────────────────────

class Logger:
    """
    Central logger. Fan-out to all configured handlers.
    Level filter — entries below configured level are dropped.
    Methods: debug(), info(), warn(), error()

    Usage:
        log = LoggerFactory.get()
        log.info("worker", "Poll cycle started")
        log.error("worker", "IMAP failed", exc=e)
    """

    def __init__(self, service: str, level: str, handlers: list):
        self._service      = service
        self._min_level    = _LEVELS.get(level.lower(), 1)
        self._handlers     = handlers

    def _log(self, level: str, source: str, message: str, exc=None) -> None:
        if _LEVELS.get(level, 0) < self._min_level:
            return
        entry = _make_entry(level, self._service, source, message, exc)
        for handler in self._handlers:
            try:
                handler.handle(entry)
            except Exception:
                pass

    def debug(self, source: str, message: str, exc=None) -> None:
        self._log("debug", source, message, exc)

    def info(self, source: str, message: str, exc=None) -> None:
        self._log("info", source, message, exc)

    def warn(self, source: str, message: str, exc=None) -> None:
        self._log("warn", source, message, exc)

    def error(self, source: str, message: str, exc=None) -> None:
        self._log("error", source, message, exc)


# ─────────────────────────────────────────────
# HANDLER BUILDER MAP
# ─────────────────────────────────────────────

def _build_handler(cfg: dict) -> LogHandler:
    """Build a single LogHandler from its config dict."""
    t = cfg.get("type", "")
    if t == "console":
        return ConsoleHandler()
    if t == "file":
        return FileHandler(cfg.get("path", "logs/app.log"))
    if t == "kv":
        return KVLogHandler(cfg["connector"], cfg.get("key_prefix", "log_"))
    if t == "rabbitmq":
        return RabbitMQLogHandler(cfg["connector"])
    if t == "http":
        return HTTPLogHandler(cfg["connector"])
    raise ValueError(f"Unknown log handler type: '{t}'. Valid: console, file, kv, rabbitmq, http")


# ─────────────────────────────────────────────
# LOGGER FACTORY
# ─────────────────────────────────────────────

_logger_instance: Logger = None


class LoggerFactory:
    """
    Reads config.yaml logger: section and returns a cached Logger instance.
    All handlers defined in config are built and attached automatically.
    Swap or add handlers in config.yaml — zero code changes needed.

    config.yaml example:
        logger:
          level: debug
          service: mail-fetcher
          handlers:
            - type: console
            - type: file
              path: logs/app.log
            - type: kv
              connector: kv
              key_prefix: log_
            - type: rabbitmq
              connector: rabbitmq
            - type: http
              connector: partner_api
    """

    @staticmethod
    def get() -> Logger:
        global _logger_instance
        if _logger_instance:
            return _logger_instance
        config     = _load_config()
        logger_cfg = config.get("logger", {})
        service    = logger_cfg.get("service", "app")
        level      = logger_cfg.get("level", "info")
        handlers   = [
            _build_handler(h)
            for h in logger_cfg.get("handlers", [{"type": "console"}])
        ]
        _logger_instance = Logger(service, level, handlers)
        return _logger_instance

    @staticmethod
    def reset() -> None:
        """Clear cached logger — forces rebuild on next get()."""
        global _logger_instance
        _logger_instance = None