"""
routes/auth.py
==============
POST /auth/register  — create first admin for a business
POST /auth/login     — returns JWT token
GET  /auth/me        — returns current user info
"""

import traceback
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, Business
from auth import hash_password, verify_password, create_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("routes.auth")


class RegisterRequest(BaseModel):
    business_name : str
    name          : str
    email         : EmailStr
    password      : str

class LoginRequest(BaseModel):
    email    : EmailStr
    password : str


@router.post("/register")
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        # check email not taken
        existing = await db.execute(select(User).where(User.email == body.email))
        if existing.scalar_one_or_none():
            raise HTTPException(400, "Email already registered")

        # get or create business
        biz_result = await db.execute(select(Business).where(Business.name == body.business_name))
        biz = biz_result.scalar_one_or_none()
        if not biz:
            biz = Business(name=body.business_name)
            db.add(biz)
            await db.flush()

        user = User(
            business_id   = biz.id,
            email         = body.email,
            password_hash = hash_password(body.password),
            name          = body.name,
            role          = "admin",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        token = create_token(user.id, biz.id, user.role)
        return {"access_token": token, "token_type": "bearer", "user": {"id": user.id, "name": user.name, "role": user.role}}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Register error: {traceback.format_exc()}")
        raise HTTPException(500, str(e))

@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user   = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")

    token = create_token(user.id, user.business_id, user.role)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user.id, "name": user.name, "role": user.role, "business_id": user.business_id}}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "business_id": user.business_id}