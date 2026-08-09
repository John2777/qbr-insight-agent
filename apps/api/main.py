from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.auth import (
    AuthPrincipal,
    LoginRateLimiter,
    authenticate_password,
    create_hs256_token,
    decode_hs256_token,
)
from packages.qbr_core.errors import AuthenticationRequired, FileTooLarge, PermissionDenied, QBRError, ResourceNotFound
from packages.qbr_core.ids import new_id


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


async def _inline_worker(app: FastAPI) -> None:
    service: QBRService = app.state.service
    while True:
        with suppress(Exception):
            worker_id = f"api-{os.getpid()}"
            processed_run = await asyncio.to_thread(service.process_next_run, worker_id)
            if not processed_run:
                await asyncio.to_thread(service.process_next_job, worker_id)
        await asyncio.sleep(service.settings.worker_poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    task: asyncio.Task[None] | None = None
    if settings.run_inline_worker:
        task = asyncio.create_task(_inline_worker(app))
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(message)s")
    logger = logging.getLogger("qbr.api")
    app = FastAPI(
        title="QBR Insight Agent API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = QBRService(settings)
    app.state.login_limiter = LoginRateLimiter(settings.login_max_attempts, settings.login_window_seconds)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Workspace-ID", "X-User-ID", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or new_id("req")
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
                separators=(",", ":"),
            )
        )
        return response

    @app.exception_handler(QBRError)
    async def qbr_error(request: Request, exc: QBRError) -> JSONResponse:
        response = JSONResponse(
            status_code=exc.status_code,
            media_type="application/problem+json",
            content={
                "type": f"https://qbr-agent.local/problems/{exc.code.lower().replace('_', '-')}",
                "title": exc.title,
                "status": exc.status_code,
                "code": exc.code,
                "detail": exc.detail,
                "request_id": getattr(request.state, "request_id", "unknown"),
                "errors": exc.errors,
            },
        )
        if exc.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        if exc.status_code == 429:
            response.headers["Retry-After"] = str(settings.login_window_seconds)
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(service: Annotated[QBRService, Depends(_service)]) -> dict[str, Any]:
        return service.health()

    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
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
        with app.state.service.db.transaction(immediate=True) as conn:
            member = conn.execute(
                "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (settings.password_workspace_id, settings.password_user_id),
            ).fetchone()
            if not member:
                raise AuthenticationRequired("Configured login identity is unavailable")
            app.state.service.db.audit(
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

    @app.get("/api/v1/auth/session")
    def session(principal: Annotated[Principal, Depends(_principal)], response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return principal.model_dump()

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> Response:
        with service.db.transaction(immediate=True) as conn:
            service.db.audit(conn, principal.workspace_id, principal.user_id, "authentication.logout", "user", principal.user_id)
        response = Response(status_code=204)
        response.delete_cookie("qbr_session", path="/", secure=settings.app_env in {"production", "prod"}, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/v1/documents", status_code=202)
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

    @app.get("/api/v1/documents")
    def documents(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return {"items": service.list_documents(principal.workspace_id)}

    @app.get("/api/v1/documents/{document_id}")
    def document(
        document_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get_document(document_id, principal.workspace_id)

    @app.delete("/api/v1/documents/{document_id}", status_code=204)
    def delete_document(
        document_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> Response:
        principal.require("document:write")
        service.delete_document(document_id, principal.workspace_id, principal.user_id)
        return Response(status_code=204)

    @app.delete("/api/v1/documents/{document_id}/purge")
    def purge_document(
        document_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("document:purge")
        return service.purge_document(document_id, principal.workspace_id, principal.user_id)

    @app.get("/api/v1/document-versions/{version_id}/slides")
    def slides(
        version_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return {"items": service.list_slides(version_id, principal.workspace_id)}

    @app.get("/api/v1/slides/{slide_id}")
    def slide(
        slide_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get_slide(slide_id, principal.workspace_id)

    @app.get("/api/v1/slides/{slide_id}/preview")
    def slide_preview(
        slide_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> FileResponse:
        path = service.preview_path(slide_id, principal.workspace_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})

    @app.get("/api/v1/jobs/{job_id}")
    def job(
        job_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get_job(job_id, principal.workspace_id)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("document:write")
        return service.cancel_job(job_id, principal.workspace_id)

    @app.post("/api/v1/jobs/{job_id}/retry")
    def retry_job(
        job_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("document:write")
        return service.retry_job(job_id, principal.workspace_id)

    @app.get("/api/v1/jobs/{job_id}/events")
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

    @app.get("/api/v1/review-tasks")
    def reviews(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return {"items": service.list_reviews(principal.workspace_id)}

    @app.post("/api/v1/review-tasks/{review_id}/claim")
    def claim_review(
        review_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("review:write")
        return service.claim_review(review_id, principal.workspace_id, principal.user_id)

    @app.post("/api/v1/review-tasks/{review_id}/resolve")
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

    @app.get("/api/v1/analytics/summary")
    def analytics_summary(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("analytics:read")
        return service.analytics_summary(principal.workspace_id)

    @app.post("/api/v1/conversations", status_code=201)
    def create_conversation(
        body: ConversationCreate,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("ask")
        return service.create_conversation(
            principal.workspace_id, principal.user_id, body.document_ids, body.title
        )

    @app.get("/api/v1/conversations")
    def conversations(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        return {"items": service.list_conversations(principal.workspace_id, principal.user_id, limit)}

    @app.get("/api/v1/conversations/{conversation_id}")
    def conversation(
        conversation_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get_conversation(conversation_id, principal.workspace_id, principal.user_id)

    @app.post("/api/v1/conversations/{conversation_id}/messages", status_code=202)
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

    @app.get("/api/v1/runs/{run_id}")
    def run_result(
        run_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get_run(run_id, principal.workspace_id)

    @app.get("/api/v1/runs/{run_id}/events")
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

    @app.post("/api/v1/messages/{message_id}/feedback", status_code=201)
    def feedback(
        message_id: str,
        body: FeedbackCreate,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[QBRService, Depends(_service)],
    ) -> dict[str, Any]:
        principal.require("feedback")
        return service.add_feedback(
            message_id, principal.workspace_id, principal.user_id, body.rating, body.category, body.comment
        )

    return app


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['event']}\nid: {event['id']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


app = create_app()


def run() -> None:
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
