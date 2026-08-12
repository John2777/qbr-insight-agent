"""Read models for conversations, answer runs, events, and citations."""

from __future__ import annotations

import sqlite3
from typing import Any

from packages.qbr_core.application.contracts import Citation
from packages.qbr_core.application.run_store import RunEventStore
from packages.qbr_core.application.runtime import metadata_with_answer_metrics
from packages.qbr_core.foundation.database import Database
from packages.qbr_core.foundation.errors import ResourceNotFound
from packages.qbr_core.foundation.serialization import _loads


class PublicResultReader:
    """Build public API representations from persisted QBR records."""

    def __init__(self, db: Database, events: RunEventStore | None = None) -> None:
        """Initialize the reader with its database dependency."""
        self._db = db
        self._events = events or RunEventStore(db)

    def get_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Return a user-scoped conversation with messages and citations."""
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=? AND workspace_id=? AND user_id=?",
                (conversation_id, workspace_id, user_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Conversation not found")
            messages = conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence_no",
                (conversation_id,),
            ).fetchall()
            message_data = []
            for message in messages:
                item = dict(message)
                metadata = _loads(item.pop("metadata_json", None), {})
                message_run = (
                    conn.execute(
                        "SELECT created_at,completed_at,model_json FROM runs WHERE id=?",
                        (message["run_id"],),
                    ).fetchone()
                    if message["run_id"]
                    else None
                )
                item["metadata"] = metadata_with_answer_metrics(metadata, message_run)
                citations = conn.execute(
                    "SELECT * FROM citations WHERE message_id=? ORDER BY claim_no",
                    (message["id"],),
                ).fetchall()
                item["citations"] = [self.citation(dict(citation), conn) for citation in citations]
                message_data.append(item)
        result = dict(row)
        result["scope"] = _loads(result.pop("scope_json"), {})
        result["messages"] = message_data
        return result

    def list_conversations(self, workspace_id: str, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """Return the user's conversations ordered by most recent activity."""
        limit = max(1, min(int(limit), 100))
        with self._db.read() as conn:
            rows = conn.execute(
                """SELECT c.id,c.title,c.scope_json,c.created_at,c.updated_at,
                     count(m.id) message_count,max(m.created_at) last_message_at,
                     (SELECT latest.content FROM messages latest
                       WHERE latest.conversation_id=c.id AND latest.role='user'
                       ORDER BY latest.sequence_no DESC LIMIT 1) last_question
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
                   WHERE c.workspace_id=? AND c.user_id=?
                   GROUP BY c.id
                   ORDER BY coalesce(max(m.created_at),c.updated_at) DESC,c.id DESC
                   LIMIT ?""",
                (workspace_id, user_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["scope"] = _loads(item.pop("scope_json"), {})
            item["last_activity_at"] = item["last_message_at"] or item["updated_at"]
            items.append(item)
        return items

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        """Return one workspace-scoped answer run and its public artifacts."""
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=? AND workspace_id=?", (run_id, workspace_id)).fetchone()
            if not row:
                raise ResourceNotFound("Run not found")
            message = conn.execute("SELECT * FROM messages WHERE id=?", (row["assistant_message_id"],)).fetchone()
            citations = conn.execute(
                "SELECT * FROM citations WHERE message_id=? ORDER BY claim_no",
                (row["assistant_message_id"],),
            ).fetchall()
            result = dict(row)
            result["warnings"] = _loads(result.pop("warning_json"), [])
            result["model"] = _loads(result.pop("model_json"), {})
            if message:
                public_message = dict(message)
                metadata = _loads(public_message.pop("metadata_json", None), {})
                public_message["metadata"] = metadata_with_answer_metrics(metadata, row)
                result["message"] = public_message
            else:
                result["message"] = None
            result["citations"] = [self.citation(dict(citation), conn) for citation in citations]
            return result

    def run_events(self, run_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return ordered run events after the supplied event identifier."""
        self.get_run(run_id, workspace_id)
        return self._events.list_after(run_id, after)

    def citation(self, item: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
        """Enrich a persisted citation with slide and preview metadata."""
        slide = conn.execute(
            """
            SELECT s.slide_no,d.id document_id,d.title document_title,e.element_type
            FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
            JOIN documents d ON d.id=dv.document_id LEFT JOIN elements e ON e.id=?
            WHERE s.id=?
            """,
            (item.get("element_id"), item["slide_id"]),
        ).fetchone()
        item["bbox"] = _loads(item.pop("bbox_json"), {})
        item["quote"] = item.pop("quote_text")
        item["label"] = f"[{item['claim_no']}]"
        if slide:
            item.update(dict(slide))
        item["preview_url"] = f"/api/v1/slides/{item['slide_id']}/preview"
        return Citation.from_mapping(item).to_dict()
