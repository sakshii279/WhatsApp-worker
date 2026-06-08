"""
rabbitmq_consumer.py
====================
Polls James's RabbitMQ API every N seconds.
For each message:
  1. Parses the double-encoded InputDataJson
  2. Upserts Business (tenant) by account name
  3. Upserts Contact by phone
  4. Saves Message to Postgres
  5. Broadcasts to WebSocket clients for that business
  6. Triggers AI agent if enabled for that business
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models import Business, Contact, Message
from websocket_manager import manager

log = logging.getLogger("rabbitmq_consumer")

_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse IST timestamp string → UTC datetime."""
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S IST")
        dt = dt.replace(tzinfo=_IST)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


async def _get_or_create_business(db: AsyncSession, name: str) -> Business:
    """Get business by account name or create a placeholder."""
    result = await db.execute(select(Business).where(Business.name == name))
    biz = result.scalar_one_or_none()
    if not biz:
        biz = Business(name=name)
        db.add(biz)
        await db.flush()
        log.info(f"Created new business: {name}")
    return biz


async def _get_or_create_contact(db: AsyncSession, business_id: str, phone: str) -> Contact:
    """Get contact by phone+business or create new."""
    result = await db.execute(
        select(Contact).where(
            Contact.business_id == business_id,
            Contact.phone == phone
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        contact = Contact(
            business_id=business_id,
            phone=phone,
            name=phone,  # default name = phone, agent/human can update later
            status="OPEN",
            label="LEAD",
        )
        db.add(contact)
        await db.flush()
        log.info(f"Created new contact: {phone} for business {business_id}")
    else:
        # update last seen
        contact.last_seen = datetime.now(timezone.utc)
    return contact


async def _message_exists(db: AsyncSession, meta_message_id: str) -> bool:
    """Avoid duplicate processing."""
    result = await db.execute(
        select(Message.id).where(Message.meta_message_id == meta_message_id)
    )
    return result.scalar_one_or_none() is not None


async def _process_record(raw: dict) -> None:
    """Parse one RabbitMQ record and save to DB."""
    # InputDataJson is double-encoded — parse the inner string
    inner_str = raw.get("InputDataJson", "{}")
    try:
        data = json.loads(inner_str)
    except json.JSONDecodeError:
        log.error(f"Failed to parse InputDataJson: {inner_str[:100]}")
        return

    account        = data.get("account", "unknown")
    sender         = data.get("sender", "unknown")
    message_id     = data.get("message_id", "")
    timestamp_str  = data.get("timestamp", "")
    msg_type       = data.get("type", "text")
    text           = data.get("text", "")
    phone_number_id = data.get("phone_number_id", "")
    attachments    = data.get("attachments", [])

    async with AsyncSessionLocal() as db:
        # skip if already processed
        if message_id and await _message_exists(db, message_id):
            return

        biz     = await _get_or_create_business(db, account)
        contact = await _get_or_create_contact(db, biz.id, sender)

        # handle media
        media_url  = None
        media_type = None
        if attachments:
            media_url  = attachments[0].get("url") or attachments[0].get("path")
            media_type = attachments[0].get("mime_type") or attachments[0].get("type")

        msg = Message(
            business_id     = biz.id,
            contact_id      = contact.id,
            phone_number_id = phone_number_id,
            meta_message_id = message_id or None,
            direction       = "incoming",
            type            = msg_type,
            text            = text,
            media_url       = media_url,
            media_type      = media_type,
            status          = "delivered",
            sent_by_agent   = False,
            timestamp       = _parse_timestamp(timestamp_str),
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        await db.refresh(contact)

        log.info(f"Saved message {message_id} from {sender} ({account})")

        # broadcast to any open dashboard WebSocket connections for this business
        await manager.broadcast(biz.id, {
            "event"      : "new_message",
            "contact_id" : contact.id,
            "contact_name": contact.name,
            "phone"      : sender,
            "text"       : text,
            "type"       : msg_type,
            "timestamp"  : msg.timestamp.isoformat(),
            "message_id" : str(msg.id),
        })

        # trigger AI agent if enabled
        try:
            from agent import handle_incoming
            await handle_incoming(biz.id, contact.id, text, db_session=None)
        except Exception as e:
            log.warning(f"Agent error: {e}")


async def _poll_once(client: httpx.AsyncClient) -> None:
    """Call James's API once and process all returned messages."""
    try:
        resp = await client.post(
            settings.RABBITMQ_API_URL,
            json={"queue": settings.RABBITMQ_QUEUE},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"RabbitMQ API returned {resp.status_code}")
            return

        data = resp.json()

        # API might return a single record or a list
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = [data]
        else:
            return

        for record in records:
            if record:
                await _process_record(record)

    except httpx.ConnectError:
        log.warning("RabbitMQ API unreachable — will retry")
    except Exception as e:
        log.error(f"Poll error: {e}")


async def start_consumer() -> None:
    """Main polling loop — runs forever as a background task."""
    log.info(f"Starting RabbitMQ consumer — polling {settings.RABBITMQ_API_URL} every {settings.RABBITMQ_POLL_INTERVAL}s")
    async with httpx.AsyncClient() as client:
        while True:
            await _poll_once(client)
            await asyncio.sleep(settings.RABBITMQ_POLL_INTERVAL)