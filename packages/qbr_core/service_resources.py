from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .db import utc_now
from .errors import Conflict, InvalidState, ResourceNotFound
from .ids import new_id
from .parser import thumbnail_path_for
from .run_warnings import warning_codes_by_severity, warning_details
from .service_component import ServiceComponent
from .service_support import _loads

logger = logging.getLogger(__name__)


class ResourceService(ServiceComponent):
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
            item["thumbnail_url"] = f"/api/v1/slides/{item['id']}/thumbnail"
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

    def thumbnail_path(self, slide_id: str, workspace_id: str) -> Path:
        preview_path = self.preview_path(slide_id, workspace_id)
        thumbnail_path = thumbnail_path_for(preview_path)
        return thumbnail_path if thumbnail_path.is_file() else preview_path

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
        degraded_patterns = [f"%{code}%" for code in warning_codes_by_severity("degraded")]
        degraded_predicate = " OR ".join("warning_json LIKE ?" for _ in degraded_patterns) or "0"
        with self.db.read() as conn:
            document_rows = conn.execute(
                """SELECT status,count(*) count FROM documents
                   WHERE workspace_id=? AND deleted_at IS NULL GROUP BY status""",
                (workspace_id,),
            ).fetchall()
            run_row = conn.execute(
                f"""SELECT count(*) total,
                     sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                     sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
                     sum(CASE WHEN warning_json LIKE '%INSUFFICIENT_EVIDENCE%' THEN 1 ELSE 0 END) no_evidence,
                     sum(CASE WHEN ({degraded_predicate}) THEN 1 ELSE 0 END) degraded,
                     avg(CASE WHEN completed_at IS NOT NULL
                       THEN (julianday(completed_at)-julianday(created_at))*86400000 END) avg_latency_ms
                   FROM runs WHERE workspace_id=?""",
                (*degraded_patterns, workspace_id),
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
                "degraded": int(run_row["degraded"] or 0),
                "avg_latency_ms": round(float(run_row["avg_latency_ms"] or 0), 1),
            },
            "reviews": {row["status"]: row["count"] for row in review_rows},
            "feedback": {
                "total": feedback_total,
                "positive_rate": round(int(feedback_row["positive"] or 0) / feedback_total, 3) if feedback_total else None,
            },
            "recent_runs": [
                {
                    **{key: value for key, value in dict(row).items() if key != "warning_json"},
                    "warnings": (warnings := _loads(row["warning_json"], [])),
                    "warning_details": warning_details(warnings),
                }
                for row in recent
            ],
        }
