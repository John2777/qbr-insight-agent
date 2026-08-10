from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse

from packages.qbr_core import QBRService
from packages.qbr_core.errors import FileTooLarge

from ..dependencies import _principal, _service
from ..schemas import Principal

router = APIRouter()


@router.post("/api/v1/documents", status_code=202)
async def upload_document(
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
    title: Annotated[str | None, Form()] = None,
    metadata: Annotated[str | None, Form()] = None,
    deduplication: Annotated[str, Form()] = "reuse",
) -> dict[str, Any]:
    principal.require("document:write")
    filename = file.filename or "presentation.pptx"
    if not filename.casefold().endswith(".pptx"):
        from packages.qbr_core.errors import UnsupportedFile

        raise UnsupportedFile("Only .pptx files are accepted by this deployment.")
    parsed_metadata: dict[str, Any] = {}
    if metadata:
        try:
            value = json.loads(metadata)
        except json.JSONDecodeError as exc:
            from packages.qbr_core.errors import Conflict

            raise Conflict("metadata must be valid JSON") from exc
        if not isinstance(value, dict):
            from packages.qbr_core.errors import Conflict

            raise Conflict("metadata must be a JSON object")
        parsed_metadata = value
    temp_dir = service.settings.data_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".pptx", dir=temp_dir)
    temp_path = Path(temp_name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > service.settings.max_upload_mib * 1024 * 1024:
                    raise FileTooLarge(f"Upload exceeds {service.settings.max_upload_mib} MiB.")
                handle.write(chunk)
        return await asyncio.to_thread(
            service.import_document,
            temp_path,
            filename=filename,
            title=title,
            metadata=parsed_metadata,
            deduplication=deduplication,
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
        )
    finally:
        if temp_path.exists():
            temp_path.unlink()
        await file.close()


@router.get("/api/v1/documents")
def documents(
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return {"items": service.list_documents(principal.workspace_id)}


@router.get("/api/v1/documents/{document_id}")
def document(
    document_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return service.get_document(document_id, principal.workspace_id)


@router.delete("/api/v1/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> Response:
    principal.require("document:write")
    service.delete_document(document_id, principal.workspace_id, principal.user_id)
    return Response(status_code=204)


@router.delete("/api/v1/documents/{document_id}/purge")
def purge_document(
    document_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    principal.require("document:purge")
    return service.purge_document(document_id, principal.workspace_id, principal.user_id)


@router.get("/api/v1/document-versions/{version_id}/slides")
def slides(
    version_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return {"items": service.list_slides(version_id, principal.workspace_id)}


@router.get("/api/v1/slides/{slide_id}")
def slide(
    slide_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> dict[str, Any]:
    return service.get_slide(slide_id, principal.workspace_id)


@router.get("/api/v1/slides/{slide_id}/preview")
def slide_preview(
    slide_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> FileResponse:
    path = service.preview_path(slide_id, principal.workspace_id)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/api/v1/slides/{slide_id}/thumbnail")
def slide_thumbnail(
    slide_id: str,
    principal: Annotated[Principal, Depends(_principal)],
    service: Annotated[QBRService, Depends(_service)],
) -> FileResponse:
    path = service.thumbnail_path(slide_id, principal.workspace_id)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})
