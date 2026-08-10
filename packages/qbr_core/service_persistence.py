from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .db import utc_now
from .evidence import classify_content_role
from .ids import new_id
from .parser import ParsedElement, ParsedPresentation, ParsedSlide
from .service_component import ServiceComponent
from .service_support import _number
from .vision import VisualKnowledge

logger = logging.getLogger(__name__)


class ParsedPersistenceService(ServiceComponent):
    def _persist_parsed(
        self,
        job_id: str,
        job: sqlite3.Row,
        parser_run_id: str,
        parsed: ParsedPresentation,
        renders: list[Path],
        render_warnings: list[str],
        visual_knowledge: dict[int, VisualKnowledge],
        visual_warnings: list[dict[str, Any]],
    ) -> None:
        warnings = [
            *parsed.warnings,
            *({"code": "RENDER_FALLBACK", "message": w} for w in render_warnings),
            *visual_warnings,
        ]
        status = "partial" if warnings or parsed.status == "partial" else "ready"
        charts_by_slide: dict[int, list[dict[str, Any]]] = {}
        for chart in parsed.charts:
            charts_by_slide.setdefault(int(chart["slide_number"]), []).append(chart)
        with self.db.transaction(immediate=True) as conn:
            self._advance_job(conn, job_id, "persisting", 0.72)
            for slide, render in zip(parsed.slides, renders, strict=True):
                self._persist_slide(
                    conn,
                    job,
                    parser_run_id,
                    slide,
                    render,
                    render_warnings,
                    charts_by_slide.get(slide.slide_no, []),
                    visual_knowledge.get(slide.slide_no),
                )
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

    def _persist_slide(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        parser_run_id: str,
        slide: ParsedSlide,
        render: Path,
        render_warnings: list[str],
        charts: list[dict[str, Any]],
        visual: VisualKnowledge | None,
    ) -> None:
        slide_id = new_id("slide")
        summary_parts = [part for part in [slide.title, *(element.text for element in slide.elements if element.text)] if part]
        conn.execute(
            "INSERT INTO slides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                slide_id,
                parser_run_id,
                job["version_id"],
                slide.slide_no,
                slide.title,
                " · ".join(summary_parts)[:2000],
                slide.notes,
                slide.width_emu,
                slide.height_emu,
                str(render),
                1.0 if not render_warnings else 0.8,
            ),
        )
        self._persist_non_chart_elements(conn, job, slide_id, slide)
        self._persist_chart_elements(conn, job, slide_id, slide, charts)
        self._persist_notes(conn, job, slide_id, slide)
        self._persist_visual_knowledge(conn, job, slide_id, slide, visual)

    def _persist_non_chart_elements(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        slide_id: str,
        slide: ParsedSlide,
    ) -> None:
        for element in (item for item in slide.elements if item.element_type != "chart"):
            element_id = self._insert_element(conn, slide_id, element)
            content = self._table_text(element) if element.element_type == "table" else element.text
            self._insert_chunk(conn, job, slide_id, element_id, element.element_type, content or "")

    def _persist_chart_elements(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        slide_id: str,
        slide: ParsedSlide,
        charts: list[dict[str, Any]],
    ) -> None:
        source_elements = [element for element in slide.elements if element.element_type == "chart"]
        for chart_index, chart in enumerate(charts):
            source = self._chart_source_element(slide, source_elements, chart, chart_index)
            element = ParsedElement(
                source.element_type,
                source.reading_order,
                source.bbox,
                chart.get("title"),
                chart,
                {"source": chart.get("data_source", "chart_cache_or_literal"), "chart_part": chart.get("chart_part")},
                float(chart.get("confidence", 0.4)),
            )
            element_id = self._insert_element(conn, slide_id, element)
            self._insert_chart(conn, job, slide_id, element_id, chart)

    def _chart_source_element(
        self,
        slide: ParsedSlide,
        source_elements: list[ParsedElement],
        chart: dict[str, Any],
        chart_index: int,
    ) -> ParsedElement:
        if chart_index < len(source_elements):
            return source_elements[chart_index]
        return ParsedElement(
            "chart",
            len(slide.elements) + chart_index + 1,
            self._normalize_chart_bbox(chart.get("bbox_emu"), slide),
            None,
            {},
            {"source": "native_ooxml", "part": chart.get("chart_part")},
            float(chart.get("confidence", 0.4)),
        )

    def _persist_notes(self, conn: sqlite3.Connection, job: sqlite3.Row, slide_id: str, slide: ParsedSlide) -> None:
        if not slide.notes:
            return
        notes = ParsedElement(
            "notes",
            len(slide.elements) + 1000,
            {"x": 0, "y": 0, "w": 1, "h": 1},
            slide.notes,
            {},
            {"source": "speaker_notes"},
            1.0,
        )
        notes_id = self._insert_element(conn, slide_id, notes)
        self._insert_chunk(conn, job, slide_id, notes_id, "notes", slide.notes)

    def _persist_visual_knowledge(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        slide_id: str,
        slide: ParsedSlide,
        visual: VisualKnowledge | None,
    ) -> None:
        if visual is None or not visual.chunks():
            return
        element = ParsedElement(
            "visual_knowledge",
            len(slide.elements) + 2000,
            {"x": 0, "y": 0, "w": 1, "h": 1},
            visual.summary,
            {
                "ocr_text": visual.ocr_text,
                "observations": list(visual.observations),
                "model": visual.model,
                "prompt_version": visual.prompt_version,
            },
            {"source": "visual_model", "model": visual.model, "prompt_version": visual.prompt_version},
            visual.confidence,
        )
        element_id = self._insert_element(conn, slide_id, element)
        for chunk_type, content in visual.chunks():
            self._insert_chunk(
                conn,
                job,
                slide_id,
                element_id,
                chunk_type,
                content,
                metadata_overrides={
                    "source_kind": "visual_model",
                    "model": visual.model,
                    "prompt_version": visual.prompt_version,
                    "confidence": visual.confidence,
                    "authority": "supplemental_visual",
                },
            )

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
                element_id,
                slide_id,
                element.element_type,
                element.reading_order,
                self.db.json(element.bbox),
                element.text,
                self.db.json(element.structured),
                self.db.json(element.provenance),
                element.confidence,
                review_status,
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
        value_axes = [axis for axis in candidates if str(axis.get("axis_type", "")).casefold() in {"valax", "value", "valueaxis"}]
        selected = value_axes[-1] if value_axes else candidates[-1] if candidates else None
        if selected is None:
            return {}
        all_value_axes = [axis for axis in axes if str(axis.get("axis_type", "")).casefold() in {"valax", "value", "valueaxis"}]
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
        *,
        metadata_overrides: dict[str, Any] | None = None,
    ) -> str | None:
        content = content.strip()
        if not content:
            return None
        chunk_id = new_id("chunk")
        digest = hashlib.sha256(content.encode()).hexdigest()
        metadata = {
            "element_type": chunk_type,
            "content_role": classify_content_role({"chunk_type": chunk_type, "content": content}),
            "parser_run_id": None,
            **(metadata_overrides or {}),
        }
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,1)",
            (
                chunk_id,
                job["workspace_id"],
                job["version_id"],
                slide_id,
                element_id,
                chunk_type,
                content,
                self.db.json(metadata),
                digest,
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
                chart_id,
                element_id,
                chart.get("title"),
                self.db.json(chart.get("chart_types", [])),
                self.db.json(chart.get("axes", [])),
                chart.get("data_source", "chart_cache_or_literal"),
                confidence,
                self.db.json(chart.get("warnings", [])),
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
                    series_id,
                    chart_id,
                    int(series.get("series_order", series_index)),
                    series_name,
                    series.get("chart_type"),
                    axis_id,
                    unit,
                    self.db.json(visual),
                    confidence,
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
                        new_id("point"),
                        series_id,
                        int(point.get("point_index", point_index)),
                        str(category) if category is not None else None,
                        str(category).casefold() if category is not None else None,
                        x_value,
                        y_value,
                        str(display) if display is not None else None,
                        confidence,
                        point.get("source"),
                        self.db.json(point),
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
                    new_id("review"),
                    job["workspace_id"],
                    element_id,
                    "pending",
                    reason,
                    self.db.json(chart),
                    utc_now(),
                ),
            )
