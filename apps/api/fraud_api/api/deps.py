from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

import jwt
import redis
import redis.asyncio as aioredis
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from fraud_api.core.config import Settings
from fraud_api.core.errors import APIError
from fraud_api.core.security import Principal, Role, decode_token
from fraud_api.scoring.service import ScoringContext
from fraud_api.stream.keys import StreamKeys

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_scoring_context(request: Request) -> ScoringContext:
    return request.app.state.scoring  # type: ignore[no-any-return]


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    unauthorized = APIError(
        401,
        "unauthorized",
        "Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized
    try:
        return decode_token(settings, creds.credentials)
    except jwt.PyJWTError as exc:
        raise unauthorized from exc


def require_roles(*roles: Role) -> Callable[[Principal], Principal]:
    allowed = frozenset(roles)

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.role not in allowed:
            raise APIError(403, "forbidden", "Insufficient role for this operation")
        return principal

    return dependency


SessionDep = Annotated[Session, Depends(get_session)]


def get_stream_keys(settings: Annotated[Settings, Depends(get_settings)]) -> StreamKeys:
    return StreamKeys(settings.stream_prefix, settings.stream_shards)


def get_redis(request: Request) -> redis.Redis:
    client = request.app.state.redis
    if client is None:
        raise APIError(503, "stream_unavailable", "Event streaming is not configured")
    return client  # type: ignore[no-any-return]


def get_redis_async(request: Request) -> aioredis.Redis:
    client = request.app.state.redis_async
    if client is None:
        raise APIError(503, "stream_unavailable", "Event streaming is not configured")
    return client  # type: ignore[no-any-return]
