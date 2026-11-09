from __future__ import annotations

import ipaddress
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from aegis.config import Settings
from aegis.core.timeutil import utcnow

_hasher = PasswordHasher()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


def create_access_token(settings: Settings, subject: str) -> str:
    now = utcnow()
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes)}
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_token(settings: Settings, token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def ip_allowed(ip: str | None, allowed: str) -> bool:
    """`allowed` is a comma separated CIDR list; empty means unrestricted."""
    nets = [a.strip() for a in allowed.split(",") if a.strip()]
    if not nets:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(n, strict=False) for n in nets)
    except ValueError:
        return False


def mask_secret(value: str) -> str:
    return "" if not value else f"…{value[-4:]}" if len(value) > 8 else "set"
