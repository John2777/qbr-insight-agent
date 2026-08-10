from __future__ import annotations

from typing import Annotated

from fastapi import Header, Request

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.auth import decode_hs256_token
from packages.qbr_core.errors import AuthenticationRequired, ResourceNotFound

from .schemas import Principal


def _service(request: Request) -> QBRService:
    return request.app.state.service


def _principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_workspace_id: Annotated[str, Header()] = "ws_demo",
    x_user_id: Annotated[str, Header()] = "user_demo",
) -> Principal:
    service: QBRService = request.app.state.service
    settings: Settings = request.app.state.settings
    if settings.auth_mode in {"jwt", "password"}:
        if settings.auth_mode == "jwt":
            token = authorization.removeprefix("Bearer ").strip() if authorization and authorization.startswith("Bearer ") else None
        else:
            # Public-demo browser sessions are cookie-only. This avoids accepting
            # stale bearer tokens that may have been stored by an older build.
            token = request.cookies.get("qbr_session")
        if not token:
            raise AuthenticationRequired("Sign in is required")
        identity = decode_hs256_token(token, settings)
        x_workspace_id = identity.workspace_id
        x_user_id = identity.user_id
    with service.db.read() as conn:
        member = conn.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (x_workspace_id, x_user_id),
        ).fetchone()
    if not member:
        raise ResourceNotFound("Resource not found")
    return Principal(workspace_id=x_workspace_id, user_id=x_user_id, roles=(str(member["role"]),))
