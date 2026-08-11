from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from packages.qbr_core.foundation.database import Database
from packages.qbr_core.foundation.serialization import _loads


def answer_deltas(answer: str, chunk_size: int = 48) -> tuple[str, ...]:
    """Split an answer without dropping long runs of non-whitespace text."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    return tuple(answer[offset : offset + chunk_size] for offset in range(0, len(answer), chunk_size)) or ("",)


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _usage_counts(source: dict[str, Any] | None) -> dict[str, int]:
    source = source or {}
    input_tokens = _token_count(source.get("input_tokens"))
    output_tokens = _token_count(source.get("output_tokens"))
    total_tokens = _token_count(source.get("total_tokens")) or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def answer_metrics(
    *,
    created_at: str,
    completed_at: str,
    planner_diagnostics: dict[str, Any] | None,
    answer_model: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        duration_ms = max(0, round((completed - created).total_seconds() * 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    components = {
        "planner": _usage_counts(planner_diagnostics),
        "answer": _usage_counts(answer_model),
    }
    token_usage = {
        key: sum(component[key] for component in components.values())
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    return {
        "duration_ms": duration_ms,
        "token_usage": token_usage,
        "components": components,
    }


def metadata_with_answer_metrics(
    metadata: dict[str, Any],
    run: sqlite3.Row | None,
) -> dict[str, Any]:
    if "answer_metrics" in metadata or run is None or not run["completed_at"]:
        return metadata
    query_plan = metadata.get("query_plan") if isinstance(metadata.get("query_plan"), dict) else {}
    planner_diagnostics = (
        query_plan.get("diagnostics") if isinstance(query_plan.get("diagnostics"), dict) else {}
    )
    enriched = dict(metadata)
    enriched["answer_metrics"] = answer_metrics(
        created_at=str(run["created_at"]),
        completed_at=str(run["completed_at"]),
        planner_diagnostics=planner_diagnostics,
        answer_model=_loads(run["model_json"], {}),
    )
    return enriched


def document_vocabulary(db: Database, workspace_id: str, document_ids: list[str]) -> list[str]:
    scope_sql = ""
    scope_args: list[Any] = []
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        scope_sql = f" AND d.id IN ({placeholders})"
        scope_args.extend(document_ids)
    with db.read() as conn:
        rows = conn.execute(
            f"""
            SELECT d.title document_title,s.title slide_title
            FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
            JOIN documents d ON d.id=dv.document_id
            WHERE d.workspace_id=? AND d.deleted_at IS NULL
              AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
            ORDER BY d.updated_at DESC,s.slide_no LIMIT 80
            """,
            (workspace_id, *scope_args),
        ).fetchall()
    terms: list[str] = []
    for row in rows:
        for value in (row["document_title"], row["slide_title"]):
            terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9&/_-]{1,40}|[\u4e00-\u9fff]{2,16}", str(value or "")))
    return list(dict.fromkeys(terms))[:120]
