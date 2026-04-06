import os
import json
import hmac
import hashlib
import requests
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader

# ─────────────────────────────────────────
#  CONFIG — set these as environment variables on Railway
# ─────────────────────────────────────────
VERIFY_TOKEN    = os.environ.get("VERIFY_TOKEN", "workflow")
WHATSAPP_TOKEN  = os.environ.get("WHATSAPP_TOKEN", "")
APP_SECRET      = os.environ.get("APP_SECRET", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")

CLOUD_NAME      = os.environ.get("CLOUDINARY_CLOUD_NAME", "dyyqnnfkw")
API_KEY         = os.environ.get("CLOUDINARY_API_KEY", "")
API_SECRET      = os.environ.get("CLOUDINARY_API_SECRET", "")

LOG_PUBLIC_ID   = "whatsapp_log"   # Cloudinary public ID for the log file

# Configure Cloudinary
cloudinary.config(
    cloud_name = CLOUD_NAME,
    api_key    = API_KEY,
    api_secret = API_SECRET
)

# Allowed media types
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
}

EXTENSION_MAP = {
    "image/jpeg"          : ".jpg",
    "image/png"           : ".png",
    "image/webp"          : ".webp",
    "image/gif"           : ".gif",
    "application/pdf"     : ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv"            : ".csv",
}

app = Flask(__name__)


# ─────────────────────────────────────────
#  SECURITY — verify webhook signature
# ─────────────────────────────────────────
def verify_signature(payload_body, signature_header):
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        APP_SECRET.encode("utf-8"),
        payload_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header[7:])


# ─────────────────────────────────────────
#  CLOUDINARY — log helpers
# ─────────────────────────────────────────
def fetch_log_from_cloudinary():
    """Download the current log JSON from Cloudinary."""
    try:
        url = f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/{LOG_PUBLIC_ID}.json"
        response = requests.get(url)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"⚠️  Could not fetch log from Cloudinary: {e}")
    return []


def push_log_to_cloudinary(log):
    """Upload the updated log JSON to Cloudinary, overwriting the previous one."""
    try:
        log_bytes = json.dumps(log, indent=2, ensure_ascii=False).encode("utf-8")
        cloudinary.uploader.upload(
            log_bytes,
            public_id        = LOG_PUBLIC_ID,
            resource_type    = "raw",
            overwrite        = True,
            invalidate       = True,
            format           = "json"
        )
        print(f"✅  Log updated on Cloudinary ({len(log)} records)")
    except Exception as e:
        print(f"⚠️  Could not push log to Cloudinary: {e}")


def append_to_log(record):
    log = fetch_log_from_cloudinary()
    log.append(record)
    push_log_to_cloudinary(log)


# ─────────────────────────────────────────
#  CLOUDINARY — media upload
# ─────────────────────────────────────────
def upload_media_to_cloudinary(media_url, mime_type, filename_hint, sender, timestamp):
    """Download media from Meta and upload directly to Cloudinary."""
    headers  = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    response = requests.get(media_url, headers=headers, stream=True)
    response.raise_for_status()

    date_str = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
    ext      = EXTENSION_MAP.get(mime_type, "")
    if filename_hint and "." in filename_hint:
        filename = filename_hint
    else:
        ts       = datetime.fromtimestamp(int(timestamp)).strftime("%H%M%S")
        filename = f"media_{ts}{ext}"

    # Cloudinary folder structure: whatsapp_media/<sender>/<date>/
    folder      = f"whatsapp_media/{sender}/{date_str}"
    public_id   = f"{folder}/{Path(filename).stem}"

    # Determine resource type
    if mime_type.startswith("image/"):
        resource_type = "image"
    else:
        resource_type = "raw"

    result = cloudinary.uploader.upload(
        response.content,
        public_id     = public_id,
        resource_type = resource_type,
        overwrite     = False,
        format        = ext.lstrip(".") if ext else None
    )

    cloudinary_url = result.get("secure_url")
    size_kb        = round(len(response.content) / 1024, 2)
    print(f"      ☁️   Uploaded to Cloudinary: {cloudinary_url}")
    return cloudinary_url, size_kb


# ─────────────────────────────────────────
#  MESSAGE PROCESSOR
# ─────────────────────────────────────────
def process_message(message, contact_name, contact_phone):
    msg_type  = message.get("type")
    timestamp = message.get("timestamp", "0")
    msg_id    = message.get("id", "")
    dt_str    = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n  ───────────────────────────────────────────────────")
    print(f"  📱  From      : {contact_name} ({contact_phone})")
    print(f"      Type      : {msg_type}")
    print(f"      Time      : {dt_str}")
    print(f"      Msg ID    : {msg_id}")

    record = {
        "id"       : msg_id,
        "from"     : {"phone": contact_phone, "name": contact_name},
        "timestamp": timestamp,
        "datetime" : dt_str,
        "type"     : msg_type,
        "hasMedia" : False,
        "media"    : None,
        "text"     : None,
        "caption"  : None,
        "context"  : None,
    }

    # ── Text message ───────────────────────────────────
    if msg_type == "text":
        record["text"] = message.get("text", {}).get("body", "")
        print(f"      Text      : {record['text'][:150]}")

    # ── Media messages ─────────────────────────────────
    elif msg_type in ("image", "document", "video", "audio", "sticker"):
        media_data = message.get(msg_type, {})
        media_id   = media_data.get("id")
        filename   = media_data.get("filename", "")
        caption    = media_data.get("caption", "")
        record["caption"] = caption if caption else None

        if media_id:
            try:
                # Step 1 — get temporary Meta URL
                url      = f"https://graph.facebook.com/v19.0/{media_id}"
                headers  = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
                resp     = requests.get(url, headers=headers)
                resp.raise_for_status()
                data     = resp.json()
                media_url  = data.get("url")
                mime_type  = data.get("mime_type")
                file_size  = data.get("file_size")

                print(f"      MIME      : {mime_type}")
                print(f"      Filename  : {filename or '(no filename)'}")

                if mime_type in ALLOWED_MIME_TYPES:
                    record["hasMedia"] = True
                    cloudinary_url, size_kb = upload_media_to_cloudinary(
                        media_url, mime_type, filename, contact_phone, timestamp
                    )
                    record["media"] = {
                        "mediaId"     : media_id,
                        "filename"    : filename,
                        "mimeType"    : mime_type,
                        "sizeBytes"   : file_size,
                        "sizeKB"      : size_kb,
                        "cloudinaryUrl": cloudinary_url,
                    }
                else:
                    print(f"      ⏭️   Skipped (mime type not allowed: {mime_type})")
                    record["hasMedia"] = True
                    record["media"]    = {
                        "mediaId" : media_id,
                        "mimeType": mime_type,
                        "skipped" : True,
                        "reason"  : "mime type not in allowed list",
                    }
            except Exception as e:
                print(f"      ⚠️  Media error: {e}")
                record["media"] = {"error": str(e)}

    # ── Reply context ───────────────────────────────────
    if "context" in message:
        record["context"] = {
            "replyToMessageId": message["context"].get("id"),
            "replyToPhone"    : message["context"].get("from"),
        }

    print(f"      hasMedia  : {record['hasMedia']}")
    return record


# ─────────────────────────────────────────
#  WEBHOOK ROUTES
# ─────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode", "")
    token     = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    print("mode=" + mode + " token=" + token + " expected=" + VERIFY_TOKEN)

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅  Webhook verified!")
        return challenge, 200
    else:
        print("❌  Failed - token mismatch")
        return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    signature = request.headers.get("X-Hub-Signature-256", "")
    if APP_SECRET and not verify_signature(request.data, signature):
        print("⚠️  Invalid signature — ignoring request")
        return jsonify({"status": "invalid signature"}), 403

    body = request.get_json()
    print(f"\n🔔  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Incoming webhook")

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value    = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                contact_map = {}
                for c in contacts:
                    phone = c.get("wa_id", "unknown")
                    name  = c.get("profile", {}).get("name", "Unknown")
                    contact_map[phone] = name

                for message in messages:
                    sender_phone = message.get("from", "unknown")
                    sender_name  = contact_map.get(sender_phone, "Unknown")
                    record       = process_message(message, sender_name, sender_phone)

                    print(f"\n  📋  Full JSON output:")
                    print(json.dumps(record, indent=2, ensure_ascii=False))

                    append_to_log(record)

    except Exception as e:
        print(f"⚠️  Error processing webhook: {e}")
        import traceback
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("\n🚀  WhatsApp Webhook started")
    print(f"    Webhook URL : http://<your-server>/webhook")
    print(f"    Media       → Cloudinary (whatsapp_media/)")
    print(f"    Log         → Cloudinary ({LOG_PUBLIC_ID}.json)\n")
    app.run(port=5000, debug=False)