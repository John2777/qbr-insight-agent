from __future__ import annotations

import json
import logging
from typing import Any


def provider_error_diagnostics(exc: Exception) -> dict[str, Any]:
    """Return operationally useful provider fields without prompts or credentials."""
    diagnostics: dict[str, Any] = {"error_type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        diagnostics["status_code"] = status_code
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and request_id:
        diagnostics["provider_request_id"] = request_id[:200]
    return diagnostics


def log_provider_failure(
    logger: logging.Logger,
    *,
    component: str,
    exc: Exception,
    provider: str | None,
    model: str | None,
    run_id: str | None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    diagnostics = provider_error_diagnostics(exc)
    event = {
        "event": "provider_call_failed",
        "component": component,
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "latency_ms": latency_ms,
        **diagnostics,
    }
    logger.warning(
        json.dumps({key: value for key, value in event.items() if value is not None}, separators=(",", ":")),
        exc_info=True,
    )
    return diagnostics
