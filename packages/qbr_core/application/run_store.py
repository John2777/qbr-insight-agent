"""Persistence boundaries for answer runs and their ordered event stream."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from packages.qbr_core.application.contracts import RunResult
from packages.qbr_core.application.runtime import answer_deltas, answer_metrics
from packages.qbr_core.application.warnings import describe_warning
from packages.qbr_core.conversations.context import ConversationContext, ConversationContextAssembler
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.foundation.errors import InvalidState, ResourceNotFound
from packages.qbr_core.foundation.identifiers import new_id
from packages.qbr_core.foundation.serialization import _loads

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedAnswerRun:
    """Contain the immutable inputs loaded for one answer execution."""

    row: sqlite3.Row
    question: str
    document_ids: tuple[str, ...]
    context: ConversationContext


class RunEventStore:
    """Append and read the ordered event stream associated with answer runs."""

    def __init__(self, db: Database) -> None:
        """Initialize the event store with its database dependency."""
        self._db = db

    def append(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Append one event within the caller's transaction."""
        conn.execute(
            "INSERT INTO run_events(run_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
            (run_id, event_type, self._db.json(data), utc_now()),
        )

    def list_after(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return public event records after the supplied event identifier."""
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id",
                (run_id, after),
            ).fetchall()
        return [
            {"id": row["id"], "event": row["event_type"], "data": _loads(row["data_json"], {})}
            for row in rows
        ]


class RunRepository:
    """Own answer-run SQL and enforce its persisted state transitions."""

    def __init__(
        self,
        db: Database,
        events: RunEventStore,
        context_assembler: ConversationContextAssembler,
    ) -> None:
        """Initialize the repository and its persistence collaborators."""
        self._db = db
        self._events = events
        self._context_assembler = context_assembler

    def create(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        workspace_id: str,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
        cutoff_sequence: int,
        created_at: str,
        client_message_id: str | None,
    ) -> None:
        """Create a pending run and its immutable context snapshot."""
        conn.execute(
            """INSERT INTO runs(
                 id,workspace_id,conversation_id,user_message_id,assistant_message_id,context_cutoff_sequence,
                 status,warning_json,model_json,created_at,completed_at,client_message_id
               ) VALUES (?,?,?,?,?,?,?,'[]','{}',?,NULL,?)""",
            (
                run_id,
                workspace_id,
                conversation_id,
                user_message_id,
                assistant_message_id,
                cutoff_sequence,
                "pending",
                created_at,
                client_message_id,
            ),
        )
        self._context_assembler.load_or_create(
            conn,
            run_id=run_id,
            conversation_id=conversation_id,
            current_user_message_id=user_message_id,
            cutoff_sequence=cutoff_sequence,
            created_at=created_at,
        )
        self._events.append(conn, run_id, "queued", {"run_id": run_id})

    def claim_next(self, *, worker_id: str, max_attempts: int, lease_expires_at: str) -> str | None:
        """Atomically claim the oldest eligible run for a worker."""
        now = utc_now()
        with self._db.transaction(immediate=True) as conn:
            run = conn.execute(
                """SELECT * FROM runs
                   WHERE attempts < ? AND (status='pending' OR (status='running' AND lease_expires_at < ?))
                   ORDER BY created_at,rowid LIMIT 1""",
                (max_attempts, now),
            ).fetchone()
            if not run:
                return None
            updated = conn.execute(
                """UPDATE runs SET status='running',started_at=coalesce(started_at,?),attempts=attempts+1,
                     lease_owner=?,lease_expires_at=?,error_detail=NULL
                   WHERE id=? AND (status='pending' OR lease_expires_at < ?)""",
                (now, worker_id, lease_expires_at, run["id"], now),
            ).rowcount
            if not updated:
                return None
            run_id = str(run["id"])
            self._events.append(conn, run_id, "run_started", {"run_id": run_id})
            return run_id

    def prepare(self, run_id: str) -> PreparedAnswerRun:
        """Load and normalize all persisted inputs required by the executor."""
        with self._db.transaction(immediate=True) as conn:
            run = conn.execute(
                """SELECT r.*,c.scope_json,c.user_id,m.sequence_no assistant_sequence
                   FROM runs r JOIN conversations c ON c.id=r.conversation_id
                   JOIN messages m ON m.id=r.assistant_message_id WHERE r.id=?""",
                (run_id,),
            ).fetchone()
            if not run:
                raise ResourceNotFound("Run not found")
            current_user_message_id, cutoff_sequence = self._resolve_question_reference(conn, run)
            question_row = conn.execute(
                """SELECT content,sequence_no FROM messages
                   WHERE id=? AND conversation_id=? AND role='user'""",
                (current_user_message_id, run["conversation_id"]),
            ).fetchone()
            if not question_row:
                raise InvalidState("Run has no user question")
            if cutoff_sequence < 1:
                cutoff_sequence = int(question_row["sequence_no"])
                conn.execute("UPDATE runs SET context_cutoff_sequence=? WHERE id=?", (cutoff_sequence, run_id))
            context = self._context_assembler.load_or_create(
                conn,
                run_id=run_id,
                conversation_id=str(run["conversation_id"]),
                current_user_message_id=current_user_message_id,
                cutoff_sequence=cutoff_sequence,
                created_at=str(run["created_at"]),
            )
        scope = _loads(run["scope_json"], {})
        return PreparedAnswerRun(
            row=run,
            question=str(question_row["content"]),
            document_ids=tuple(scope.get("document_ids", [])),
            context=context,
        )

    def fail(self, prepared: PreparedAnswerRun, exc: Exception, *, max_attempts: int) -> None:
        """Move a failed execution to pending retry or terminal failure."""
        run = prepared.row
        run_id = str(run["id"])
        with self._db.transaction(immediate=True) as conn:
            current = conn.execute("SELECT attempts FROM runs WHERE id=?", (run_id,)).fetchone()
            retrying = bool(current and int(current["attempts"]) < max_attempts)
            conn.execute(
                """UPDATE runs SET status=?,error_detail=?,lease_owner=NULL,lease_expires_at=NULL,
                     completed_at=? WHERE id=?""",
                ("pending" if retrying else "failed", str(exc)[:1000], None if retrying else utc_now(), run_id),
            )
            if not retrying:
                conn.execute(
                    "UPDATE messages SET status='failed',content=? WHERE id=?",
                    ("Answer generation failed. Try again later.", run["assistant_message_id"]),
                )
            self._events.append(
                conn,
                run_id,
                "warning" if retrying else "error",
                {"code": "RUN_RETRY" if retrying else "RUN_FAILED", "detail": type(exc).__name__},
            )

    def complete(
        self,
        prepared: PreparedAnswerRun,
        result: RunResult,
    ) -> None:
        """Atomically persist the completed run and its public event stream."""
        run = prepared.row
        run_id = str(run["id"])
        completed_at = utc_now()
        metadata = result.metadata.to_dict()
        model_info = result.model
        warnings = list(result.warnings)
        query_plan = metadata.get("query_plan") if isinstance(metadata.get("query_plan"), dict) else {}
        planner_diagnostics = query_plan.get("diagnostics") if isinstance(query_plan.get("diagnostics"), dict) else {}
        metadata["answer_metrics"] = answer_metrics(
            created_at=str(run["created_at"]),
            completed_at=completed_at,
            planner_diagnostics=planner_diagnostics,
            answer_model=model_info,
        )
        self._log_completion_signals(run_id, warnings, model_info, metadata)
        with self._db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET content=?,status='completed',metadata_json=? WHERE id=?",
                (result.answer, self._db.json(metadata), run["assistant_message_id"]),
            )
            for index, evidence_item in enumerate(result.evidence, 1):
                citation_id = new_id("cit")
                conn.execute(
                    "INSERT INTO citations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        citation_id,
                        run["assistant_message_id"],
                        index,
                        evidence_item.document_version_id,
                        evidence_item.slide_id,
                        evidence_item.element_id,
                        evidence_item.chunk_id,
                        evidence_item.quote,
                        self._db.json(evidence_item.bbox),
                        evidence_item.confidence,
                        evidence_item.source_kind,
                    ),
                )
                self._events.append(conn, run_id, "citation", {"id": citation_id, "label": f"[{index}]"})
            for piece in answer_deltas(result.answer):
                self._events.append(conn, run_id, "answer_delta", {"delta": piece})
            for warning in warnings:
                self._events.append(conn, run_id, "warning", {"message": warning})
            self._events.append(conn, run_id, "model", model_info)
            conn.execute(
                """UPDATE runs SET status='completed',warning_json=?,model_json=?,completed_at=?,
                     lease_owner=NULL,lease_expires_at=NULL WHERE id=?""",
                (self._db.json(warnings), self._db.json(model_info), completed_at, run_id),
            )
            self._events.append(conn, run_id, "completed", {"message_id": run["assistant_message_id"]})

    @staticmethod
    def _resolve_question_reference(conn: sqlite3.Connection, run: sqlite3.Row) -> tuple[str, int]:
        """Resolve modern and legacy run rows to their user-message reference."""
        if run["user_message_id"]:
            return str(run["user_message_id"]), int(run["context_cutoff_sequence"] or 0)
        legacy_user = conn.execute(
            """SELECT id,sequence_no FROM messages
               WHERE conversation_id=? AND role='user' AND sequence_no<?
               ORDER BY sequence_no DESC LIMIT 1""",
            (run["conversation_id"], run["assistant_sequence"]),
        ).fetchone()
        if not legacy_user:
            raise InvalidState("Run has no user question")
        user_message_id = str(legacy_user["id"])
        cutoff_sequence = int(legacy_user["sequence_no"])
        conn.execute(
            "UPDATE runs SET user_message_id=?,context_cutoff_sequence=? WHERE id=?",
            (user_message_id, cutoff_sequence, run["id"]),
        )
        return user_message_id, cutoff_sequence

    @staticmethod
    def _log_completion_signals(
        run_id: str,
        warnings: list[str],
        model_info: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """Log warning severity and answer provenance for operational diagnosis."""
        if not warnings:
            return
        severity_rank = {"info": 0, "warning": 1, "degraded": 2, "error": 3}
        max_severity = max((describe_warning(code).severity for code in warnings), key=severity_rank.__getitem__)
        log = logger.info if max_severity == "info" else logger.warning
        log(
            json.dumps(
                {
                    "event": "answer_run_completed_with_signals",
                    "run_id": run_id,
                    "warning_codes": list(dict.fromkeys(warnings)),
                    "max_severity": max_severity,
                    "model_status": model_info.get("status"),
                    "answer_source": model_info.get("answer_source"),
                    "verification_disposition": metadata.get("verification", {}).get("disposition"),
                    "planner": model_info.get("planner"),
                },
                separators=(",", ":"),
            )
        )
