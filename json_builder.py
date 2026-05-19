"""
json_builder.py
===============
Build a clean, structured record from a parsed WhatsApp message.

The record is what gets sent to the connector (RabbitMQ, KV, HTTP).
Mirrors email json_builder structure so downstream consumers stay consistent.

Public API:
    build_record(msg: dict, account_name: str, saved_attachments: list) -> dict
"""

from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_to_ist(epoch: int) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def build_record(msg: dict, account_name: str, saved_attachments: list) -> dict:
    """
    Build a structured record dict from a parsed message.

    Fields mirror email record structure for downstream consistency:
        account, sender, message_id, timestamp, type, text,
        attachments, phone_number_id, raw
    """
    return {
        "account"        : account_name,
        "sender"         : msg.get("sender", ""),
        "message_id"     : msg.get("message_id", ""),
        "timestamp"      : _ts_to_ist(msg.get("timestamp", 0)),
        "type"           : msg.get("type", "unknown"),
        "text"           : msg.get("text", ""),
        "attachments"    : saved_attachments,
        "phone_number_id": msg.get("phone_number_id", ""),
        "raw"            : msg.get("raw", {}),
    }