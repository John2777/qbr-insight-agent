from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from packages.qbr_core import QBRService

from ..dependencies import _principal, _service
from ..events import _sse
from ..schemas import Principal

router = APIRouter()


@router.get("/api/v1/jobs/{job_id}")
def job(
    job_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return service.get_job(job_id, principal.workspace_id)


@router.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("document:write")
    return service.cancel_job(job_id, principal.workspace_id)


@router.post("/api/v1/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("document:write")
    return service.retry_job(job_id, principal.workspace_id)


@router.get("/api/v1/jobs/{job_id}/events")
async def job_event_stream(
    job_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> StreamingResponse:
    after = int(request.headers.get("Last-Event-ID", "0") or 0)

    async def stream() -> AsyncIterator[str]:
        cursor = after
        while True:
            events = service.job_events(job_id, principal.workspace_id, cursor)
            for event in events:
                cursor = event["id"]
                yield _sse(event)
                if event["event"] in {"completed", "error"}:
                    return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
