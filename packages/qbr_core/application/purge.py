from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.foundation.errors import ResourceNotFound

logger = logging.getLogger(__name__)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class DocumentPurgeService:
    """Idempotently remove a document and all locally persisted derived data."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        rebuild_workspace_index: Callable[[str], None],
    ) -> None:
        """Initialize the document purge service and its dependencies."""
        self.settings = settings
        self.db = db
        self.rebuild_workspace_index = rebuild_workspace_index

    def is_requested(self, document_id: str) -> bool:
        """Return whether a purge has been requested for the document."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM document_purges WHERE document_id=? LIMIT 1",
                (document_id,),
            ).fetchone()
        return row is not None

    def discard_version_objects(self, workspace_id: str, version_id: str) -> None:
        """Best-effort cleanup used by workers that observe a concurrent purge."""
        try:
            self._remove_version_objects(workspace_id, version_id)
        except OSError:
            logger.warning("Worker could not discard objects for purged version %s", version_id, exc_info=True)

    def purge(self, document_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Remove the requested document and all dependent artifacts."""
        resumed = False
        with self.db.transaction(immediate=True) as conn:
            purge_row = conn.execute(
                "SELECT * FROM document_purges WHERE workspace_id=? AND document_id=?",
                (workspace_id, document_id),
            ).fetchone()
            if purge_row and purge_row["status"] == "completed":
                result = _loads(purge_row["result_json"], {})
                return {
                    **result,
                    "document_id": document_id,
                    "status": "completed",
                    "already_purged": True,
                }

            document = conn.execute(
                "SELECT id FROM documents WHERE id=? AND workspace_id=?",
                (document_id, workspace_id),
            ).fetchone()
            if not document and not purge_row:
                raise ResourceNotFound("Document not found")

            if document:
                version_ids = self._ids(
                    conn,
                    "SELECT id FROM document_versions WHERE document_id=? ORDER BY version_no",
                    (document_id,),
                )
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO document_purges(
                      workspace_id,document_id,requested_by,version_ids_json,status,
                      result_json,created_at,updated_at,completed_at
                    ) VALUES (?,?,?,?,?,'{}',?,?,NULL)
                    ON CONFLICT(workspace_id,document_id) DO UPDATE SET
                      requested_by=excluded.requested_by,
                      version_ids_json=excluded.version_ids_json,
                      status='purging',updated_at=excluded.updated_at,completed_at=NULL
                    """,
                    (workspace_id, document_id, user_id, self.db.json(version_ids), "purging", now, now),
                )
                deleted = self._purge_database_rows(conn, document_id, workspace_id, version_ids)
                result: dict[str, Any] = {
                    "document_id": document_id,
                    "deleted": deleted,
                    "files_removed": 0,
                    "vector_index": "pending",
                }
                conn.execute(
                    """UPDATE document_purges SET status='database_deleted',result_json=?,updated_at=?
                       WHERE workspace_id=? AND document_id=?""",
                    (self.db.json(result), utc_now(), workspace_id, document_id),
                )
            else:
                resumed = True

        # Serialize the non-transactional object/index stage with a database write
        # lock. Concurrent retries then observe the completed ledger row instead
        # of racing to remove the same directory and overwriting its final status.
        with self.db.transaction(immediate=True) as conn:
            purge_row = conn.execute(
                "SELECT * FROM document_purges WHERE workspace_id=? AND document_id=?",
                (workspace_id, document_id),
            ).fetchone()
            if purge_row["status"] == "completed":
                result = _loads(purge_row["result_json"], {})
                return {
                    **result,
                    "document_id": document_id,
                    "status": "completed",
                    "already_purged": True,
                }

            version_ids = _loads(purge_row["version_ids_json"], [])
            result = _loads(purge_row["result_json"], {})
            failures: list[dict[str, str]] = []
            files_removed = int(result.get("files_removed") or 0)
            for version_id in version_ids:
                try:
                    files_removed += self._remove_version_objects(workspace_id, str(version_id))
                except OSError as exc:
                    logger.warning("Object cleanup failed for purged document version %s", version_id, exc_info=True)
                    failures.append({"stage": "objects", "version_id": str(version_id), "error": type(exc).__name__})

            vector_index = "rebuilt"
            try:
                self.rebuild_workspace_index(workspace_id)
            except Exception as exc:  # a retry must be able to resume derived-cache cleanup
                logger.warning("Vector index rebuild failed during document purge", exc_info=True)
                vector_index = "failed"
                failures.append({"stage": "vector_index", "error": type(exc).__name__})

            status = "completed" if not failures else "partial"
            result.update(
                {
                    "document_id": document_id,
                    "status": status,
                    "files_removed": files_removed,
                    "vector_index": vector_index,
                    "failures": failures,
                }
            )
            now = utc_now()
            conn.execute(
                """UPDATE document_purges
                   SET status=?,result_json=?,version_ids_json=?,updated_at=?,completed_at=?
                   WHERE workspace_id=? AND document_id=?""",
                (
                    status,
                    self.db.json(result),
                    "[]" if status == "completed" else self.db.json(version_ids),
                    now,
                    now if status == "completed" else None,
                    workspace_id,
                    document_id,
                ),
            )
            conn.execute(
                """DELETE FROM audit_events
                   WHERE workspace_id=? AND target_type='document' AND target_id=? AND action='document.purge'""",
                (workspace_id, document_id),
            )
            self.db.audit(
                conn,
                workspace_id,
                user_id,
                "document.purge",
                "document",
                document_id,
                {
                    "status": status,
                    "deleted": result.get("deleted", {}),
                    "files_removed": files_removed,
                    "vector_index": vector_index,
                },
            )
        return {**result, "already_purged": False, "resumed": resumed}

    def _purge_database_rows(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        workspace_id: str,
        version_ids: list[str],
    ) -> dict[str, int]:
        """Delete document-dependent database rows in dependency order."""
        deleted: dict[str, int] = {}
        slide_ids = self._related_ids(conn, "slides", "document_version_id", version_ids)
        element_ids = self._related_ids(conn, "elements", "slide_id", slide_ids)
        chart_ids = self._related_ids(conn, "charts", "element_id", element_ids)
        series_ids = self._related_ids(conn, "chart_series", "chart_id", chart_ids)
        chunk_ids = self._related_ids(conn, "chunks", "document_version_id", version_ids)
        review_ids = self._related_ids(conn, "review_tasks", "element_id", element_ids)
        job_ids = self._related_ids(conn, "ingestion_jobs", "document_version_id", version_ids)
        conversation_ids = self._related_conversation_ids(conn, workspace_id, document_id, version_ids)
        message_ids = self._related_ids(conn, "messages", "conversation_id", conversation_ids)
        run_ids = self._related_ids(conn, "runs", "conversation_id", conversation_ids)

        self._delete_by_ids(conn, deleted, "feedback", "message_id", message_ids)
        self._delete_by_ids(conn, deleted, "citations", "message_id", message_ids)
        self._delete_by_ids(conn, deleted, "run_events", "run_id", run_ids)
        self._delete_by_ids(conn, deleted, "runs", "id", run_ids)
        self._delete_by_ids(conn, deleted, "messages", "id", message_ids)
        self._delete_by_ids(conn, deleted, "conversations", "id", conversation_ids)

        self._delete_by_ids(conn, deleted, "review_revisions", "review_task_id", review_ids)
        self._delete_by_ids(conn, deleted, "review_tasks", "id", review_ids)
        self._delete_by_ids(conn, deleted, "chart_points", "series_id", series_ids)
        self._delete_by_ids(conn, deleted, "chart_series", "id", series_ids)
        self._delete_by_ids(conn, deleted, "charts", "id", chart_ids)
        self._delete_by_ids(conn, deleted, "chunk_fts", "chunk_id", chunk_ids)
        self._delete_by_ids(conn, deleted, "chunk_embeddings", "chunk_id", chunk_ids)
        self._delete_by_ids(conn, deleted, "chunks", "id", chunk_ids)
        self._delete_by_ids(conn, deleted, "elements", "id", element_ids)
        self._delete_by_ids(conn, deleted, "slides", "id", slide_ids)
        self._delete_by_ids(conn, deleted, "parser_runs", "document_version_id", version_ids)
        self._delete_by_ids(conn, deleted, "job_events", "job_id", job_ids)
        self._delete_by_ids(conn, deleted, "ingestion_jobs", "id", job_ids)

        audit_target_ids = [document_id, *review_ids]
        self._delete_by_ids(conn, deleted, "audit_events", "target_id", audit_target_ids)
        self._delete_by_ids(conn, deleted, "document_versions", "id", version_ids)
        deleted["documents"] = max(
            conn.execute(
                "DELETE FROM documents WHERE id=? AND workspace_id=?",
                (document_id, workspace_id),
            ).rowcount,
            0,
        )
        return deleted

    def _related_conversation_ids(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        document_id: str,
        version_ids: list[str],
    ) -> list[str]:
        """Return conversations affected by removal of the document."""
        related: set[str] = set()
        rows = conn.execute(
            "SELECT id,scope_json FROM conversations WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
        for row in rows:
            scope = _loads(row["scope_json"], {})
            if isinstance(scope, dict) and document_id in scope.get("document_ids", []):
                related.add(str(row["id"]))
        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            cited = conn.execute(
                f"""SELECT DISTINCT m.conversation_id
                    FROM citations c JOIN messages m ON m.id=c.message_id
                    JOIN conversations cv ON cv.id=m.conversation_id
                    WHERE cv.workspace_id=? AND c.document_version_id IN ({placeholders})""",
                (workspace_id, *version_ids),
            ).fetchall()
            related.update(str(row["conversation_id"]) for row in cited)
        return sorted(related)

    def _remove_version_objects(self, workspace_id: str, version_id: str) -> int:
        """Remove version objects for this document purge service."""
        workspace_root = (self.settings.object_dir / workspace_id).resolve()
        version_root = (workspace_root / version_id).resolve()
        if version_root.parent != workspace_root:
            raise OSError("Unsafe document object path")
        if not version_root.exists():
            return 0
        file_count = sum(1 for item in version_root.rglob("*") if item.is_file())
        shutil.rmtree(version_root)
        return file_count

    @staticmethod
    def _ids(
        conn: sqlite3.Connection,
        query: str,
        params: Sequence[object] = (),
    ) -> list[str]:
        """Return normalized identifiers from database query results."""
        return [str(row["id"]) for row in conn.execute(query, params).fetchall()]

    @classmethod
    def _related_ids(
        cls,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        parent_ids: list[str],
    ) -> list[str]:
        """Return normalized identifiers from database query results."""
        if not parent_ids:
            return []
        placeholders = ",".join("?" for _ in parent_ids)
        return cls._ids(conn, f"SELECT id FROM {table} WHERE {column} IN ({placeholders})", parent_ids)

    @staticmethod
    def _delete_by_ids(
        conn: sqlite3.Connection,
        deleted: dict[str, int],
        table: str,
        column: str,
        ids: list[str],
    ) -> None:
        """Delete by ids for this document purge service."""
        if not ids:
            deleted[table] = 0
            return
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids)
        deleted[table] = max(cursor.rowcount, 0)
