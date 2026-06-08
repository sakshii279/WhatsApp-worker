"""
routes/chats.py
===============
GET  /chats                        — list all contacts with last message
GET  /chats/{contact_id}/messages  — full message history for a contact
POST /chats/{contact_id}/send      — send a text message to a contact
PUT  /chats/{contact_id}           — update status, assigned_to, label, note
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Contact, Message, User, Business
from auth import get_current_user
from whatsapp import send_text_message

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("")
async def list_chats(
    status: str | None = None,
    assigned_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(Contact).where(Contact.business_id == user.business_id)
    if status:
        q = q.where(Contact.status == status)
    if assigned_to:
        q = q.where(Contact.assigned_to == assigned_to)
    q = q.order_by(desc(Contact.last_seen))

    result   = await db.execute(q)
    contacts = result.scalars().all()

    out = []
    for c in contacts:
        # get last message
        last_msg_result = await db.execute(
            select(Message)
            .where(Message.contact_id == c.id)
            .order_by(desc(Message.timestamp))
            .limit(1)
        )
        last_msg = last_msg_result.scalar_one_or_none()
        out.append({
            "id"         : c.id,
            "phone"      : c.phone,
            "name"       : c.name,
            "email"      : c.email,
            "company"    : c.company,
            "label"      : c.label,
            "status"     : c.status,
            "assigned_to": c.assigned_to,
            "note"       : c.note,
            "last_seen"  : c.last_seen.isoformat() if c.last_seen else None,
            "last_message": {
                "text"     : last_msg.text,
                "type"     : last_msg.type,
                "direction": last_msg.direction,
                "timestamp": last_msg.timestamp.isoformat(),
            } if last_msg else None,
        })
    return out


@router.get("/{contact_id}/messages")
async def get_messages(
    contact_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # verify contact belongs to this business
    c_result = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.business_id == user.business_id)
    )
    if not c_result.scalar_one_or_none():
        raise HTTPException(404, "Contact not found")

    result = await db.execute(
        select(Message)
        .where(Message.contact_id == contact_id)
        .order_by(Message.timestamp)
        .limit(limit)
    )
    msgs = result.scalars().all()
    return [
        {
            "id"           : m.id,
            "direction"    : m.direction,
            "type"         : m.type,
            "text"         : m.text,
            "media_url"    : m.media_url,
            "status"       : m.status,
            "sent_by_agent": m.sent_by_agent,
            "timestamp"    : m.timestamp.isoformat(),
        }
        for m in msgs
    ]


class SendMessageRequest(BaseModel):
    text: str

@router.post("/{contact_id}/send")
async def send_message(
    contact_id: str,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c_result = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.business_id == user.business_id)
    )
    contact = c_result.scalar_one_or_none()
    if not contact:
        raise HTTPException(404, "Contact not found")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    if not biz or not biz.access_token or not biz.phone_number_id:
        raise HTTPException(400, "Meta credentials not configured — go to Settings")

    sent = await send_text_message(
        phone_number_id = biz.phone_number_id,
        access_token    = biz.access_token,
        to              = contact.phone,
        text            = body.text,
    )
    if not sent:
        raise HTTPException(502, "Failed to send message via Meta API")

    msg = Message(
        business_id     = user.business_id,
        contact_id      = contact_id,
        phone_number_id = biz.phone_number_id,
        direction       = "outgoing",
        type            = "text",
        text            = body.text,
        status          = "sent",
        sent_by_agent   = False,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return {"id": msg.id, "status": "sent"}


class UpdateContactRequest(BaseModel):
    status     : str | None = None
    assigned_to: str | None = None
    label      : str | None = None
    note       : str | None = None
    name       : str | None = None

@router.put("/{contact_id}")
async def update_contact(
    contact_id: str,
    body: UpdateContactRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c_result = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.business_id == user.business_id)
    )
    contact = c_result.scalar_one_or_none()
    if not contact:
        raise HTTPException(404, "Contact not found")

    for field, val in body.model_dump(exclude_none=True).items():
        setattr(contact, field, val)

    await db.commit()
    return {"status": "updated"}