from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, issue_token, verify_password
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: Annotated[AsyncSession, Depends(get_session)]):
    user = (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return TokenResponse(access_token=issue_token(user))


@router.post("/refresh", response_model=TokenResponse)
async def refresh(user: Annotated[User, Depends(get_current_user)]):
    return TokenResponse(access_token=issue_token(user))
