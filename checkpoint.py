"""
checkpoint.py
=============
Persist the last processed message_id per WhatsApp account.

Checkpoints are stored as plain JSON files:
    {checkpoints_dir}/{account_name}.json

This mirrors the email checkpoint pattern (timestamp → message_id).

Public API:
    save(ckpt_dir, account_name, message_id)  -> None
    load(ckpt_dir, account_name)              -> str | None
"""

import os
import json
import logging

logger = logging.getLogger(__name__)


def _path(ckpt_dir: str, account_name: str) -> str:
    safe = account_name.replace("@", "_").replace("/", "_")
    return os.path.join(ckpt_dir, f"{safe}.json")


def save(ckpt_dir: str, account_name: str, message_id: str) -> None:
    """Persist the last successfully processed message_id."""
    os.makedirs(ckpt_dir, exist_ok=True)
    fpath = _path(ckpt_dir, account_name)
    try:
        with open(fpath, "w") as f:
            json.dump({"last_message_id": message_id}, f)
        logger.debug("checkpoint saved: %s → %s", account_name, message_id)
    except Exception as exc:
        logger.error("checkpoint save failed: %s", exc)


def load(ckpt_dir: str, account_name: str) -> str | None:
    """Load the last processed message_id. Returns None if no checkpoint."""
    fpath = _path(ckpt_dir, account_name)
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath) as f:
            data = json.load(f)
        return data.get("last_message_id")
    except Exception as exc:
        logger.error("checkpoint load failed: %s", exc)
        return None