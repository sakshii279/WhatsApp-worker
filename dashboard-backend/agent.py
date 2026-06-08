"""
agent.py
========
AI agent that auto-replies to incoming messages.
- Configurable per business via AgentConfig
- Uses Claude (Anthropic) as the LLM
- Checks escalation keywords → assigns to human if matched
- Respects working hours setting
- Sends reply via Meta Cloud API
"""

import logging
from datetime import datetime, timezone, timedelta

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from models import AgentConfig, Contact, Message, Business
from whatsapp import send_text_message

log = logging.getLogger("agent")

_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
_IST    = timezone(timedelta(hours=5, minutes=30))


async def _get_agent_config(db: AsyncSession, business_id: str) -> AgentConfig | None:
    result = await db.execute(
        select(AgentConfig).where(
            AgentConfig.business_id == business_id,
            AgentConfig.enabled == True,
        )
    )
    return result.scalar_one_or_none()


async def _get_chat_history(db: AsyncSession, contact_id: str, limit: int = 20) -> list[dict]:
    """Fetch last N messages for context."""
    result = await db.execute(
        select(Message)
        .where(Message.contact_id == contact_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    msgs = result.scalars().all()
    history = []
    for m in reversed(msgs):
        role    = "user"      if m.direction == "incoming" else "assistant"
        content = m.text or f"[{m.type} attachment]"
        history.append({"role": role, "content": content})
    return history


def _within_working_hours(cfg: AgentConfig) -> bool:
    if not cfg.working_hours_only:
        return True
    now = datetime.now(_IST).strftime("%H:%M")
    return cfg.working_hours_start <= now <= cfg.working_hours_end


def _should_escalate(text: str, cfg: AgentConfig) -> bool:
    if not cfg.escalate_keywords:
        return False
    keywords = [k.strip().lower() for k in cfg.escalate_keywords.split(",")]
    return any(k in text.lower() for k in keywords)


async def handle_incoming(
    business_id: str,
    contact_id: str,
    message_text: str,
    db_session: AsyncSession | None = None,
) -> None:
    """
    Main agent entry point.
    Called by rabbitmq_consumer for every incoming message.
    """
    async with AsyncSessionLocal() as db:
        cfg = await _get_agent_config(db, business_id)
        if not cfg:
            return  # agent not enabled for this business

        if not _within_working_hours(cfg):
            log.info(f"Outside working hours for business {business_id} — skipping agent")
            return

        # check if chat is already assigned to a human — don't override
        contact_result = await db.execute(select(Contact).where(Contact.id == contact_id))
        contact = contact_result.scalar_one_or_none()
        if not contact:
            return

        if contact.assigned_to:
            log.info(f"Contact {contact_id} assigned to human agent — skipping AI")
            return

        # check escalation keywords
        if _should_escalate(message_text, cfg):
            log.info(f"Escalation keyword detected for contact {contact_id}")
            contact.status = "PENDING"
            await db.commit()
            return

        # get business info for Meta API call
        biz_result = await db.execute(select(Business).where(Business.id == business_id))
        biz = biz_result.scalar_one_or_none()
        if not biz or not biz.access_token or not biz.phone_number_id:
            log.warning(f"Business {business_id} missing Meta credentials — can't send reply")
            return

        # build chat history for context
        history = await _get_chat_history(db, contact_id)

        # call Claude
        system_prompt = cfg.system_prompt or (
            "You are a helpful customer support agent. "
            "Be polite, concise and resolve the customer's issue."
        )

        try:
            response = await _client.messages.create(
                model      = "claude-sonnet-4-20250514",
                max_tokens = 500,
                system     = system_prompt,
                messages   = history or [{"role": "user", "content": message_text}],
                temperature= cfg.temperature,
            )
            reply_text = response.content[0].text.strip()
        except Exception as e:
            log.error(f"Claude API error: {e}")
            return

        # send reply via Meta Cloud API
        sent = await send_text_message(
            phone_number_id = biz.phone_number_id,
            access_token    = biz.access_token,
            to              = contact.phone,
            text            = reply_text,
        )
        if not sent:
            log.error(f"Failed to send agent reply to {contact.phone}")
            return

        # save outgoing message to DB
        out_msg = Message(
            business_id     = business_id,
            contact_id      = contact_id,
            phone_number_id = biz.phone_number_id,
            direction       = "outgoing",
            type            = "text",
            text            = reply_text,
            status          = "sent",
            sent_by_agent   = True,
        )
        db.add(out_msg)

        if cfg.auto_resolve:
            contact.status = "RESOLVED"

        await db.commit()
        log.info(f"Agent replied to {contact.phone}: {reply_text[:60]}...")