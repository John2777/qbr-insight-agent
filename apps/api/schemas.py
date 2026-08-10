from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from packages.qbr_core.auth import AuthPrincipal


class Principal(BaseModel):
    workspace_id: str
    user_id: str
    roles: tuple[str, ...] = ("viewer",)

    def require(self, permission: str) -> None:
        AuthPrincipal(self.workspace_id, self.user_id, self.roles).require(permission)


class ConversationCreate(BaseModel):
    title: str = "New QBR conversation"
    document_ids: list[str] = Field(default_factory=list)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    client_message_id: str | None = None
    response_mode: str = "stream"


class FeedbackCreate(BaseModel):
    rating: int
    category: str | None = None
    comment: str | None = Field(default=None, max_length=2000)


class ReviewResolve(BaseModel):
    resolution: str = "resolved"
    corrected: dict[str, Any] | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
