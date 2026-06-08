"""
routes/settings.py
==================
GET/PUT  /settings/meta          — Meta credentials (phone_number_id, access_token, waba_id)
GET/PUT  /settings/agent         — AI agent config
GET      /settings/agents        — list team agents (users)
POST     /settings/agents        — add new agent user
DELETE   /settings/agents/{id}   — remove agent
GET/POST/DELETE /settings/labels — custom contact labels
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Business, AgentConfig, User, Label
from auth import get_current_user, require_admin, hash_password

router = APIRouter(prefix="/settings", tags=["settings"])


# ── Meta credentials ──────────────────────────────────────────
class MetaConfigRequest(BaseModel):
    phone_number_id: str
    waba_id        : str
    access_token   : str
    verify_token   : str | None = None

@router.get("/meta")
async def get_meta_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    return {
        "phone_number_id": biz.phone_number_id,
        "waba_id"        : biz.waba_id,
        "access_token"   : "***" if biz.access_token else None,  # never expose token
        "verify_token"   : biz.verify_token,
        "configured"     : bool(biz.access_token and biz.phone_number_id),
    }

@router.put("/meta")
async def update_meta_config(
    body: MetaConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    biz = biz_result.scalar_one_or_none()
    biz.phone_number_id = body.phone_number_id
    biz.waba_id         = body.waba_id
    biz.access_token    = body.access_token
    biz.verify_token    = body.verify_token
    await db.commit()
    return {"status": "updated"}


# ── AI Agent config ───────────────────────────────────────────
class AgentConfigRequest(BaseModel):
    name                : str  = "Default Agent"
    enabled             : bool = False
    system_prompt       : str | None = None
    temperature         : float = 0.3
    auto_resolve        : bool = False
    escalate_keywords   : str | None = None
    working_hours_only  : bool = False
    working_hours_start : str  = "09:00"
    working_hours_end   : str  = "18:00"
    language            : str  = "en"

@router.get("/agent")
async def get_agent_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(AgentConfig).where(AgentConfig.business_id == user.business_id)
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        return {"configured": False}
    return {
        "configured"          : True,
        "id"                  : cfg.id,
        "name"                : cfg.name,
        "enabled"             : cfg.enabled,
        "system_prompt"       : cfg.system_prompt,
        "temperature"         : cfg.temperature,
        "auto_resolve"        : cfg.auto_resolve,
        "escalate_keywords"   : cfg.escalate_keywords,
        "working_hours_only"  : cfg.working_hours_only,
        "working_hours_start" : cfg.working_hours_start,
        "working_hours_end"   : cfg.working_hours_end,
        "language"            : cfg.language,
    }

@router.put("/agent")
async def update_agent_config(
    body: AgentConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    result = await db.execute(
        select(AgentConfig).where(AgentConfig.business_id == user.business_id)
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        cfg = AgentConfig(business_id=user.business_id)
        db.add(cfg)

    for field, val in body.model_dump().items():
        setattr(cfg, field, val)

    await db.commit()
    return {"status": "updated"}


# ── Team agents (users) ───────────────────────────────────────
@router.get("/agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(User).where(User.business_id == user.business_id, User.is_active == True)
    )
    users = result.scalars().all()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role} for u in users]

class AddAgentRequest(BaseModel):
    name    : str
    email   : EmailStr
    password: str
    role    : str = "agent"

@router.post("/agents")
async def add_agent(
    body: AddAgentRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    new_user = User(
        business_id   = user.business_id,
        email         = body.email,
        password_hash = hash_password(body.password),
        name          = body.name,
        role          = body.role,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {"id": new_user.id, "name": new_user.name}

@router.delete("/agents/{agent_id}")
async def remove_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    result = await db.execute(
        select(User).where(User.id == agent_id, User.business_id == user.business_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")
    agent.is_active = False
    await db.commit()
    return {"status": "removed"}


# ── Custom Labels ─────────────────────────────────────────────
@router.get("/labels")
async def list_labels(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Label).where(Label.business_id == user.business_id)
    )
    labels = result.scalars().all()
    return [{"id": l.id, "name": l.name, "description": l.description, "color_bg": l.color_bg, "color_text": l.color_text} for l in labels]

class LabelRequest(BaseModel):
    name       : str
    description: str | None = None
    color_bg   : str = "#e8f0fe"
    color_text : str = "#1a56a0"

@router.post("/labels")
async def create_label(
    body: LabelRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    label = Label(business_id=user.business_id, **body.model_dump())
    db.add(label)
    await db.commit()
    await db.refresh(label)
    return {"id": label.id, "name": label.name}

@router.delete("/labels/{label_id}")
async def delete_label(
    label_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.business_id == user.business_id)
    )
    label = result.scalar_one_or_none()
    if not label:
        raise HTTPException(404, "Label not found")
    await db.delete(label)
    await db.commit()
    return {"status": "deleted"}