"""
routes/bulk.py
==============
POST /bulk/send    — send a template to a list of phone numbers
GET  /bulk         — list all campaigns
GET  /bulk/{id}    — campaign detail with recipient statuses
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, AsyncSessionLocal
from models import Campaign, CampaignRecipient, Template, Business, Contact, Message, User
from auth import get_current_user
from whatsapp import send_template_message

router = APIRouter(prefix="/bulk", tags=["bulk"])


class BulkSendRequest(BaseModel):
    name         : str
    template_id  : str
    phone_numbers: list[str]
    language_code: str = "en"
    components   : list = []


async def _send_campaign(campaign_id: str, access_token: str, phone_number_id: str, template_name: str, language_code: str, components: list):
    """Background task — sends messages one by one, updates recipient status."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
        )
        recipients = result.scalars().all()

        campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
        campaign = campaign_result.scalar_one_or_none()

        for r in recipients:
            sent = await send_template_message(
                phone_number_id = phone_number_id,
                access_token    = access_token,
                to              = r.phone,
                template_name   = template_name,
                language_code   = language_code,
                components      = components,
            )
            r.status  = "sent" if sent else "failed"
            r.sent_at = datetime.now(timezone.utc)
            if sent and campaign:
                campaign.delivered += 1
            elif campaign:
                campaign.failed += 1
            await asyncio.sleep(0.1)  # stay within Meta rate limits

        await db.commit()


@router.post("/send")
async def bulk_send(
    body: BulkSendRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tpl_result = await db.execute(
        select(Template).where(Template.id == body.template_id, Template.business_id == user.business_id)
    )
    tpl = tpl_result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(404, "Template not found")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    if not biz or not biz.access_token or not biz.phone_number_id:
        raise HTTPException(400, "Meta credentials not configured")

    # deduplicate numbers
    phones = list(set(n.strip() for n in body.phone_numbers if n.strip()))

    campaign = Campaign(
        business_id = user.business_id,
        name        = body.name,
        template_id = tpl.id,
        created_by  = user.id,
        total       = len(phones),
    )
    db.add(campaign)
    await db.flush()

    for phone in phones:
        db.add(CampaignRecipient(campaign_id=campaign.id, phone=phone))

    await db.commit()
    await db.refresh(campaign)

    # run sending in background so API returns immediately
    background_tasks.add_task(
        _send_campaign,
        campaign.id,
        biz.access_token,
        biz.phone_number_id,
        tpl.name,
        body.language_code,
        body.components,
    )

    return {"campaign_id": campaign.id, "total": len(phones), "status": "queued"}


@router.get("")
async def list_campaigns(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Campaign)
        .where(Campaign.business_id == user.business_id)
        .order_by(Campaign.created_at.desc())
    )
    campaigns = result.scalars().all()
    return [
        {
            "id"        : c.id,
            "name"      : c.name,
            "total"     : c.total,
            "delivered" : c.delivered,
            "read"      : c.read,
            "failed"    : c.failed,
            "created_at": c.created_at.isoformat(),
        }
        for c in campaigns
    ]


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.business_id == user.business_id)
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    r_result = await db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
    )
    recipients = r_result.scalars().all()

    return {
        "id"        : campaign.id,
        "name"      : campaign.name,
        "total"     : campaign.total,
        "delivered" : campaign.delivered,
        "read"      : campaign.read,
        "failed"    : campaign.failed,
        "created_at": campaign.created_at.isoformat(),
        "recipients": [{"phone": r.phone, "status": r.status, "sent_at": r.sent_at.isoformat() if r.sent_at else None} for r in recipients],
    }