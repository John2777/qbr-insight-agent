from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from packages.qbr_core import QBRService

from ..dependencies import _service

router = APIRouter()


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(service: Annotated[QBRService, Depends(_service)]) -> dict[str, Any]:
    return service.health()
