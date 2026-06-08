"""
routes/templates.py
===================
GET    /templates          — list all templates for this business
POST   /templates          — create new template locally
DELETE /templates/{id}     — delete template
POST   /templates/sync     — pull approved templates from Meta API
POST   /templates/{id}/send — send template to a contact
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Template, Business, Contact, Message, User
from auth import get_current_user
from whatsapp import fetch_templates_from_meta, send_template_message

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Template).where(Template.business_id == user.business_id)
    )
    templates = result.scalars().all()
    return [
        {
            "id"        : t.id,
            "name"      : t.name,
            "category"  : t.category,
            "status"    : t.status,
            "body"      : t.body,
            "header"    : t.header,
            "footer"    : t.footer,
            "created_at": t.created_at.isoformat(),
        }
        for t in templates
    ]


class CreateTemplateRequest(BaseModel):
    name    : str
    category: str = "UTILITY"
    body    : str
    header  : str | None = None
    footer  : str | None = None

@router.post("")
async def create_template(
    body: CreateTemplateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tpl = Template(
        business_id = user.business_id,
        name        = body.name,
        category    = body.category,
        status      = "PENDING",
        body        = body.body,
        header      = body.header,
        footer      = body.footer,
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return {"id": tpl.id, "status": "created"}


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Template).where(Template.id == template_id, Template.business_id == user.business_id)
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(404, "Template not found")
    await db.delete(tpl)
    await db.commit()
    return {"status": "deleted"}


@router.post("/sync")
async def sync_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pull approved templates from Meta and upsert into local DB."""
    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    if not biz or not biz.access_token or not biz.waba_id:
        raise HTTPException(400, "Meta credentials not configured")

    meta_templates = await fetch_templates_from_meta(biz.waba_id, biz.access_token)
    synced = 0
    for mt in meta_templates:
        # check if already exists
        existing = await db.execute(
            select(Template).where(
                Template.business_id == user.business_id,
                Template.name == mt.get("name"),
            )
        )
        tpl = existing.scalar_one_or_none()
        components = mt.get("components", [])
        body_text  = next((c.get("text","") for c in components if c.get("type") == "BODY"), "")
        header_text = next((c.get("text") for c in components if c.get("type") == "HEADER"), None)
        footer_text = next((c.get("text") for c in components if c.get("type") == "FOOTER"), None)

        if tpl:
            tpl.status = mt.get("status", "PENDING")
            tpl.body   = body_text
        else:
            tpl = Template(
                business_id = user.business_id,
                name        = mt.get("name"),
                category    = mt.get("category", "UTILITY"),
                status      = mt.get("status", "PENDING"),
                body        = body_text,
                header      = header_text,
                footer      = footer_text,
            )
            db.add(tpl)
        synced += 1

    await db.commit()
    return {"synced": synced}


class SendTemplateRequest(BaseModel):
    contact_id    : str
    language_code : str = "en"
    components    : list = []

@router.post("/{template_id}/send")
async def send_template(
    template_id: str,
    body: SendTemplateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tpl_result = await db.execute(
        select(Template).where(Template.id == template_id, Template.business_id == user.business_id)
    )
    tpl = tpl_result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(404, "Template not found")

    contact_result = await db.execute(
        select(Contact).where(Contact.id == body.contact_id, Contact.business_id == user.business_id)
    )
    contact = contact_result.scalar_one_or_none()
    if not contact:
        raise HTTPException(404, "Contact not found")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    if not biz or not biz.access_token or not biz.phone_number_id:
        raise HTTPException(400, "Meta credentials not configured")

    sent = await send_template_message(
        phone_number_id = biz.phone_number_id,
        access_token    = biz.access_token,
        to              = contact.phone,
        template_name   = tpl.name,
        language_code   = body.language_code,
        components      = body.components,
    )
    if not sent:
        raise HTTPException(502, "Failed to send template via Meta API")

    msg = Message(
        business_id     = user.business_id,
        contact_id      = contact.id,
        phone_number_id = biz.phone_number_id,
        direction       = "outgoing",
        type            = "template",
        text            = tpl.body,
        status          = "sent",
        sent_by_agent   = False,
    )
    db.add(msg)
    await db.commit()
    return {"status": "sent"}