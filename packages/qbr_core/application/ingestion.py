from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from packages.qbr_core.documents.parser import parse_presentation, render_slides
from packages.qbr_core.documents.vision import SlideVisionEnricher, VisualKnowledge
from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.foundation.errors import Conflict, InvalidState, ResourceNotFound
from packages.qbr_core.foundation.identifiers import new_id
from packages.qbr_core.foundation.leases import LeaseCoordinator
from packages.qbr_core.foundation.serialization import _sha256_file
from packages.qbr_core.security.archive import OOXML_MIME, inspect_pptx
from packages.qbr_core.skills.registry import SkillDescriptor, SkillRegistry

logger = logging.getLogger(__name__)


class ParsedPersistence(Protocol):
    """Persist parsed presentations for the ingestion workflow."""

    def persist_parsed(
        self,
        job_id: str,
        job: sqlite3.Row,
        parser_run_id: str,
        parsed: Any,
        renders: list[Path],
        render_warnings: list[str],
        visual_knowledge: dict[int, VisualKnowledge],
        visual_warnings: list[dict[str, Any]],
    ) -> None:
        """Persist one parsed presentation and finish its job."""


class PurgeCoordinator(Protocol):
    """Expose the purge operations required during ingestion."""

    def is_requested(self, document_id: str) -> bool:
        """Return whether a permanent purge was requested."""

    def discard_version_objects(self, workspace_id: str, version_id: str) -> None:
        """Discard objects for a version being purged."""


class IngestionService:
    """Coordinate secure upload, parsing, enrichment, and ingestion job leases."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        process_lock: threading.Lock,
        leases: LeaseCoordinator,
        document_purges: PurgeCoordinator,
        parser_skill: SkillDescriptor,
        skill_registry: SkillRegistry,
        vision_enricher: SlideVisionEnricher | None,
        persistence: ParsedPersistence,
    ) -> None:
        """Initialize ingestion with explicit collaborators."""
        self.settings = settings
        self.db = db
        self._process_lock = process_lock
        self.leases = leases
        self.document_purges = document_purges
        self.parser_skill = parser_skill
        self.skill_registry = skill_registry
        self.vision_enricher = vision_enricher
        self.persistence = persistence

    def import_document(
        self,
        temp_path: Path,
        *,
        filename: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        deduplication: str,
        workspace_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Validate and queue a presentation for ingestion."""
        inspection = inspect_pptx(temp_path, max_slides=self.settings.max_slides)
        size = temp_path.stat().st_size
        digest = _sha256_file(temp_path)
        if deduplication not in {"reuse", "new_version", "reject"}:
            raise Conflict("deduplication must be reuse, new_version, or reject")
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT d.id document_id, d.title, d.status, dv.id version_id
                FROM documents d JOIN document_versions dv ON dv.document_id=d.id
                WHERE d.workspace_id=? AND d.deleted_at IS NULL AND dv.sha256=?
                ORDER BY dv.created_at DESC LIMIT 1
                """,
                (workspace_id, digest),
            ).fetchone()
            if existing and deduplication == "reject":
                raise Conflict("This presentation content already exists in the workspace.")
            if existing and deduplication == "reuse":
                job = conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE document_version_id=? ORDER BY created_at DESC LIMIT 1",
                    (existing["version_id"],),
                ).fetchone()
                return {
                    "document": {"id": existing["document_id"], "title": existing["title"], "status": existing["status"]},
                    "version": {"id": existing["version_id"]},
                    "job": dict(job) if job else None,
                    "reused": True,
                }

            now = utc_now()
            if existing and deduplication == "new_version":
                document_id = existing["document_id"]
                version_no = conn.execute(
                    "SELECT coalesce(max(version_no),0)+1 FROM document_versions WHERE document_id=?",
                    (document_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
                    (now, document_id),
                )
            else:
                document_id = new_id("doc")
                version_no = 1
                display_title = (title or Path(filename).stem or "Untitled presentation").strip()[:240]
                conn.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,NULL,?,?,?)",
                    (
                        document_id,
                        workspace_id,
                        display_title,
                        "processing",
                        self.db.json(metadata or {}),
                        user_id,
                        now,
                        now,
                    ),
                )
            version_id = new_id("dv")
            object_dir = self.settings.object_dir / workspace_id / version_id
            object_dir.mkdir(parents=True, exist_ok=True)
            final_path = object_dir / f"{digest}.pptx"
            os.replace(temp_path, final_path)
            conn.execute(
                "INSERT INTO document_versions VALUES (?,?,?,?,?,?,?,NULL,?)",
                (version_id, document_id, version_no, digest, OOXML_MIME, size, str(final_path), now),
            )
            job_id = new_id("job")
            conn.execute(
                """
                INSERT INTO ingestion_jobs(
                  id,workspace_id,document_version_id,status,current_stage,progress,total_slides,created_at,updated_at
                ) VALUES (?,?,?,'pending','queued',0,?,?,?)
                """,
                (job_id, workspace_id, version_id, inspection.slide_count, now, now),
            )
            self._job_event(conn, job_id, "progress", {"stage": "queued", "progress": 0.0})
            self.db.audit(conn, workspace_id, user_id, "document.upload", "document", document_id, {"version_id": version_id})
        return {
            "document": {"id": document_id, "title": title or Path(filename).stem, "status": "processing"},
            "version": {"id": version_id, "version_no": version_no},
            "job": {"id": job_id, "status": "pending"},
            "reused": False,
        }

    def _job_event(self, conn: sqlite3.Connection, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Persist one ordered ingestion job event."""
        conn.execute(
            "INSERT INTO job_events(job_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
            (job_id, event_type, self.db.json(data), utc_now()),
        )

    def process_next_job(self, worker_id: str = "worker-local") -> str | None:
        """Claim and execute the next eligible ingestion job."""
        if not self._process_lock.acquire(blocking=False):
            return None
        try:
            now = utc_now()
            expiry = self._lease_expiry()
            with self.db.transaction(immediate=True) as conn:
                job = conn.execute(
                    """
                    SELECT * FROM ingestion_jobs
                    WHERE status='pending' OR (status='running' AND lease_expires_at < ?)
                    ORDER BY created_at LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if not job:
                    return None
                updated = conn.execute(
                    """
                    UPDATE ingestion_jobs SET status='running',current_stage='validating',progress=0.05,
                      attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=?
                    WHERE id=? AND (status='pending' OR lease_expires_at < ?)
                    """,
                    (worker_id, expiry, now, job["id"], now),
                ).rowcount
                if not updated:
                    return None
                self._job_event(conn, job["id"], "progress", {"stage": "validating", "progress": 0.05})
            with self._lease_heartbeat("ingestion_jobs", str(job["id"]), worker_id):
                self.process_job(job["id"], worker_id=worker_id)
            return str(job["id"])
        finally:
            self._process_lock.release()

    def process_job(self, job_id: str, *, worker_id: str = "worker-local") -> None:
        """Execute one document ingestion job through completion."""
        del worker_id
        row = self._load_ingestion_job(job_id)
        self._validate_ingestion_job(row)
        if self._purge_requested(row):
            return
        parser_run_id, output_dir = self._start_parser_run(job_id, row)
        if self._cancel_and_finish(job_id, row, parser_run_id):
            return
        try:
            self._parse_render_and_persist(job_id, row, parser_run_id, output_dir)
        except Exception as exc:
            if self._purge_requested(row):
                return
            self._record_ingestion_failure(job_id, row, parser_run_id, exc)
            raise

    def _load_ingestion_job(self, job_id: str) -> sqlite3.Row | None:
        """Load an ingestion job with its document version and parser metadata."""
        with self.db.read() as conn:
            return conn.execute(
                """
                SELECT j.*,dv.original_uri,dv.id version_id,dv.document_id,d.workspace_id
                FROM ingestion_jobs j JOIN document_versions dv ON dv.id=j.document_version_id
                JOIN documents d ON d.id=dv.document_id WHERE j.id=?
                """,
                (job_id,),
            ).fetchone()

    @staticmethod
    def _validate_ingestion_job(row: sqlite3.Row | None) -> None:
        """Validate that a claimed job can safely enter parser execution."""
        if not row:
            raise ResourceNotFound("Job not found")
        if row["status"] not in {"running", "pending"}:
            raise InvalidState(f"Job is {row['status']}")

    def _purge_requested(self, row: sqlite3.Row) -> bool:
        """Return whether deletion was requested during ingestion."""
        if self.document_purges.is_requested(str(row["document_id"])):
            self.document_purges.discard_version_objects(str(row["workspace_id"]), str(row["version_id"]))
            return True
        return False

    def _start_parser_run(self, job_id: str, row: sqlite3.Row) -> tuple[str, Path]:
        """Create and return the active parser run identifier."""
        parser_run_id = new_id("pr")
        output_dir = self.settings.object_dir / row["workspace_id"] / row["version_id"] / parser_run_id
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO parser_runs VALUES (?,?,?,?,?,'running','{}',?,NULL)",
                (
                    parser_run_id,
                    row["version_id"],
                    self.parser_skill.name,
                    self.parser_skill.version,
                    self.parser_skill.schema_version,
                    now,
                ),
            )
            self._advance_job(conn, job_id, "native_parsing", 0.2)
        return parser_run_id, output_dir

    def _cancel_and_finish(self, job_id: str, row: sqlite3.Row, parser_run_id: str) -> bool:
        """Finish the job as cancelled when deletion has been requested."""
        if self._cancel_requested(job_id):
            self._finish_cancelled(job_id, row["document_id"], parser_run_id)
            return True
        return False

    def _parse_render_and_persist(self, job_id: str, row: sqlite3.Row, parser_run_id: str, output_dir: Path) -> None:
        """Parse, render, and persist one document version."""
        parsed = parse_presentation(
            Path(row["original_uri"]),
            output_dir,
            max_slides=self.settings.max_slides,
            skill_registry=self.skill_registry,
            parser_skill=self.parser_skill,
        )
        if self._cancel_and_finish(job_id, row, parser_run_id):
            return
        with self.db.transaction(immediate=True) as conn:
            self._advance_job(conn, job_id, "rendering", 0.55)
        renders, render_warnings = render_slides(Path(row["original_uri"]), output_dir, parsed.slides)
        if self._cancel_and_finish(job_id, row, parser_run_id) or self._purge_requested(row):
            return
        visual_knowledge, visual_warnings = self._enrich_visuals(job_id, parsed.slides, renders)
        self.persistence.persist_parsed(
            job_id,
            row,
            parser_run_id,
            parsed,
            renders,
            render_warnings,
            visual_knowledge,
            visual_warnings,
        )

    def _enrich_visuals(
        self, job_id: str, slides: list[Any], renders: list[Path]
    ) -> tuple[dict[int, VisualKnowledge], list[dict[str, Any]]]:
        """Enrich rendered slides with optional visual observations."""
        if self.vision_enricher is None:
            return {}, []
        with self.db.transaction(immediate=True) as conn:
            self._advance_job(conn, job_id, "visual_enrichment", 0.64)
        eligible = [
            (slide, render)
            for slide, render in zip(slides, renders, strict=True)
            if self.settings.vision_enrich_all_slides or any(element.element_type == "image" for element in slide.elements)
        ]
        warnings = self._visual_limit_warnings(len(eligible))
        knowledge: dict[int, VisualKnowledge] = {}
        for slide, render in eligible[: self.settings.vision_max_slides]:
            try:
                knowledge[slide.slide_no] = self.vision_enricher.enrich(render)
            except Exception as exc:
                logger.warning("Visual enrichment failed for slide %s", slide.slide_no, exc_info=True)
                warnings.append({"code": "VISUAL_ENRICHMENT_FAILED", "slide_no": slide.slide_no, "error_type": type(exc).__name__})
        return knowledge, warnings

    def _visual_limit_warnings(self, eligible_count: int) -> list[dict[str, Any]]:
        """Return warnings for slides skipped by the vision limit."""
        if eligible_count <= self.settings.vision_max_slides:
            return []
        return [
            {
                "code": "VISUAL_ENRICHMENT_LIMIT",
                "eligible_slides": eligible_count,
                "processed_slides": self.settings.vision_max_slides,
            }
        ]

    def _record_ingestion_failure(self, job_id: str, row: sqlite3.Row, parser_run_id: str, exc: Exception) -> None:
        """Record a retryable or terminal ingestion failure."""
        with self.db.transaction(immediate=True) as conn:
            attempts = int(conn.execute("SELECT attempts FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()[0])
            retrying = attempts < self.settings.job_max_attempts
            next_status = "pending" if retrying else "failed"
            next_stage = "retry_wait" if retrying else "failed"
            conn.execute(
                "UPDATE parser_runs SET status='failed',quality_json=?,completed_at=? WHERE id=?",
                (self.db.json({"error": str(exc)}), utc_now(), parser_run_id),
            )
            conn.execute(
                f"""
                UPDATE ingestion_jobs SET status='{next_status}',current_stage='{next_stage}',error_code='PARSER_OUTPUT_INVALID',
                  error_detail=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?
                """,
                (str(exc)[:1000], utc_now(), job_id),
            )
            conn.execute(
                "UPDATE documents SET status=?,updated_at=? WHERE id=?",
                ("processing" if retrying else "failed", utc_now(), row["document_id"]),
            )
            self._job_event(
                conn,
                job_id,
                "warning" if retrying else "error",
                {"code": "PARSER_RETRY" if retrying else "PARSER_OUTPUT_INVALID", "detail": str(exc)[:500], "attempt": attempts},
            )

    def _lease_expiry(self) -> str:
        """Return the expiry timestamp for a newly claimed ingestion job."""
        return self.leases.expiry()

    @contextmanager
    def _lease_heartbeat(self, table: str, item_id: str, owner: str) -> Iterator[None]:
        """Create a heartbeat context for an owned ingestion job."""
        with self.leases.heartbeat(table, item_id, owner):
            yield

    def _cancel_requested(self, job_id: str) -> bool:
        """Return whether cancellation has been requested for a job."""
        with self.db.read() as conn:
            row = conn.execute("SELECT cancel_requested FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def _finish_cancelled(self, job_id: str, document_id: str, parser_run_id: str) -> None:
        """Persist the terminal cancelled state and its event."""
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE parser_runs SET status='cancelled',completed_at=? WHERE id=?",
                (now, parser_run_id),
            )
            conn.execute(
                """UPDATE ingestion_jobs SET status='cancelled',current_stage='cancelled',
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                (now, job_id),
            )
            conn.execute(
                "UPDATE documents SET status='cancelled',updated_at=? WHERE id=?",
                (now, document_id),
            )
            self._job_event(conn, job_id, "completed", {"status": "cancelled"})

    def _advance_job(self, conn: sqlite3.Connection, job_id: str, stage: str, progress: float) -> None:
        """Advance ingestion progress and emit the corresponding event."""
        conn.execute(
            "UPDATE ingestion_jobs SET current_stage=?,progress=?,updated_at=? WHERE id=?",
            (stage, progress, utc_now(), job_id),
        )
        self._job_event(conn, job_id, "progress", {"stage": stage, "progress": progress})
