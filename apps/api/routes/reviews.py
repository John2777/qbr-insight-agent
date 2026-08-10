from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from packages.qbr_core import QBRService

from ..dependencies import _principal, _service
from ..schemas import Principal, ReviewResolve

router = APIRouter()


@router.get("/api/v1/review-tasks")
def reviews(
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return {"items": service.list_reviews(principal.workspace_id)}


@router.post("/api/v1/review-tasks/{review_id}/claim")
def claim_review(
    review_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("review:write")
    return service.claim_review(review_id, principal.workspace_id, principal.user_id)


@router.post("/api/v1/review-tasks/{review_id}/resolve")
def resolve_review(
    review_id: str,
    body: ReviewResolve,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("review:write")
    return service.resolve_review(
        review_id,
        principal.workspace_id,
        principal.user_id,
        body.corrected,
        body.resolution,
    )


@router.get("/api/v1/analytics/summary")
def analytics_summary(
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("analytics:read")
    return service.analytics_summary(principal.workspace_id)
