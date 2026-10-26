from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from fraud_api.core.config import Settings

_hasher = PasswordHasher()
# Used to equalize timing when the user does not exist.
_DUMMY_HASH = _hasher.hash("timing-equalizer")


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    SERVICE = "service"  # machine clients (payment gateway, replay simulator)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role


def issue_token(settings: Settings, principal: Principal) -> tuple[str, int]:
    now = datetime.now(UTC)
    ttl = settings.jwt_ttl_minutes * 60
    claims = {
        "sub": str(principal.user_id),
        "org": str(principal.organization_id),
        "role": principal.role.value,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return token, ttl


def decode_token(settings: Settings, token: str) -> Principal:
    """Raises ``jwt.PyJWTError`` on any invalid/expired/forged token."""
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],  # pinned: never accept "none" or algorithm confusion
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "org", "role"]},
    )
    try:
        return Principal(
            user_id=uuid.UUID(claims["sub"]),
            organization_id=uuid.UUID(claims["org"]),
            role=Role(claims["role"]),
        )
    except ValueError as exc:
        raise jwt.InvalidTokenError("Malformed claims") from exc
