from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import AuthenticationRequired, PermissionDenied, TooManyRequests

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read", "ask", "feedback"},
    "editor": {"read", "ask", "feedback", "document:write"},
    "reviewer": {"read", "ask", "feedback", "review:write"},
    "admin": {"read", "ask", "feedback", "document:write", "review:write", "analytics:read"},
}


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    workspace_id: str
    user_id: str
    roles: tuple[str, ...]

    def require(self, permission: str) -> None:
        granted = set().union(*(ROLE_PERMISSIONS.get(role, set()) for role in self.roles))
        if permission not in granted:
            raise PermissionDenied(f"Permission {permission!r} is required")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def decode_hs256_token(token: str, settings: Settings) -> AuthPrincipal:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header))
        payload: dict[str, Any] = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationRequired("Invalid bearer token") from exc
    if header.get("alg") != "HS256" or header.get("typ") not in {None, "JWT"}:
        raise AuthenticationRequired("Only HS256 JWT bearer tokens are accepted")
    signed = f"{encoded_header}.{encoded_payload}".encode()
    expected = hmac.new(settings.jwt_secret.encode(), signed, hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(encoded_signature)
    except ValueError as exc:
        raise AuthenticationRequired("Invalid bearer token signature") from exc
    if not hmac.compare_digest(expected, supplied):
        raise AuthenticationRequired("Invalid bearer token signature")
    now = int(time.time())
    if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
        raise AuthenticationRequired("Bearer token has expired")
    if payload.get("nbf") is not None and int(payload["nbf"]) > now:
        raise AuthenticationRequired("Bearer token is not active yet")
    if payload.get("iss") != settings.jwt_issuer or payload.get("aud") != settings.jwt_audience:
        raise AuthenticationRequired("Bearer token issuer or audience is invalid")
    user_id = payload.get("sub")
    workspace_id = payload.get("workspace_id")
    roles = payload.get("roles", [])
    if not isinstance(user_id, str) or not isinstance(workspace_id, str) or not isinstance(roles, list):
        raise AuthenticationRequired("Bearer token is missing identity claims")
    normalized_roles = tuple(role for role in roles if isinstance(role, str) and role in ROLE_PERMISSIONS)
    if not normalized_roles:
        raise AuthenticationRequired("Bearer token contains no recognized role")
    return AuthPrincipal(workspace_id, user_id, normalized_roles)


def create_hs256_token(
    settings: Settings,
    *,
    user_id: str,
    workspace_id: str,
    roles: list[str],
    ttl_seconds: int = 3600,
) -> str:
    """Create a deployment/demo token. Production identity should issue the same claims."""
    now = int(time.time())
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url_encode(
        json.dumps(
            {
                "sub": user_id,
                "workspace_id": workspace_id,
                "roles": roles,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "iat": now,
                "exp": now + ttl_seconds,
            },
            separators=(",", ":"),
        ).encode()
    )
    signed = f"{header}.{payload}".encode()
    signature = _b64url_encode(hmac.new(settings.jwt_secret.encode(), signed, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def create_password_hash(password: str, *, salt: bytes | None = None) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or len(password) > 1024:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=_b64url_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, _b64url_decode(expected))
    except (ValueError, MemoryError):
        return False


def authenticate_password(settings: Settings, username: str, password: str) -> bool:
    password_valid = verify_password(password, settings.password_hash)
    username_valid = hmac.compare_digest(username.encode(), settings.password_username.encode())
    return password_valid and username_valid


class LoginRateLimiter:
    """Per-process limiter for the single-instance public demo."""

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [value for value in self._failures.get(key, []) if now - value < self.window_seconds]
            self._failures[key] = recent
            if len(recent) >= self.max_attempts:
                raise TooManyRequests("Too many login attempts; try again later")

    def failure(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
