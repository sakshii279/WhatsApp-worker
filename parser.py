"""
parser.py
=========
Parse raw Meta webhook payload into a structured dict.

Meta sends a nested JSON payload. This module flattens it into
a clean dict that the rest of the pipeline can work with.

Public API:
    parse_payload(data: dict)         -> list[dict]   (one dict per message)
    get_sender(msg: dict)             -> str
    get_message_id(msg: dict)         -> str
    msg_short(message_id: str)        -> str           (first 8 chars)
    collect_media(msg: dict)          -> list[dict]    (media items to download)
"""


# ── Payload parsing ───────────────────────────────────────────

def parse_payload(data: dict) -> list:
    """
    Flatten a Meta webhook payload into a list of message dicts.
    Each dict has: sender, message_id, timestamp, type, text, media, raw.
    Returns empty list if payload has no messages.
    """
    messages = []
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                messages.append(_flatten(msg, value))
    return messages


def _flatten(msg: dict, value: dict) -> dict:
    return {
        "sender"     : _get_sender(msg),
        "message_id" : msg.get("id", ""),
        "timestamp"  : int(msg.get("timestamp", 0)),
        "type"       : msg.get("type", "unknown"),
        "text"       : _get_text(msg),
        "media"      : _get_media(msg),
        "phone_number_id": value.get("metadata", {}).get("phone_number_id", ""),
        "raw"        : msg,
    }


def _get_sender(msg: dict) -> str:
    return msg.get("from", "unknown")


def _get_text(msg: dict) -> str:
    return msg.get("text", {}).get("body", "")


def _get_media(msg: dict) -> dict:
    """Return media dict if message contains downloadable media, else {}."""
    media_types = ["image", "audio", "video", "document", "sticker", "voice"]
    for mt in media_types:
        if mt in msg:
            block = msg[mt]
            return {
                "type"     : mt,
                "media_id" : block.get("id", ""),
                "mime_type": block.get("mime_type", ""),
                "filename" : block.get("filename", ""),
                "sha256"   : block.get("sha256", ""),
                "caption"  : block.get("caption", ""),
            }
    return {}


# ── Helpers ───────────────────────────────────────────────────

def get_sender(msg: dict) -> str:
    return msg.get("sender", "unknown")


def get_message_id(msg: dict) -> str:
    return msg.get("message_id", "")


def msg_short(message_id: str) -> str:
    """Return first 8 chars of message_id for use in filenames."""
    return message_id[:8] if message_id else "00000000"


def collect_media(msg: dict) -> list:
    """Return list of media dicts to download. Empty list if no media."""
    media = msg.get("media", {})
    if not media or not media.get("media_id"):
        return []
    return [media]