from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.foundation.errors import AuthenticationRequired, PermissionDenied, ResourceNotFound
from packages.qbr_core.security.authentication import LoginRateLimiter, authenticate_password, create_hs256_token

from ..dependencies import _principal, _service
from ..schemas import LoginRequest, Principal

router = APIRouter()


@router.post("/api/v1/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    if settings.auth_mode != "password":
        raise ResourceNotFound("Resource not found")
    origin = request.headers.get("Origin")
    if settings.app_env in {"production", "prod"} and origin not in settings.cors_origins:
        raise PermissionDenied("Login origin is not allowed")
    remote = request.client.host if request.client else "unknown"
    limiter_key = hashlib.sha256(f"{remote}:{body.username.casefold()}".encode()).hexdigest()
    limiter: LoginRateLimiter = request.app.state.login_limiter
    limiter.check(limiter_key)
    if not authenticate_password(settings, body.username, body.password):
        limiter.failure(limiter_key)
        raise AuthenticationRequired("Invalid username or password")
    limiter.reset(limiter_key)
    with request.app.state.service.db.transaction(immediate=True) as conn:
        member = conn.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (settings.password_workspace_id, settings.password_user_id),
        ).fetchone()
        if not member:
            raise AuthenticationRequired("Configured login identity is unavailable")
        request.app.state.service.db.audit(
            conn,
            settings.password_workspace_id,
            settings.password_user_id,
            "authentication.login",
            "user",
            settings.password_user_id,
        )
    token = create_hs256_token(
        settings,
        user_id=settings.password_user_id,
        workspace_id=settings.password_workspace_id,
        roles=[str(member["role"])],
        ttl_seconds=settings.session_ttl_seconds,
    )
    response.set_cookie(
        "qbr_session",
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.app_env in {"production", "prod"},
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"user_id": settings.password_user_id, "workspace_id": settings.password_workspace_id, "role": member["role"]}


@router.get("/api/v1/auth/session")
def session(principal: Annotated[Principal, Depends(_principal)], response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return principal.model_dump()


@router.post("/api/v1/auth/logout", status_code=204)
def logout(
    request: Request,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> Response:
    settings: Settings = request.app.state.settings
    with service.db.transaction(immediate=True) as conn:
        service.db.audit(conn, principal.workspace_id, principal.user_id, "authentication.logout", "user", principal.user_id)
    response = Response(status_code=204)
    response.delete_cookie("qbr_session", path="/", secure=settings.app_env in {"production", "prod"}, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response
