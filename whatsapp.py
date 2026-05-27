import os
import json
import hmac
import hashlib
import requests
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify

# ─────────────────────────────────────────
#  CONFIG — fill these in from Meta Dashboard
# ─────────────────────────────────────────
VERIFY_TOKEN    = "workflow"
WHATSAPP_TOKEN  = "YOUR_WHATSAPP_TOKEN"
APP_SECRET      = "YOUR_APP_SECRET"
PHONE_NUMBER_ID = "YOUR_PHONE_NUMBER_ID" # from Meta Dashboard → WhatsApp → API Setup
DOWNLOAD_DIR    = "media"
METADATA_LOG    = "whatsapp_log.json"

ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/x-iwork-numbers-sffnumbers",
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
    "application/x-iwork-numbers-sffnumbers": ".numbers",
}

app = Flask(__name__)


# ─────────────────────────────────────────
#  NGROK FIX - bypass browser warning page
# ─────────────────────────────────────────
@app.after_request
def add_ngrok_header(response):
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


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
#  MEDIA DOWNLOAD
# ─────────────────────────────────────────
def get_media_url(media_id):
    url     = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data.get("url"), data.get("mime_type"), data.get("file_size")


def download_media(media_url, mime_type, filename_hint, sender, timestamp):
    headers  = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    response = requests.get(media_url, headers=headers, stream=True)
    response.raise_for_status()

    date_str = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
    ext      = EXTENSION_MAP.get(mime_type, "")

    if filename_hint and "." in filename_hint:
        filename = filename_hint.strip()
    else:
        ts       = datetime.fromtimestamp(int(timestamp)).strftime("%H%M%S")
        filename = f"media_{ts}{ext}"

    save_path = Path(DOWNLOAD_DIR) / sender / date_str / filename
    save_path.parent.mkdir(parents=True, exist_ok=True)

    with open(save_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    size_kb = round(save_path.stat().st_size / 1024, 2)
    print(f"      💾  Saved: {filename} ({size_kb} KB) → {save_path}")
    return str(save_path), size_kb

# ─────────────────────────────────────────
#  SEND MESSAGE
# ─────────────────────────────────────────
def send_whatsapp_message(to_phone, text):
    url     = f"https://graph.facebook.com/v19.0/{"1151798138012301"}/messages"
    headers = {
        "Authorization": f"Bearer {"EAAVZBeZCcyzEwBRWgn2qVMZBn3ustzDdWw01I0lR9SXAXwq05VqwYTfbgUvNLNHxApEP9SAQnpVnZBJpFFK0k0YMtePsEM88B8cuZA36PhYkINmOzxVkwLcGrquAdIbpxImeYR784CykmE4Gg1t8dx225JLZBZCQ9f9iLLzQP8D8fYZCytYcH8ttZAve2TpJGnKNPAwZDZD"}",
        "Content-Type" : "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to"               : to_phone,
        "type"             : "text",
        "text"             : {"body": text},
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"      ✉️   Ack sent to {to_phone}")
    except Exception as e:
        print(f"      ⚠️  Failed to send ack to {to_phone}: {e}")

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

    if msg_type == "text":
        record["text"] = message.get("text", {}).get("body", "")
        print(f"      Text      : {record['text'][:150]}")

    elif msg_type in ("image", "document", "video", "audio", "sticker"):
        media_data = message.get(msg_type, {})
        media_id   = media_data.get("id")
        filename   = media_data.get("filename", "")
        caption    = media_data.get("caption", "")
        record["caption"] = caption if caption else None

        if media_id:
            try:
                media_url, mime_type, file_size = get_media_url(media_id)
                print(f"      MIME      : {mime_type}")
                print(f"      Filename  : {filename or '(no filename)'}")

                if mime_type in ALLOWED_MIME_TYPES:
                    record["hasMedia"] = True
                    saved_path, size_kb = download_media(
                        media_url, mime_type, filename, contact_phone, timestamp
                    )
                    record["media"] = {
                        "mediaId"  : media_id,
                        "filename" : filename or Path(saved_path).name,
                        "mimeType" : mime_type,
                        "sizeBytes": file_size,
                        "sizeKB"   : size_kb,
                        "savedPath": saved_path,
                    }
                else:
                    print(f"      ⏭️   Skipped (mime type not in allowed list: {mime_type})")
                    record["hasMedia"] = True
                    record["media"] = {
                        "mediaId" : media_id,
                        "mimeType": mime_type,
                        "skipped" : True,
                        "reason"  : "mime type not in allowed list",
                    }
            except Exception as e:
                print(f"      ⚠️  Media download failed: {e}")
                record["media"] = {"error": str(e)}

    if "context" in message:
        record["context"] = {
            "replyToMessageId": message["context"].get("id"),
            "replyToPhone"    : message["context"].get("from"),
        }

    print(f"      hasMedia  : {record['hasMedia']}")
    return record


# ─────────────────────────────────────────
#  LOG
# ─────────────────────────────────────────
def append_to_log(record):
    log = []
    if os.path.exists(METADATA_LOG):
        with open(METADATA_LOG) as f:
            log = json.load(f)
    log.append(record)
    with open(METADATA_LOG, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────
#  WEBHOOK ROUTES
# ─────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode", "")
    token     = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅  Webhook verified!")
        return challenge, 200
    else:
        print("❌  Webhook verification failed — token mismatch")
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
                    print(f"\n  📝  Logged to {METADATA_LOG}")

                    ack = "Thank you! We have received your message and will get back to you shortly."
                    send_whatsapp_message(sender_phone, ack)

    except Exception as e:
        print(f"⚠️  Error processing webhook: {e}")
        import traceback
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
    print("\n🚀  WhatsApp Attachment Downloader started")
    print(f"    Webhook URL  : http://<your-server>:5000/webhook")
    print(f"    Allowed types: Images, PDFs, Excel files, CSV, Apple Numbers")
    print(f"    Media saved  → ./{DOWNLOAD_DIR}/<phone>/<date>/")
    print(f"    Log file     → ./{METADATA_LOG}")
    app.run(port=5000, debug=False)