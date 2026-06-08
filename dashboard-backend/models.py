"""
models.py
=========
SQLAlchemy ORM models for the WhatsApp CRM dashboard.
Tables are auto-created on first run — no manual SQL needed.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, Integer, Boolean,
    DateTime, ForeignKey, Float, Enum
)
from sqlalchemy.orm import relationship
from database import Base

def _now():
    return datetime.now(timezone.utc)

def _uuid():
    return str(uuid.uuid4())


# ── Businesses (tenants) ──────────────────────────────────────
class Business(Base):
    __tablename__ = "businesses"

    id              = Column(String, primary_key=True, default=_uuid)
    name            = Column(String, unique=True, nullable=False)  # matches "account" in RabbitMQ JSON
    phone_number_id = Column(String, nullable=True)
    waba_id         = Column(String, nullable=True)
    access_token    = Column(Text,   nullable=True)
    verify_token    = Column(String, nullable=True)
    webhook_secret  = Column(String, nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), default=_now)

    users       = relationship("User",     back_populates="business")
    contacts    = relationship("Contact",  back_populates="business")
    templates   = relationship("Template", back_populates="business")
    campaigns   = relationship("Campaign", back_populates="business")
    agent_configs = relationship("AgentConfig", back_populates="business")


# ── Users (agents/admins per business) ───────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(String, primary_key=True, default=_uuid)
    business_id   = Column(String, ForeignKey("businesses.id"), nullable=False)
    email         = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    name          = Column(String, nullable=False)
    role          = Column(Enum("admin", "agent", name="user_role"), default="agent")
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), default=_now)

    business          = relationship("Business", back_populates="users")
    assigned_contacts = relationship("Contact", back_populates="assigned_user")


# ── Contacts (customers) ──────────────────────────────────────
class Contact(Base):
    __tablename__ = "contacts"

    id            = Column(String, primary_key=True, default=_uuid)
    business_id   = Column(String, ForeignKey("businesses.id"), nullable=False)
    phone         = Column(String, nullable=False)
    name          = Column(String, nullable=True)
    email         = Column(String, nullable=True)
    company       = Column(String, nullable=True)
    label         = Column(String, default="LEAD")
    status        = Column(Enum("OPEN", "PENDING", "RESOLVED", name="contact_status"), default="OPEN")
    assigned_to   = Column(String, ForeignKey("users.id"), nullable=True)
    note          = Column(Text, nullable=True)
    last_seen     = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=_now)

    business      = relationship("Business", back_populates="contacts")
    assigned_user = relationship("User", back_populates="assigned_contacts")
    messages      = relationship("Message", back_populates="contact")


# ── Messages ──────────────────────────────────────────────────
class Message(Base):
    __tablename__ = "messages"

    id              = Column(String, primary_key=True, default=_uuid)
    business_id     = Column(String, ForeignKey("businesses.id"), nullable=False)
    contact_id      = Column(String, ForeignKey("contacts.id"),   nullable=False)
    phone_number_id = Column(String, nullable=True)
    meta_message_id = Column(String, nullable=True, unique=True)  # wamid from Meta
    direction       = Column(Enum("incoming", "outgoing", name="msg_direction"), default="incoming")
    type            = Column(String, default="text")              # text/image/audio/document
    text            = Column(Text,   nullable=True)
    media_url       = Column(String, nullable=True)
    media_type      = Column(String, nullable=True)
    status          = Column(String, default="delivered")         # sent/delivered/read
    sent_by_agent   = Column(Boolean, default=False)              # True if sent by AI agent
    timestamp       = Column(DateTime(timezone=True), default=_now)
    created_at      = Column(DateTime(timezone=True), default=_now)

    contact  = relationship("Contact", back_populates="messages")


# ── Templates ─────────────────────────────────────────────────
class Template(Base):
    __tablename__ = "templates"

    id          = Column(String, primary_key=True, default=_uuid)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    name        = Column(String, nullable=False)
    category    = Column(String, default="UTILITY")   # UTILITY / MARKETING / AUTHENTICATION
    status      = Column(String, default="PENDING")   # APPROVED / PENDING / REJECTED
    body        = Column(Text,   nullable=False)
    header      = Column(Text,   nullable=True)
    footer      = Column(Text,   nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_now)

    business = relationship("Business", back_populates="templates")


# ── Campaigns (bulk sends) ────────────────────────────────────
class Campaign(Base):
    __tablename__ = "campaigns"

    id          = Column(String, primary_key=True, default=_uuid)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    name        = Column(String, nullable=False)
    template_id = Column(String, ForeignKey("templates.id"), nullable=True)
    created_by  = Column(String, ForeignKey("users.id"),     nullable=True)
    total       = Column(Integer, default=0)
    delivered   = Column(Integer, default=0)
    read        = Column(Integer, default=0)
    failed      = Column(Integer, default=0)
    created_at  = Column(DateTime(timezone=True), default=_now)

    business   = relationship("Business",  back_populates="campaigns")
    template   = relationship("Template")
    recipients = relationship("CampaignRecipient", back_populates="campaign")


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"

    id          = Column(String, primary_key=True, default=_uuid)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False)
    phone       = Column(String, nullable=False)
    status      = Column(String, default="pending")   # pending/sent/delivered/failed
    sent_at     = Column(DateTime(timezone=True), nullable=True)

    campaign = relationship("Campaign", back_populates="recipients")


# ── Agent Config (per business, configurable prompt) ─────────
class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id                  = Column(String, primary_key=True, default=_uuid)
    business_id         = Column(String, ForeignKey("businesses.id"), nullable=False)
    name                = Column(String, default="Default Agent")
    enabled             = Column(Boolean, default=False)
    system_prompt       = Column(Text, nullable=True)    # the main business prompt
    temperature         = Column(Float,   default=0.3)
    auto_resolve        = Column(Boolean, default=False) # auto-mark resolved after reply
    escalate_keywords   = Column(Text,    nullable=True) # comma-separated: "angry,refund,legal"
    working_hours_only  = Column(Boolean, default=False)
    working_hours_start = Column(String,  default="09:00")
    working_hours_end   = Column(String,  default="18:00")
    language            = Column(String,  default="en")
    created_at          = Column(DateTime(timezone=True), default=_now)

    business = relationship("Business", back_populates="agent_configs")


# ── Custom Labels (per business) ──────────────────────────────
class Label(Base):
    __tablename__ = "labels"

    id          = Column(String, primary_key=True, default=_uuid)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    name        = Column(String, nullable=False)
    description = Column(String, nullable=True)
    color_bg    = Column(String, default="#e8f0fe")
    color_text  = Column(String, default="#1a56a0")
    created_at  = Column(DateTime(timezone=True), default=_now)