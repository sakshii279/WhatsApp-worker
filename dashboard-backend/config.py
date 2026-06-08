"""
config.py
=========
All settings loaded from .env file.
"""

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Postgres (Supabase or self-hosted)
    DATABASE_URL: str                        # postgresql+asyncpg://user:pass@host/db

    # JWT Auth
    JWT_SECRET: str
    JWT_ALGORITHM: str   = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24       # 24 hours

    # Azure Service Bus
    SERVICEBUS_CONNECTION_STRING: str
    SERVICEBUS_TOPIC: str        = "whatsapp-messages"
    SERVICEBUS_SUBSCRIPTION: str = "dashboard-sub"

    # AI Agent (Anthropic Claude)
    ANTHROPIC_API_KEY: str = ""

    # Meta WhatsApp Cloud API
    META_API_VERSION: str = "v19.0"
    META_BASE_URL: str    = "https://graph.facebook.com"

    class Config:
        env_file = ".env"

settings = Settings()