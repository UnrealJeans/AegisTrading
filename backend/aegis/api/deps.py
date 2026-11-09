from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import Settings, get_settings
from aegis.core.bus import Bus, get_bus
from aegis.core.security import decode_token
from aegis.db.models import AuditLog
from aegis.db.session import session_scope

bearer = HTTPBearer(auto_error=False)


async def db_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as s:
        yield s


def client_ip(request: Request, settings: Settings) -> str | None:
    if settings.trust_proxy_headers:
        real = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if real:
            return real
    return request.client.host if request.client else None


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    payload = decode_token(settings, creds.credentials) if creds else None
    if not payload or not payload.get("sub"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return str(payload["sub"])


async def audit(session: AsyncSession, actor: str, action: str, request: Request | None, settings: Settings, **detail: object) -> None:
    session.add(AuditLog(actor=actor, action=action, ip=client_ip(request, settings) if request else None, detail=detail))


SessionDep = Annotated[AsyncSession, Depends(db_session)]
UserDep = Annotated[str, Depends(current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
BusDep = Annotated[Bus, Depends(get_bus)]
