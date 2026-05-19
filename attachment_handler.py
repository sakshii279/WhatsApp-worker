"""
attachment_handler.py
=====================
Download and save WhatsApp media attachments via the Graph API.

Meta requires two steps to download media:
    1. GET /v19.0/{media_id} → returns a temporary download URL
    2. GET {download_url}    → returns the actual file bytes

Files are saved under:
    {attachments_dir}/{sender}/{short_msg_id}_{epoch}_{type}{counter}{ext}

This mirrors the email attachment_handler naming pattern exactly.

Public API:
    save_all(media_list, sender, conn, att_dir, short, epoch) -> list[dict]
"""

import os
import mimetypes
import logging

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v19.0"


# ── MIME → extension ──────────────────────────────────────────

def _ext(mime_type: str, filename: str) -> str:
    """Derive file extension from mime_type or original filename."""
    if filename and "." in filename:
        return os.path.splitext(filename)[1]
    ext = mimetypes.guess_extension(mime_type or "")
    return ext if ext else ".bin"


# ── Single media download ─────────────────────────────────────

def _fetch_download_url(conn, media_id: str) -> str:
    """Step 1: resolve media_id to a temporary download URL."""
    url  = f"{GRAPH_BASE}/{media_id}"
    resp = conn.get(url)
    resp.raise_for_status()
    return resp.json().get("url", "")


def _download_bytes(conn, download_url: str) -> bytes:
    """Step 2: download the actual file bytes."""
    resp = conn.get(download_url)
    resp.raise_for_status()
    return resp.content


def _save_file(data: bytes, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _download_one(conn, media: dict, sender: str, att_dir: str, short: str, epoch: int, counter: int) -> dict:
    media_id  = media["media_id"]
    mime_type = media.get("mime_type", "")
    filename  = media.get("filename", "")
    mtype     = media.get("type", "file")

    ext       = _ext(mime_type, filename)
    fname     = f"{short}_{epoch}_{mtype}{counter}{ext}"
    fpath     = os.path.join(att_dir, sender, fname)

    try:
        dl_url = _fetch_download_url(conn, media_id)
        data   = _download_bytes(conn, dl_url)
        _save_file(data, fpath)
        logger.info("saved attachment: %s", fpath)
        return {"filename": fname, "path": fpath, "media_id": media_id, "mime_type": mime_type, "size": len(data)}
    except Exception as exc:
        logger.error("failed to download media_id=%s: %s", media_id, exc)
        return {"filename": fname, "path": None, "media_id": media_id, "error": str(exc)}


# ── Public API ────────────────────────────────────────────────

def save_all(media_list: list, sender: str, conn, att_dir: str, short: str, epoch: int) -> list:
    """
    Download and save all media items for one message.
    Returns list of saved file dicts.
    """
    saved = []
    for i, media in enumerate(media_list):
        result = _download_one(conn, media, sender, att_dir, short, epoch, i)
        saved.append(result)
    return saved