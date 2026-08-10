from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.auth import (
    LoginRateLimiter,
)
from packages.qbr_core.errors import QBRError
from packages.qbr_core.ids import new_id

from .routes import (
    auth_router,
    conversations_router,
    documents_router,
    health_router,
    jobs_router,
    reviews_router,
)


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

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(documents_router)
    app.include_router(jobs_router)
    app.include_router(reviews_router)
    app.include_router(conversations_router)

    return app


app = create_app()


def run() -> None:
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
