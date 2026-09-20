from datetime import timedelta
import secrets
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, update
from app.api.dependencies import Identity, current_identity, digest_token, require_cache, require_store
from app.core.application.auth import dummy_hash, verify_password
from app.models.entities.storage import AuthSession, RequestRecord, User, utcnow

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: SecretStr = Field(min_length=1, max_length=128)


@router.post("/login")
async def login(body: LoginInput, request: Request, response: Response):
    started = time.monotonic()
    store, cache = require_store(request), require_cache(request)
    username = body.username.lower()
    reservation = await cache.reserve_login(username)
    async with store.sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
        valid = await verify_password(user.password_hash if user else dummy_hash, body.password.get_secret_value())
        if not user or not user.enabled or not valid:
            raise HTTPException(401, "用户名或密码错误")
        await cache.login_success(reservation)
        token = secrets.token_urlsafe(48)
        expires = utcnow() + timedelta(seconds=request.app.state.settings.token_ttl_seconds)
        session.add(AuthSession(token_digest=digest_token(token), user_id=user.id, expires_at=expires))
        session.add(RequestRecord(request_id=request.state.request_id, user_id=user.id, action="login", status="success",
                                  duration_ms=int((time.monotonic() - started) * 1000)))
        await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": token, "token_type": "bearer", "expires_at": expires}


@router.post("/logout", status_code=204)
async def logout(request: Request, identity: Identity = Depends(current_identity)):
    async with require_store(request).sessions() as session:
        await session.execute(update(AuthSession).where(AuthSession.token_digest == identity.token_digest).values(revoked=True))
        session.add(RequestRecord(request_id=request.state.request_id, user_id=identity.user_id, action="logout", status="success"))
        await session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/me")
async def me(identity: Identity = Depends(current_identity)):
    return {"id": identity.user_id, "username": identity.username, "role": identity.role}
