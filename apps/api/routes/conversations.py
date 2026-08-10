from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from packages.qbr_core import QBRService

from ..dependencies import _principal, _service
from ..events import _sse
from ..schemas import ConversationCreate, FeedbackCreate, MessageCreate, Principal

router = APIRouter()


@router.post("/api/v1/conversations", status_code=201)
def create_conversation(
    body: ConversationCreate,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("ask")
    return service.create_conversation(principal.workspace_id, principal.user_id, body.document_ids, body.title)


@router.get("/api/v1/conversations")
def conversations(
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    return {"items": service.list_conversations(principal.workspace_id, principal.user_id, limit)}


@router.get("/api/v1/conversations/{conversation_id}")
def conversation(
    conversation_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return service.get_conversation(conversation_id, principal.workspace_id, principal.user_id)


@router.delete("/api/v1/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> Response:
    principal.require("ask")
    service.delete_conversation(conversation_id, principal.workspace_id, principal.user_id)
    return Response(status_code=204)


@router.post("/api/v1/conversations/{conversation_id}/messages", status_code=202)
async def ask(
    conversation_id: str,
    body: MessageCreate,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("ask")
    return service.ask(
        conversation_id,
        body.content,
        principal.workspace_id,
        principal.user_id,
        body.client_message_id,
    )


@router.get("/api/v1/runs/{run_id}")
def run_result(
    run_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return service.get_run(run_id, principal.workspace_id)


@router.get("/api/v1/runs/{run_id}/events")
async def run_event_stream(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> StreamingResponse:
    after = int(request.headers.get("Last-Event-ID", "0") or 0)

    async def stream() -> AsyncIterator[str]:
        cursor = after
        while True:
            events = service.run_events(run_id, principal.workspace_id, cursor)
            for event in events:
                cursor = event["id"]
                yield _sse(event)
                if event["event"] in {"completed", "error"}:
                    return
            if await request.is_disconnected():
                return
            run = service.get_run(run_id, principal.workspace_id)
            if run["status"] in {"completed", "failed"} and not events:
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/v1/messages/{message_id}/feedback", status_code=201)
def feedback(
    message_id: str,
    body: FeedbackCreate,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("feedback")
    return service.add_feedback(message_id, principal.workspace_id, principal.user_id, body.rating, body.category, body.comment)
