from dataclasses import dataclass
import hashlib
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from app.models.entities.storage import AuthSession, User, utcnow

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    user_id: str
    username: str
    role: str
    token_digest: str


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_store(request: Request):
    if request.app.state.store is None:
        raise HTTPException(503, "数据库尚未配置")
    return request.app.state.store


def require_cache(request: Request):
    if request.app.state.cache is None:
        raise HTTPException(503, "缓存尚未配置")
    return request.app.state.cache


def require_model_client(request: Request):
    if request.app.state.model_client is None:
        raise HTTPException(503, "模型客户端尚未配置")
    return request.app.state.model_client


def require_vector_store(request: Request):
    if request.app.state.vector_store is None:
        raise HTTPException(503, "ChromaDB 尚未配置")
    return request.app.state.vector_store


async def current_identity(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != "bearer" or len(credentials.credentials) > 256:
        raise HTTPException(401, "请登录或重新登录", headers={"WWW-Authenticate": "Bearer"})
    digest = digest_token(credentials.credentials)
    async with require_store(request).sessions() as session:
        row = (await session.execute(select(User, AuthSession).join(AuthSession, AuthSession.user_id == User.id)
                                    .where(AuthSession.token_digest == digest))).first()
        if not row or not row[0].enabled or row[1].revoked or row[1].expires_at <= utcnow():
            raise HTTPException(401, "请登录或重新登录", headers={"WWW-Authenticate": "Bearer"})
        return Identity(row[0].id, row[0].username, row[0].role, digest)


async def administrator(identity: Identity = Depends(current_identity)):
    if identity.role != "admin":
        raise HTTPException(403, "没有管理权限")
    return identity
