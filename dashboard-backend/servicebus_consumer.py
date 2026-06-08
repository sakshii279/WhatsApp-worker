"""
servicebus_consumer.py
======================
Reads messages from Azure Service Bus queue (whatsApp_data).
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

from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models import Business, Contact, Message
from websocket_manager import manager

log = logging.getLogger("servicebus_consumer")

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
    result = await db.execute(select(Business).where(Business.name == name))
    biz = result.scalar_one_or_none()
    if not biz:
        biz = Business(name=name)
        db.add(biz)
        await db.flush()
        log.info(f"Created new business: {name}")
    return biz


async def _get_or_create_contact(db: AsyncSession, business_id: str, phone: str) -> Contact:
    result = await db.execute(
        select(Contact).where(
            Contact.business_id == business_id,
            Contact.phone == phone,
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        contact = Contact(
            business_id = business_id,
            phone       = phone,
            name        = phone,   # default name = phone, can be updated later
            status      = "OPEN",
            label       = "LEAD",
        )
        db.add(contact)
        await db.flush()
        log.info(f"Created new contact: {phone} for business {business_id}")
    else:
        contact.last_seen = datetime.now(timezone.utc)
    return contact


async def _message_exists(db: AsyncSession, meta_message_id: str) -> bool:
    """Prevent duplicate processing if Service Bus redelivers."""
    result = await db.execute(
        select(Message.id).where(Message.meta_message_id == meta_message_id)
    )
    return result.scalar_one_or_none() is not None


async def _process_message(raw_body: str) -> None:
    """Parse one Service Bus message body and persist to DB."""
    try:
        outer = json.loads(raw_body)
    except json.JSONDecodeError:
        log.error(f"Failed to parse Service Bus message body: {raw_body[:100]}")
        return

    # InputDataJson is double-encoded — parse the inner JSON string
    inner_str = outer.get("InputDataJson", "{}")
    try:
        data = json.loads(inner_str)
    except json.JSONDecodeError:
        log.error(f"Failed to parse InputDataJson: {inner_str[:100]}")
        return

    account         = data.get("account", "unknown")
    sender          = data.get("sender", "unknown")
    message_id      = data.get("message_id", "")
    timestamp_str   = data.get("timestamp", "")
    msg_type        = data.get("type", "text")
    text            = data.get("text", "")
    phone_number_id = data.get("phone_number_id", "")
    attachments     = data.get("attachments", [])

    async with AsyncSessionLocal() as db:
        # skip if already saved (Service Bus at-least-once delivery)
        if message_id and await _message_exists(db, message_id):
            log.info(f"Skipping duplicate message: {message_id}")
            return

        biz     = await _get_or_create_business(db, account)
        contact = await _get_or_create_contact(db, biz.id, sender)

        # handle attachments
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

        # push to any open dashboard WebSocket for this business
        await manager.broadcast(biz.id, {
            "event"       : "new_message",
            "contact_id"  : contact.id,
            "contact_name": contact.name,
            "phone"       : sender,
            "text"        : text,
            "type"        : msg_type,
            "timestamp"   : msg.timestamp.isoformat(),
            "message_id"  : str(msg.id),
        })

        # trigger AI agent if enabled for this business
        try:
            from agent import handle_incoming
            await handle_incoming(biz.id, contact.id, text, db_session=None)
        except Exception as e:
            log.warning(f"Agent error: {e}")


async def start_consumer() -> None:
    """
    Main Service Bus consumer loop.
    Runs forever — reconnects automatically on failure.
    """
    log.info(f"Starting Service Bus consumer on topic: {settings.SERVICEBUS_TOPIC} subscription: {settings.SERVICEBUS_SUBSCRIPTION}")

    while True:
        try:
            async with ServiceBusClient.from_connection_string(
                settings.SERVICEBUS_CONNECTION_STRING,
                logging_enable=False,
            ) as client:
                async with client.get_subscription_receiver(
                    topic_name        = settings.SERVICEBUS_TOPIC,
                    subscription_name = settings.SERVICEBUS_SUBSCRIPTION,
                    max_wait_time     = 5,
                ) as receiver:
                    log.info("Service Bus receiver connected — waiting for messages...")
                    async for sb_message in receiver:
                        try:
                            # message body is bytes — decode to string
                            body = b"".join(sb_message.body).decode("utf-8")
                            await _process_message(body)
                            # acknowledge — removes message from queue
                            await receiver.complete_message(sb_message)
                        except Exception as e:
                            log.error(f"Error processing message: {e}")
                            # abandon — message goes back to queue for retry
                            await receiver.abandon_message(sb_message)

        except Exception as e:
            log.error(f"Service Bus connection error: {e} — retrying in 5s")
            await asyncio.sleep(5)