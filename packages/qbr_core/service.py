from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .errors import Conflict, InvalidState, ResourceNotFound
from .ids import new_id
from .lease import LeaseCoordinator, LeasePolicy
from .llm import EvidenceQAAgent
from .parser import ParsedElement, ParsedPresentation, ParsedSlide, parse_presentation, render_slides
from .purge import DocumentPurgeService
from .qa_service import QAApplicationService
from .retrieval import EvidenceRetriever
from .security import OOXML_MIME, inspect_pptx
from .skill_registry import (
    NATIVE_CHART_CAPABILITY,
    PARSER_SKILL_KIND,
    REASONING_SKILL_KIND,
    STRUCTURED_TABLE_REASONING_CAPABILITY,
    SkillRegistry,
    default_skill_paths,
)
from .vector import create_vector_store

logger = logging.getLogger(__name__)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class QBRService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_directories()
        self.skill_registry = SkillRegistry(settings.skill_paths or default_skill_paths())
        self.parser_skill = self.skill_registry.resolve(
            kind=PARSER_SKILL_KIND,
            name=settings.parser_skill_name or None,
            capability=NATIVE_CHART_CAPABILITY,
            accepts=OOXML_MIME,
        )
        self.table_reasoning_skill = self.skill_registry.resolve(
            kind=REASONING_SKILL_KIND,
            name=settings.table_reasoning_skill_name or None,
            capability=STRUCTURED_TABLE_REASONING_CAPABILITY,
            accepts="application/x-qbr-structured-table",
        )
        self.db = Database(settings.database_path)
        self.db.initialize()
        self._process_lock = threading.Lock()
        self.leases = LeaseCoordinator(
            self.db,
            LeasePolicy(settings.job_lease_seconds, settings.job_heartbeat_seconds),
        )
        vector_store = create_vector_store(self.db, settings)
        self.retriever = EvidenceRetriever(
            self.db,
            mode=settings.retrieval_strategy,
            vector_store=vector_store,
            lexical_candidate_k=settings.lexical_candidate_k,
            vector_candidate_k=settings.vector_candidate_k,
            rrf_k=settings.retrieval_rrf_k,
            lexical_weight=settings.retrieval_lexical_weight,
            vector_weight=settings.retrieval_vector_weight,
        )
        self.document_purges = DocumentPurgeService(settings, self.db, self.retriever.rebuild_workspace)
        self.qa_agent = EvidenceQAAgent(settings) if settings.llm_configured else None
        self.qa_service = QAApplicationService(
            settings=settings,
            db=self.db,
            retriever=self.retriever,
            qa_agent=self.qa_agent,
            skill_registry=self.skill_registry,
            table_reasoning_skill=self.table_reasoning_skill,
            leases=self.leases,
        )

    def health(self) -> dict[str, Any]:
        with self.db.read() as conn:
            schema = conn.execute("SELECT max(version) FROM schema_meta").fetchone()[0]
            fts = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
            pending_jobs = conn.execute("SELECT count(*) FROM ingestion_jobs WHERE status='pending'").fetchone()[0]
            pending_runs = conn.execute("SELECT count(*) FROM runs WHERE status='pending'").fetchone()[0]
        return {
            "status": "ready",
            "database": "ok",
            "schema_version": schema,
            "fts5": bool(fts),
            "parser_skill": self.parser_skill.reference,
            "skills": self.skill_registry.status(),
            "queues": {"ingestion_pending": pending_jobs, "answer_pending": pending_runs},
            "auth": {"mode": self.settings.auth_mode, "environment": self.settings.app_env},
            "llm": {
                "enabled": self.settings.llm_enabled,
                "configured": self.settings.llm_configured,
                "provider": self.settings.llm_provider if self.settings.llm_enabled else None,
                "model": self.settings.llm_model if self.settings.llm_enabled else None,
            },
            "retrieval": {
                "strategy": self.settings.retrieval_strategy,
                "vector_configured": self.settings.vector_configured,
                "vector_available": self.retriever.vector_available,
                "vector_backend": self.retriever.vector_backend,
                "embedding_model": self.settings.embedding_model if self.settings.vector_configured else None,
            },
        }

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
                        document_id, workspace_id, display_title, "processing", self.db.json(metadata or {}),
                        user_id, now, now,
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
        conn.execute(
            "INSERT INTO job_events(job_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
            (job_id, event_type, self.db.json(data), utc_now()),
        )

    def process_next_job(self, worker_id: str = "worker-local") -> str | None:
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
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT j.*,dv.original_uri,dv.id version_id,dv.document_id,d.workspace_id
                FROM ingestion_jobs j JOIN document_versions dv ON dv.id=j.document_version_id
                JOIN documents d ON d.id=dv.document_id WHERE j.id=?
                """,
                (job_id,),
            ).fetchone()
        if not row:
            raise ResourceNotFound("Job not found")
        if row["status"] not in {"running", "pending"}:
            raise InvalidState(f"Job is {row['status']}")
        if self.document_purges.is_requested(str(row["document_id"])):
            self.document_purges.discard_version_objects(str(row["workspace_id"]), str(row["version_id"]))
            return
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
        if self._cancel_requested(job_id):
            self._finish_cancelled(job_id, row["document_id"], parser_run_id)
            return
        try:
            parsed = parse_presentation(
                Path(row["original_uri"]),
                output_dir,
                max_slides=self.settings.max_slides,
                skill_registry=self.skill_registry,
                parser_skill=self.parser_skill,
            )
            if self._cancel_requested(job_id):
                self._finish_cancelled(job_id, row["document_id"], parser_run_id)
                return
            with self.db.transaction(immediate=True) as conn:
                self._advance_job(conn, job_id, "rendering", 0.55)
            renders, render_warnings = render_slides(Path(row["original_uri"]), output_dir, parsed.slides)
            if self._cancel_requested(job_id):
                self._finish_cancelled(job_id, row["document_id"], parser_run_id)
                return
            if self.document_purges.is_requested(str(row["document_id"])):
                self.document_purges.discard_version_objects(str(row["workspace_id"]), str(row["version_id"]))
                return
            self._persist_parsed(job_id, row, parser_run_id, parsed, renders, render_warnings)
        except Exception as exc:
            if self.document_purges.is_requested(str(row["document_id"])):
                self.document_purges.discard_version_objects(str(row["workspace_id"]), str(row["version_id"]))
                return
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
            raise

    def _lease_expiry(self) -> str:
        return self.leases.expiry()

    @contextmanager
    def _lease_heartbeat(self, table: str, item_id: str, owner: str) -> Iterator[None]:
        with self.leases.heartbeat(table, item_id, owner):
            yield

    def _cancel_requested(self, job_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute("SELECT cancel_requested FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def _finish_cancelled(self, job_id: str, document_id: str, parser_run_id: str) -> None:
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
        conn.execute(
            "UPDATE ingestion_jobs SET current_stage=?,progress=?,updated_at=? WHERE id=?",
            (stage, progress, utc_now(), job_id),
        )
        self._job_event(conn, job_id, "progress", {"stage": stage, "progress": progress})

    def _persist_parsed(
        self,
        job_id: str,
        job: sqlite3.Row,
        parser_run_id: str,
        parsed: ParsedPresentation,
        renders: list[Path],
        render_warnings: list[str],
    ) -> None:
        warnings = [*parsed.warnings, *({"code": "RENDER_FALLBACK", "message": w} for w in render_warnings)]
        status = "partial" if warnings or parsed.status == "partial" else "ready"
        charts_by_slide: dict[int, list[dict[str, Any]]] = {}
        for chart in parsed.charts:
            charts_by_slide.setdefault(int(chart["slide_number"]), []).append(chart)
        with self.db.transaction(immediate=True) as conn:
            self._advance_job(conn, job_id, "persisting", 0.72)
            for slide, render in zip(parsed.slides, renders, strict=True):
                slide_id = new_id("slide")
                summary_parts = [part for part in [slide.title, *(e.text for e in slide.elements if e.text)] if part]
                conn.execute(
                    "INSERT INTO slides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        slide_id, parser_run_id, job["version_id"], slide.slide_no, slide.title,
                        " · ".join(summary_parts)[:2000], slide.notes, slide.width_emu, slide.height_emu,
                        str(render), 1.0 if not render_warnings else 0.8,
                    ),
                )
                chart_elements = [element for element in slide.elements if element.element_type == "chart"]
                non_charts = [element for element in slide.elements if element.element_type != "chart"]
                for element in non_charts:
                    element_id = self._insert_element(conn, slide_id, element)
                    content = self._table_text(element) if element.element_type == "table" else element.text
                    self._insert_chunk(conn, job, slide_id, element_id, element.element_type, content or "")
                for chart_index, chart in enumerate(charts_by_slide.get(slide.slide_no, [])):
                    if chart_index < len(chart_elements):
                        source_element = chart_elements[chart_index]
                    else:
                        source_element = ParsedElement(
                            "chart", len(slide.elements) + chart_index + 1,
                            self._normalize_chart_bbox(chart.get("bbox_emu"), slide), None, {},
                            {"source": "native_ooxml", "part": chart.get("chart_part")},
                            float(chart.get("confidence", 0.4)),
                        )
                    element_id = self._insert_element(
                        conn,
                        slide_id,
                        ParsedElement(
                            source_element.element_type, source_element.reading_order, source_element.bbox,
                            chart.get("title"), chart, {
                                "source": chart.get("data_source", "chart_cache_or_literal"),
                                "chart_part": chart.get("chart_part"),
                            }, float(chart.get("confidence", 0.4)),
                        ),
                    )
                    self._insert_chart(conn, job, slide_id, element_id, chart)
                if slide.notes:
                    notes_element = ParsedElement(
                        "notes", len(slide.elements) + 1000, {"x": 0, "y": 0, "w": 1, "h": 1},
                        slide.notes, {}, {"source": "speaker_notes"}, 1.0,
                    )
                    notes_id = self._insert_element(conn, slide_id, notes_element)
                    self._insert_chunk(conn, job, slide_id, notes_id, "notes", slide.notes)
            quality = {
                "warnings": warnings,
                "source_priority": "embedded_workbook > chart_cache_or_literal > visual",
                "skill": {
                    "reference": self.parser_skill.reference,
                    "content_hash": self.parser_skill.content_hash,
                },
            }
            conn.execute(
                "UPDATE parser_runs SET status=?,quality_json=?,completed_at=? WHERE id=?",
                (status, self.db.json(quality), utc_now(), parser_run_id),
            )
            conn.execute(
                "UPDATE document_versions SET active_parser_run_id=? WHERE id=?",
                (parser_run_id, job["version_id"]),
            )
            conn.execute(
                "UPDATE documents SET status=?,updated_at=? WHERE id=?",
                (status, utc_now(), job["document_id"]),
            )
            conn.execute(
                """
                UPDATE ingestion_jobs SET status=?,current_stage='completed',progress=1,processed_slides=total_slides,
                  warnings_count=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?
                """,
                (status, len(warnings), utc_now(), job_id),
            )
            for warning in warnings:
                self._job_event(conn, job_id, "warning", warning if isinstance(warning, dict) else {"message": warning})
            self._job_event(conn, job_id, "completed", {"status": status, "warnings_count": len(warnings)})
        try:
            self.retriever.index_document_version(str(job["workspace_id"]), str(job["version_id"]))
        except Exception:
            # Vector indexes are derived caches. Ingestion remains successful and
            # FTS retrieval stays available if embedding/index refresh fails.
            logger.warning("Vector indexing failed after document ingestion", exc_info=True)

    def _normalize_chart_bbox(self, bbox: dict[str, Any] | None, slide: ParsedSlide) -> dict[str, float]:
        if not bbox:
            return {"x": 0.05, "y": 0.15, "w": 0.9, "h": 0.7}
        return {
            "x": int(bbox.get("x", 0)) / slide.width_emu,
            "y": int(bbox.get("y", 0)) / slide.height_emu,
            "w": int(bbox.get("w", slide.width_emu)) / slide.width_emu,
            "h": int(bbox.get("h", slide.height_emu)) / slide.height_emu,
        }

    def _insert_element(self, conn: sqlite3.Connection, slide_id: str, element: ParsedElement) -> str:
        element_id = new_id("el")
        review_status = "pending" if element.confidence < 0.65 else "accepted"
        conn.execute(
            "INSERT INTO elements VALUES (?,?,NULL,?,?,?,?,?,?,?,?)",
            (
                element_id, slide_id, element.element_type, element.reading_order, self.db.json(element.bbox),
                element.text, self.db.json(element.structured), self.db.json(element.provenance),
                element.confidence, review_status,
            ),
        )
        return element_id

    @staticmethod
    def _table_text(element: ParsedElement) -> str:
        return "\n".join(" | ".join(row) for row in element.structured.get("rows", []))

    @staticmethod
    def _series_axis_metadata(chart: dict[str, Any], series: dict[str, Any]) -> dict[str, Any]:
        """Resolve a series to its value axis instead of assuming the last id."""
        axes = [axis for axis in chart.get("axes", []) if isinstance(axis, dict)]
        axis_ids = {str(axis_id) for axis_id in series.get("axis_ids", []) if axis_id is not None}
        candidates = [axis for axis in axes if str(axis.get("axis_id")) in axis_ids]
        value_axes = [
            axis
            for axis in candidates
            if str(axis.get("axis_type", "")).casefold() in {"valax", "value", "valueaxis"}
        ]
        selected = value_axes[-1] if value_axes else candidates[-1] if candidates else None
        if selected is None:
            return {}
        all_value_axes = [
            axis
            for axis in axes
            if str(axis.get("axis_type", "")).casefold() in {"valax", "value", "valueaxis"}
        ]
        position = str(selected.get("position") or "").casefold()
        if position in {"r", "right", "t", "top"}:
            role = "secondary"
        elif position in {"l", "left", "b", "bottom"}:
            role = "primary"
        else:
            selected_id = str(selected.get("axis_id"))
            role = "secondary" if len(all_value_axes) > 1 and str(all_value_axes[-1].get("axis_id")) == selected_id else "primary"
        return {
            "axis_id": str(selected.get("axis_id")) if selected.get("axis_id") is not None else None,
            "role": role,
            "position": selected.get("position"),
            "title": selected.get("title"),
            "number_format": selected.get("number_format"),
            "display_units": selected.get("display_units"),
            "minimum": selected.get("minimum"),
            "maximum": selected.get("maximum"),
        }

    def _insert_chunk(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        slide_id: str,
        element_id: str,
        chunk_type: str,
        content: str,
    ) -> str | None:
        content = content.strip()
        if not content:
            return None
        chunk_id = new_id("chunk")
        digest = hashlib.sha256(content.encode()).hexdigest()
        metadata = {"element_type": chunk_type, "parser_run_id": None}
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,1)",
            (
                chunk_id, job["workspace_id"], job["version_id"], slide_id, element_id,
                chunk_type, content, self.db.json(metadata), digest,
            ),
        )
        conn.execute(
            "INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)",
            (chunk_id, job["workspace_id"], content),
        )
        return chunk_id

    def _insert_chart(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        slide_id: str,
        element_id: str,
        chart: dict[str, Any],
        *,
        create_review_task: bool = True,
    ) -> None:
        chart_id = new_id("chart")
        confidence = float(chart.get("confidence", 0.4))
        conn.execute(
            "INSERT INTO charts VALUES (?,?,?,?,?,?,?,?)",
            (
                chart_id, element_id, chart.get("title"), self.db.json(chart.get("chart_types", [])),
                self.db.json(chart.get("axes", [])), chart.get("data_source", "chart_cache_or_literal"),
                confidence, self.db.json(chart.get("warnings", [])),
            ),
        )
        chart_lines = [str(chart.get("title") or "Chart")]
        for series_index, series in enumerate(chart.get("series", [])):
            series_id = new_id("series")
            series_name = str(series.get("name") or f"Series {series_index + 1}")
            axis = self._series_axis_metadata(chart, series)
            axis_id = axis.get("axis_id")
            unit_value = series.get("number_format") or axis.get("number_format")
            unit = self.db.json(unit_value) if isinstance(unit_value, dict | list) else unit_value
            visual = dict(series.get("visual", {}))
            if axis:
                visual["axis"] = axis
            conn.execute(
                "INSERT INTO chart_series VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    series_id, chart_id, int(series.get("series_order", series_index)), series_name,
                    series.get("chart_type"), axis_id, unit, self.db.json(visual), confidence,
                ),
            )
            values: list[str] = []
            for point_index, point in enumerate(series.get("points", [])):
                category = point.get("category")
                y_value = _number(point.get("value"))
                if y_value is None:
                    y_value = _number(point.get("y"))
                x_value = _number(point.get("x"))
                display = point.get("display")
                conn.execute(
                    "INSERT INTO chart_points VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("point"), series_id, int(point.get("point_index", point_index)),
                        str(category) if category is not None else None,
                        str(category).casefold() if category is not None else None,
                        x_value, y_value, str(display) if display is not None else None,
                        confidence, point.get("source"), self.db.json(point),
                    ),
                )
                shown = display if display is not None else y_value
                values.append(f"{category if category is not None else point_index}={shown}")
            axis_label = axis.get("role") or "axis unspecified"
            if axis.get("title"):
                axis_label += f":{axis['title']}"
            line = f"{series_name} | {axis_label} | {unit or 'unit unspecified'} | " + "; ".join(values)
            chart_lines.append(line)
        self._insert_chunk(conn, job, slide_id, element_id, "chart_series", "\n".join(chart_lines))
        if create_review_task and (confidence < 0.85 or chart.get("warnings")):
            reason = "LOW_CHART_CONFIDENCE" if confidence < 0.85 else "CHART_WARNING"
            conn.execute(
                "INSERT INTO review_tasks VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?)",
                (
                    new_id("review"), job["workspace_id"], element_id, "pending", reason,
                    self.db.json(chart), utc_now(),
                ),
            )

    def list_documents(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT d.*,dv.id active_version_id,dv.version_no,j.id job_id,j.progress,j.current_stage,
                  (SELECT count(*) FROM slides s WHERE s.parser_run_id=dv.active_parser_run_id) slide_count,
                  (SELECT count(*) FROM review_tasks rt JOIN elements e ON e.id=rt.element_id
                   JOIN slides s ON s.id=e.slide_id WHERE s.parser_run_id=dv.active_parser_run_id AND rt.status='pending') review_count
                FROM documents d
                LEFT JOIN document_versions dv ON dv.id=(
                  SELECT id FROM document_versions WHERE document_id=d.id ORDER BY version_no DESC LIMIT 1
                )
                LEFT JOIN ingestion_jobs j ON j.id=(
                  SELECT id FROM ingestion_jobs WHERE document_version_id=dv.id ORDER BY created_at DESC LIMIT 1
                )
                WHERE d.workspace_id=? AND d.deleted_at IS NULL ORDER BY d.updated_at DESC
                """,
                (workspace_id,),
            ).fetchall()
        result = self.db.rows(rows)
        for item in result:
            item["metadata"] = _loads(item.pop("metadata_json", "{}"), {})
        return result

    def get_document(self, document_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=? AND workspace_id=? AND deleted_at IS NULL",
                (document_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Document not found")
            versions = conn.execute(
                """SELECT id,version_no,sha256,mime_type,size_bytes,created_at,active_parser_run_id
                   FROM document_versions WHERE document_id=? ORDER BY version_no DESC""",
                (document_id,),
            ).fetchall()
        item = dict(row)
        item["metadata"] = _loads(item.pop("metadata_json"), {})
        item["versions"] = self.db.rows(versions)
        return item

    def list_slides(self, version_id: str, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT s.id,s.slide_no,s.title,s.summary,s.quality_score,s.render_uri,
                  (SELECT count(*) FROM elements e WHERE e.slide_id=s.id) element_count
                FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE s.document_version_id=? AND s.parser_run_id=dv.active_parser_run_id
                  AND d.workspace_id=? AND d.deleted_at IS NULL ORDER BY s.slide_no
                """,
                (version_id, workspace_id),
            ).fetchall()
        return [self._public_slide(dict(row)) for row in rows]

    def get_slide(self, slide_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT s.*,d.id document_id,d.title document_title FROM slides s
                JOIN document_versions dv ON dv.id=s.document_version_id JOIN documents d ON d.id=dv.document_id
                WHERE s.id=? AND d.workspace_id=? AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
                """,
                (slide_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Slide not found")
            elements = conn.execute(
                "SELECT * FROM elements WHERE slide_id=? ORDER BY reading_order",
                (slide_id,),
            ).fetchall()
            charts = conn.execute(
                """
                SELECT c.*,e.id element_id FROM charts c JOIN elements e ON e.id=c.element_id
                WHERE e.slide_id=?
                """,
                (slide_id,),
            ).fetchall()
            chart_data: list[dict[str, Any]] = []
            for chart in charts:
                chart_item = dict(chart)
                series_rows = conn.execute("SELECT * FROM chart_series WHERE chart_id=? ORDER BY series_order", (chart["id"],)).fetchall()
                series_data = []
                for series in series_rows:
                    series_item = dict(series)
                    points = conn.execute("SELECT * FROM chart_points WHERE series_id=? ORDER BY point_order", (series["id"],)).fetchall()
                    series_item["points"] = [self._decode_point(dict(point)) for point in points]
                    series_item["visual"] = _loads(series_item.pop("visual_json"), {})
                    series_data.append(series_item)
                chart_item["series"] = series_data
                chart_item["chart_types"] = _loads(chart_item.pop("chart_types_json"), [])
                chart_item["axes"] = _loads(chart_item.pop("axes_json"), [])
                chart_item["warnings"] = _loads(chart_item.pop("warnings_json"), [])
                chart_data.append(chart_item)
        item = self._public_slide(dict(row))
        item["elements"] = [self._decode_element(dict(element)) for element in elements]
        item["charts"] = chart_data
        return item

    def _public_slide(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("render_uri"):
            item["preview_url"] = f"/api/v1/slides/{item['id']}/preview"
        item.pop("render_uri", None)
        return item

    def _decode_element(self, item: dict[str, Any]) -> dict[str, Any]:
        item["bbox"] = _loads(item.pop("bbox_json"), {})
        item["structured"] = _loads(item.pop("structured_json"), {})
        item["provenance"] = _loads(item.pop("provenance_json"), {})
        return item

    def _decode_point(self, item: dict[str, Any]) -> dict[str, Any]:
        item["raw"] = _loads(item.pop("raw_json"), {})
        return item

    def preview_path(self, slide_id: str, workspace_id: str) -> Path:
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT s.render_uri FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE s.id=? AND d.workspace_id=? AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
                """,
                (slide_id, workspace_id),
            ).fetchone()
        if not row or not Path(row["render_uri"]).is_file():
            raise ResourceNotFound("Slide preview not found")
        return Path(row["render_uri"])

    def get_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM ingestion_jobs WHERE id=? AND workspace_id=?", (job_id, workspace_id)).fetchone()
        if not row:
            raise ResourceNotFound("Job not found")
        return dict(row)

    def job_events(self, job_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.get_job(job_id, workspace_id)
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id", (job_id, after)).fetchall()
        return [{"id": row["id"], "event": row["event_type"], "data": _loads(row["data_json"], {})} for row in rows]

    def cancel_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT j.status,d.id document_id FROM ingestion_jobs j
                   JOIN document_versions dv ON dv.id=j.document_version_id
                   JOIN documents d ON d.id=dv.document_id
                   WHERE j.id=? AND j.workspace_id=?""",
                (job_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Job not found")
            if row["status"] not in {"pending", "running"}:
                raise InvalidState("Only pending or running jobs can be cancelled")
            now = utc_now()
            if row["status"] == "pending":
                conn.execute(
                    """UPDATE ingestion_jobs SET status='cancelled',current_stage='cancelled',
                       cancel_requested=1,updated_at=? WHERE id=?""",
                    (now, job_id),
                )
                conn.execute(
                    "UPDATE documents SET status='cancelled',updated_at=? WHERE id=?",
                    (now, row["document_id"]),
                )
                self._job_event(conn, job_id, "completed", {"status": "cancelled"})
            else:
                conn.execute("UPDATE ingestion_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now, job_id))
        return self.get_job(job_id, workspace_id)

    def retry_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT j.status,d.id document_id FROM ingestion_jobs j
                   JOIN document_versions dv ON dv.id=j.document_version_id
                   JOIN documents d ON d.id=dv.document_id
                   WHERE j.id=? AND j.workspace_id=?""",
                (job_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Job not found")
            if row["status"] not in {"failed", "partial", "cancelled"}:
                raise InvalidState("Only failed, partial, or cancelled jobs can be retried")
            now = utc_now()
            conn.execute(
                """UPDATE ingestion_jobs SET status='pending',current_stage='queued',progress=0,
                   processed_slides=0,warnings_count=0,cancel_requested=0,error_code=NULL,error_detail=NULL,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                (now, job_id),
            )
            conn.execute(
                "UPDATE documents SET status='processing',updated_at=? WHERE id=?",
                (now, row["document_id"]),
            )
            self._job_event(conn, job_id, "progress", {"stage": "queued", "progress": 0.0})
        return self.get_job(job_id, workspace_id)

    def delete_document(self, document_id: str, workspace_id: str, user_id: str) -> None:
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT id FROM documents WHERE id=? AND workspace_id=? AND deleted_at IS NULL",
                (document_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Document not found")
            conn.execute("UPDATE documents SET deleted_at=?,status='deleted',updated_at=? WHERE id=?", (now, now, document_id))
            conn.execute(
                "UPDATE chunks SET active=0 WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id=?)",
                (document_id,),
            )
            conn.execute(
                """DELETE FROM chunk_fts WHERE chunk_id IN (
                     SELECT id FROM chunks WHERE document_version_id IN (
                       SELECT id FROM document_versions WHERE document_id=?
                     )
                   )""",
                (document_id,),
            )
            self.db.audit(conn, workspace_id, user_id, "document.delete", "document", document_id)
        try:
            self.retriever.rebuild_workspace(workspace_id)
        except Exception:
            logger.warning("Vector index rebuild failed after document deletion", exc_info=True)

    def purge_document(self, document_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        return self.document_purges.purge(document_id, workspace_id, user_id)

    def list_reviews(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT rt.id,rt.status,rt.reason,rt.created_at,rt.assigned_to,rt.reviewed_at,
                  rt.original_json,rt.corrected_json,e.id element_id,s.slide_no,d.id document_id,d.title document_title
                FROM review_tasks rt JOIN elements e ON e.id=rt.element_id JOIN slides s ON s.id=e.slide_id
                JOIN document_versions dv ON dv.id=s.document_version_id JOIN documents d ON d.id=dv.document_id
                WHERE rt.workspace_id=? AND d.deleted_at IS NULL ORDER BY rt.created_at DESC
                """,
                (workspace_id,),
            ).fetchall()
        items = self.db.rows(rows)
        for item in items:
            item["original"] = _loads(item.pop("original_json"), {})
            item["corrected"] = _loads(item.pop("corrected_json"), None)
        return items

    def claim_review(self, review_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM review_tasks WHERE id=? AND workspace_id=?",
                (review_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Review task not found")
            if row["status"] not in {"pending", "in_review"}:
                raise InvalidState("Review task is already closed")
            if row["assigned_to"] not in {None, user_id}:
                raise Conflict("Review task is assigned to another reviewer")
            conn.execute(
                "UPDATE review_tasks SET status='in_review',assigned_to=? WHERE id=?",
                (user_id, review_id),
            )
            self.db.audit(conn, workspace_id, user_id, "review.claim", "review_task", review_id)
        return next(item for item in self.list_reviews(workspace_id) if item["id"] == review_id)

    def resolve_review(
        self,
        review_id: str,
        workspace_id: str,
        user_id: str,
        corrected: dict[str, Any] | None,
        resolution: str = "resolved",
    ) -> dict[str, Any]:
        if resolution not in {"resolved", "dismissed"}:
            raise Conflict("resolution must be resolved or dismissed")
        if resolution == "resolved" and (not corrected or not isinstance(corrected.get("series"), list)):
            raise Conflict("A corrected chart with a series array is required")
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT rt.*,e.id element_id,s.id slide_id,dv.id version_id,d.id document_id
                   FROM review_tasks rt JOIN elements e ON e.id=rt.element_id
                   JOIN slides s ON s.id=e.slide_id JOIN document_versions dv ON dv.id=s.document_version_id
                   JOIN documents d ON d.id=dv.document_id
                   WHERE rt.id=? AND rt.workspace_id=? AND d.deleted_at IS NULL""",
                (review_id, workspace_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Review task not found")
            if row["status"] not in {"pending", "in_review"}:
                raise InvalidState("Review task is already closed")
            if row["assigned_to"] not in {None, user_id}:
                raise Conflict("Review task is assigned to another reviewer")
            revision_no = conn.execute(
                "SELECT coalesce(max(revision_no),0)+1 FROM review_revisions WHERE review_task_id=?",
                (review_id,),
            ).fetchone()[0]
            corrected_payload = dict(corrected or {})
            if resolution == "resolved":
                corrected_payload["confidence"] = 1.0
                corrected_payload["warnings"] = []
                conn.execute(
                    """INSERT INTO review_revisions(id,review_task_id,revision_no,corrected_json,reviewer_id,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (new_id("rev"), review_id, revision_no, self.db.json(corrected_payload), user_id, utc_now()),
                )
                chunk_ids = conn.execute("SELECT id FROM chunks WHERE element_id=?", (row["element_id"],)).fetchall()
                conn.executemany("DELETE FROM chunk_fts WHERE chunk_id=?", [(item["id"],) for item in chunk_ids])
                conn.execute("DELETE FROM chunks WHERE element_id=?", (row["element_id"],))
                conn.execute("DELETE FROM charts WHERE element_id=?", (row["element_id"],))
                conn.execute(
                    """UPDATE elements SET structured_json=?,text_content=?,confidence=1,review_status='accepted'
                       WHERE id=?""",
                    (self.db.json(corrected_payload), corrected_payload.get("title"), row["element_id"]),
                )
                self._insert_chart(
                    conn,
                    {"workspace_id": workspace_id, "version_id": row["version_id"]},
                    row["slide_id"],
                    row["element_id"],
                    corrected_payload,
                    create_review_task=False,
                )
            else:
                conn.execute("UPDATE elements SET review_status='dismissed' WHERE id=?", (row["element_id"],))
            now = utc_now()
            conn.execute(
                """UPDATE review_tasks SET status=?,assigned_to=?,corrected_json=?,reviewed_at=? WHERE id=?""",
                (resolution, user_id, self.db.json(corrected_payload) if corrected else None, now, review_id),
            )
            self.db.audit(
                conn,
                workspace_id,
                user_id,
                f"review.{resolution}",
                "review_task",
                review_id,
                {"revision_no": revision_no if corrected else None},
            )
        if resolution == "resolved":
            try:
                self.retriever.index_document_version(workspace_id, str(row["version_id"]))
            except Exception:
                logger.warning("Vector index refresh failed after chart review", exc_info=True)
        return next(item for item in self.list_reviews(workspace_id) if item["id"] == review_id)

    def analytics_summary(self, workspace_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            document_rows = conn.execute(
                """SELECT status,count(*) count FROM documents
                   WHERE workspace_id=? AND deleted_at IS NULL GROUP BY status""",
                (workspace_id,),
            ).fetchall()
            run_row = conn.execute(
                """SELECT count(*) total,
                     sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                     sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
                     sum(CASE WHEN warning_json LIKE '%INSUFFICIENT_EVIDENCE%' THEN 1 ELSE 0 END) no_evidence,
                     avg(CASE WHEN completed_at IS NOT NULL
                       THEN (julianday(completed_at)-julianday(created_at))*86400000 END) avg_latency_ms
                   FROM runs WHERE workspace_id=?""",
                (workspace_id,),
            ).fetchone()
            review_rows = conn.execute(
                "SELECT status,count(*) count FROM review_tasks WHERE workspace_id=? GROUP BY status",
                (workspace_id,),
            ).fetchall()
            feedback_row = conn.execute(
                """SELECT count(*) total,sum(CASE WHEN f.rating=1 THEN 1 ELSE 0 END) positive
                   FROM feedback f JOIN messages m ON m.id=f.message_id
                   JOIN conversations c ON c.id=m.conversation_id WHERE c.workspace_id=?""",
                (workspace_id,),
            ).fetchone()
            recent = conn.execute(
                """SELECT r.id,r.status,r.created_at,r.completed_at,r.warning_json,c.title
                   FROM runs r JOIN conversations c ON c.id=r.conversation_id
                   WHERE r.workspace_id=? ORDER BY r.created_at DESC LIMIT 10""",
                (workspace_id,),
            ).fetchall()
        feedback_total = int(feedback_row["total"] or 0)
        return {
            "documents": {row["status"]: row["count"] for row in document_rows},
            "runs": {
                "total": int(run_row["total"] or 0),
                "completed": int(run_row["completed"] or 0),
                "failed": int(run_row["failed"] or 0),
                "no_evidence": int(run_row["no_evidence"] or 0),
                "avg_latency_ms": round(float(run_row["avg_latency_ms"] or 0), 1),
            },
            "reviews": {row["status"]: row["count"] for row in review_rows},
            "feedback": {
                "total": feedback_total,
                "positive_rate": round(int(feedback_row["positive"] or 0) / feedback_total, 3) if feedback_total else None,
            },
            "recent_runs": [
                {**{key: value for key, value in dict(row).items() if key != "warning_json"}, "warnings": _loads(row["warning_json"], [])}
                for row in recent
            ],
        }

    # Keep the original facade stable for API, worker, and scripts while the
    # conversation/answer use cases live behind their own application boundary.
    def create_conversation(
        self,
        workspace_id: str,
        user_id: str,
        document_ids: list[str] | None = None,
        title: str = "New QBR conversation",
    ) -> dict[str, Any]:
        return self.qa_service.create_conversation(workspace_id, user_id, document_ids, title)

    def get_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        return self.qa_service.get_conversation(conversation_id, workspace_id, user_id)

    def list_conversations(
        self,
        workspace_id: str,
        user_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        return self.qa_service.list_conversations(workspace_id, user_id, limit)

    def delete_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> None:
        self.qa_service.delete_conversation(conversation_id, workspace_id, user_id)

    def ask(
        self,
        conversation_id: str,
        content: str,
        workspace_id: str,
        user_id: str,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        return self.qa_service.ask(conversation_id, content, workspace_id, user_id, client_message_id)

    def process_next_run(self, worker_id: str = "worker-local") -> str | None:
        return self.qa_service.process_next_run(worker_id)

    def process_run(self, run_id: str) -> None:
        self.qa_service.process_run(run_id)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        return self.qa_service.get_run(run_id, workspace_id)

    def run_events(self, run_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.qa_service.run_events(run_id, workspace_id, after)

    def add_feedback(
        self,
        message_id: str,
        workspace_id: str,
        user_id: str,
        rating: int,
        category: str | None,
        comment: str | None,
    ) -> dict[str, Any]:
        return self.qa_service.add_feedback(message_id, workspace_id, user_id, rating, category, comment)

    # Compatibility shims for the deterministic-policy unit tests. New callers
    # should exercise these policies through QAApplicationService.
    def _answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.qa_service._answer(question, workspace_id, document_ids)

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        return self.qa_service._table_reasoning_answer(question, sources)

    def _constraint_abstention(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        return self.qa_service._constraint_abstention(question, chunks)

    def _chart_comparison_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        return self.qa_service._chart_comparison_answer(question, chart_rows)

    def _chart_answer(
        self,
        question: str,
        ranked: list[tuple[int, dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.qa_service._chart_answer(question, ranked)
