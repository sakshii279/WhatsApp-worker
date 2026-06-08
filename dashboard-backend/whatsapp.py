"""
whatsapp.py
===========
Wrappers for Meta WhatsApp Cloud API calls.
- Send text message
- Send template message
- Fetch approved templates from Meta
"""

import logging
import httpx
from config import settings

log = logging.getLogger("whatsapp")

def _url(phone_number_id: str, path: str) -> str:
    return f"{settings.META_BASE_URL}/{settings.META_API_VERSION}/{phone_number_id}/{path}"

def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


async def send_text_message(
    phone_number_id: str,
    access_token: str,
    to: str,
    text: str,
) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                _url(phone_number_id, "messages"),
                headers=_headers(access_token),
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                return True
            log.error(f"Meta send_text failed: {r.status_code} {r.text}")
            return False
        except Exception as e:
            log.error(f"send_text_message error: {e}")
            return False


async def send_template_message(
    phone_number_id: str,
    access_token: str,
    to: str,
    template_name: str,
    language_code: str = "en",
    components: list = None,
) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components or [],
        },
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                _url(phone_number_id, "messages"),
                headers=_headers(access_token),
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                return True
            log.error(f"Meta send_template failed: {r.status_code} {r.text}")
            return False
        except Exception as e:
            log.error(f"send_template_message error: {e}")
            return False


async def fetch_templates_from_meta(waba_id: str, access_token: str) -> list:
    """Pull approved templates from Meta Business API."""
    url = f"{settings.META_BASE_URL}/{settings.META_API_VERSION}/{waba_id}/message_templates"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                url,
                headers=_headers(access_token),
                params={"limit": 100},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json().get("data", [])
            log.error(f"fetch_templates failed: {r.status_code} {r.text}")
            return []
        except Exception as e:
            log.error(f"fetch_templates error: {e}")
            return []