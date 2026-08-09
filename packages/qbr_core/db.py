from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ids import new_id

SCHEMA_VERSION = 9


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, retention_days INTEGER NOT NULL DEFAULT 90,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), title TEXT NOT NULL,
  status TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', deleted_at TEXT,
  created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_workspace ON documents(workspace_id, deleted_at, updated_at);
CREATE TABLE IF NOT EXISTS document_versions (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), version_no INTEGER NOT NULL,
  sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, original_uri TEXT NOT NULL,
  active_parser_run_id TEXT, created_at TEXT NOT NULL, UNIQUE(document_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON document_versions(sha256);
CREATE TABLE IF NOT EXISTS parser_runs (
  id TEXT PRIMARY KEY, document_version_id TEXT NOT NULL REFERENCES document_versions(id),
  skill_name TEXT NOT NULL, skill_version TEXT NOT NULL, schema_version TEXT NOT NULL,
  status TEXT NOT NULL, quality_json TEXT NOT NULL DEFAULT '{}', started_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(id), status TEXT NOT NULL,
  current_stage TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 1),
  processed_slides INTEGER NOT NULL DEFAULT 0, total_slides INTEGER NOT NULL DEFAULT 0,
  warnings_count INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT, lease_expires_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  error_code TEXT, error_detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON ingestion_jobs(status, lease_expires_at, created_at);
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
  event_type TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slides (
  id TEXT PRIMARY KEY, parser_run_id TEXT NOT NULL REFERENCES parser_runs(id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(id), slide_no INTEGER NOT NULL,
  title TEXT, summary TEXT, notes_text TEXT, width_emu INTEGER NOT NULL, height_emu INTEGER NOT NULL,
  render_uri TEXT, quality_score REAL NOT NULL DEFAULT 1, UNIQUE(parser_run_id, slide_no)
);
CREATE TABLE IF NOT EXISTS elements (
  id TEXT PRIMARY KEY, slide_id TEXT NOT NULL REFERENCES slides(id), parent_id TEXT,
  element_type TEXT NOT NULL, reading_order INTEGER NOT NULL, bbox_json TEXT NOT NULL,
  text_content TEXT, structured_json TEXT NOT NULL DEFAULT '{}', provenance_json TEXT NOT NULL DEFAULT '{}',
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), review_status TEXT NOT NULL DEFAULT 'accepted'
);
CREATE INDEX IF NOT EXISTS idx_elements_slide ON elements(slide_id, reading_order);
CREATE TABLE IF NOT EXISTS charts (
  id TEXT PRIMARY KEY, element_id TEXT NOT NULL UNIQUE REFERENCES elements(id), title TEXT,
  chart_types_json TEXT NOT NULL, axes_json TEXT NOT NULL, source_kind TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS chart_series (
  id TEXT PRIMARY KEY, chart_id TEXT NOT NULL REFERENCES charts(id) ON DELETE CASCADE,
  series_order INTEGER NOT NULL, name TEXT, chart_type TEXT, axis_id TEXT, unit TEXT,
  visual_json TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL, UNIQUE(chart_id, series_order)
);
CREATE TABLE IF NOT EXISTS chart_points (
  id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES chart_series(id) ON DELETE CASCADE,
  point_order INTEGER NOT NULL, category TEXT, category_key TEXT, x_value REAL, y_value REAL,
  display_value TEXT, confidence REAL NOT NULL, source_ref TEXT, raw_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(series_id, point_order)
);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(id), slide_id TEXT REFERENCES slides(id),
  element_id TEXT REFERENCES elements(id), chunk_type TEXT NOT NULL, content TEXT NOT NULL,
  metadata_json TEXT NOT NULL, content_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(workspace_id, document_version_id, active);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  chunk_id UNINDEXED, workspace_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk_id TEXT NOT NULL UNIQUE REFERENCES chunks(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  model TEXT NOT NULL, embedding_identity TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
  content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_workspace
  ON chunk_embeddings(workspace_id, model, dimensions);
CREATE TABLE IF NOT EXISTS review_tasks (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), element_id TEXT REFERENCES elements(id),
  status TEXT NOT NULL, reason TEXT NOT NULL, original_json TEXT NOT NULL, corrected_json TEXT,
  assigned_to TEXT, reviewed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, scope_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), conversation_id TEXT NOT NULL,
  assistant_message_id TEXT NOT NULL, status TEXT NOT NULL, warning_json TEXT NOT NULL DEFAULT '[]',
  model_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, completed_at TEXT,
  started_at TEXT, lease_owner TEXT, lease_expires_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  error_detail TEXT, client_message_id TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
  event_type TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations (
  id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  claim_no INTEGER NOT NULL, document_version_id TEXT NOT NULL, slide_id TEXT NOT NULL,
  element_id TEXT, chunk_id TEXT, quote_text TEXT NOT NULL, bbox_json TEXT NOT NULL,
  confidence REAL NOT NULL, source_kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id), user_id TEXT NOT NULL REFERENCES users(id),
  rating INTEGER NOT NULL CHECK(rating IN (-1, 1)), category TEXT, comment TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_purges (
  workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, requested_by TEXT NOT NULL,
  version_ids_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  completed_at TEXT, PRIMARY KEY (workspace_id, document_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
  target_type TEXT NOT NULL, target_id TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_revisions (
  id TEXT PRIMARY KEY, review_task_id TEXT NOT NULL REFERENCES review_tasks(id),
  revision_no INTEGER NOT NULL, corrected_json TEXT NOT NULL, reviewer_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL, UNIQUE(review_task_id, revision_no)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction(immediate=True) as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
            now = utc_now()
            conn.execute(
                "INSERT OR IGNORE INTO users(id,email,display_name,created_at) VALUES ('user_demo',?,?,?)",
                ("demo@qbr.local", "Demo User", now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO workspaces(id,name,created_at) VALUES ('ws_demo',?,?)",
                ("Demo Workspace", now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role,created_at) VALUES (?,?,?,?)",
                ("ws_demo", "user_demo", "admin", now),
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Small, explicit migrations suitable for the single-node demo deployment."""
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "metadata_json" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        additions = {
            "model_json": "TEXT NOT NULL DEFAULT '{}'",
            "started_at": "TEXT",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "error_detail": "TEXT",
            "client_message_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in run_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs(status, lease_expires_at, created_at)")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_client_message
               ON runs(conversation_id,client_message_id) WHERE client_message_id IS NOT NULL"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS review_revisions (
                 id TEXT PRIMARY KEY, review_task_id TEXT NOT NULL REFERENCES review_tasks(id),
                 revision_no INTEGER NOT NULL, corrected_json TEXT NOT NULL,
                 reviewer_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                 UNIQUE(review_task_id, revision_no)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chunk_embeddings (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 chunk_id TEXT NOT NULL UNIQUE REFERENCES chunks(id) ON DELETE CASCADE,
                 workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                 model TEXT NOT NULL, embedding_identity TEXT NOT NULL DEFAULT '',
                 dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
                 content_hash TEXT NOT NULL, created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_workspace
               ON chunk_embeddings(workspace_id, model, dimensions)"""
        )
        embedding_columns = {row[1] for row in conn.execute("PRAGMA table_info(chunk_embeddings)").fetchall()}
        if "embedding_identity" not in embedding_columns:
            conn.execute("ALTER TABLE chunk_embeddings ADD COLUMN embedding_identity TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS document_purges (
                 workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, requested_by TEXT NOT NULL,
                 version_ids_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                 result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                 completed_at TEXT, PRIMARY KEY (workspace_id, document_id)
               )"""
        )
        Database._repair_table_chunks(conn)

    @staticmethod
    def _repair_table_chunks(conn: sqlite3.Connection) -> None:
        """Restore row/column boundaries in table chunks created by older parsers.

        ``elements.structured_json`` is the authoritative representation.  Earlier
        versions preferred the flattened shape text when building a chunk, which
        made deterministic table reasoning impossible even though the cells were
        still safely stored on the element.
        """
        rows = conn.execute(
            """SELECT ch.id,ch.workspace_id,ch.content,e.structured_json
               FROM chunks ch JOIN elements e ON e.id=ch.element_id
               WHERE ch.chunk_type='table'"""
        ).fetchall()
        for row in rows:
            try:
                structured = json.loads(row["structured_json"] or "{}")
            except json.JSONDecodeError:
                continue
            table_rows = structured.get("rows") if isinstance(structured, dict) else None
            if not isinstance(table_rows, list) or not table_rows:
                continue
            content = "\n".join(
                " | ".join("" if cell is None else str(cell).strip() for cell in table_row)
                for table_row in table_rows
                if isinstance(table_row, list)
            ).strip()
            if not content or content == row["content"]:
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            conn.execute("UPDATE chunks SET content=?,content_hash=? WHERE id=?", (content, digest, row["id"]))
            conn.execute("DELETE FROM chunk_fts WHERE chunk_id=?", (row["id"],))
            conn.execute(
                "INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)",
                (row["id"], row["workspace_id"], content),
            )

    @staticmethod
    def row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def audit(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id("audit"), workspace_id, actor_id, action, target_type, target_id,
                self.json(metadata or {}), utc_now(),
            ),
        )
