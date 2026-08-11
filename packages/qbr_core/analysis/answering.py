from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from packages.qbr_core.analysis.calculations import ChartCalculator, VerifiedCalculation
from packages.qbr_core.analysis.charts.analyzer import ChartAnalyzer
from packages.qbr_core.foundation.database import Database
from packages.qbr_core.planning import QueryPlan, RetrievalQuery, deterministic_plan
from packages.qbr_core.retrieval.engine import EvidenceRetriever, RetrievalResult
from packages.qbr_core.retrieval.evidence import EvidencePack, EvidencePackBuilder
from packages.qbr_core.skills.registry import SkillDescriptor, SkillRegistry


@dataclass(slots=True)
class AnswerResult:
    """Grounding material for the answer model plus auditable evidence."""

    answer: str
    evidence: list[dict[str, Any]]
    warnings: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    grounding_context: str = ""

    def legacy(self) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Return the legacy tuple representation for compatible callers."""
        return self.answer, self.evidence, self.warnings


class DeterministicAnswerEngine:
    """Build evidence context without deciding the user's intent or prose shape.

    Deterministic components are limited to retrieval, evidence extraction and
    optional calculation skills.  User-facing synthesis belongs to the LLM.
    """

    def __init__(
        self,
        *,
        db: Database,
        retriever: EvidenceRetriever,
        skill_registry: SkillRegistry,
        table_reasoning_skill: SkillDescriptor,
    ) -> None:
        """Initialize the deterministic answer engine and its dependencies."""
        self.db = db
        self.retriever = retriever
        self.skill_registry = skill_registry
        self.table_reasoning_skill = table_reasoning_skill
        self.evidence_builder = EvidencePackBuilder()
        self.chart_calculator = ChartCalculator()
        self.chart_analyzer = ChartAnalyzer()

    def answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Produce an evidence-grounded answer for the supplied question."""
        return self.answer_result(question, workspace_id, document_ids).legacy()

    def answer_result(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        plan: QueryPlan | None = None,
    ) -> AnswerResult:
        """Produce an answer together with evidence and diagnostics."""
        task = plan or deterministic_plan(question, document_ids)
        retrieval = self.retriever.search_plan(task, workspace_id, document_ids, top_k=16)
        if len(document_ids) > 1:
            retrieval = self._supplement_document_lanes(task, workspace_id, document_ids, retrieval)
        pack = self.evidence_builder.build(task, retrieval.items, max_atoms=8)
        missing_documents = pack.diagnostics.get("missing_document_ids", [])
        if task.execution_profile == "deep" and (
            (pack.coverage.has_gaps and pack.coverage.supported_count < 2) or missing_documents
        ):
            retrieval, pack = self._retry_missing_evidence(task, workspace_id, document_ids, retrieval, pack)

        calculation = self._table_calculation(question, retrieval.items) if not task.needs_visuals else None
        chart_rows = self._load_chart_rows(workspace_id, document_ids) if calculation is None else []
        if calculation is None:
            calculation = self.chart_calculator.analyze(question, chart_rows)
        if calculation is None:
            preferred_element_ids = {
                str(item.get("element_id") or "")
                for item in pack.evidence
                if item.get("element_id") and str(item.get("content_role") or "") == "chart"
            }
            preferred_slide_ids = {
                str(item.get("slide_id") or "")
                for item in pack.evidence
                if item.get("slide_id")
            }
            calculation = self.chart_analyzer.analyze(
                question,
                chart_rows,
                plan=task,
                preferred_element_ids=preferred_element_ids,
                preferred_slide_ids=preferred_slide_ids,
            )
        if calculation is not None:
            pack = self.evidence_builder.with_additional_evidence(task, pack, calculation.evidence)
        evidence = self._merge_evidence(calculation, pack.evidence)
        warnings = list(task.warnings)
        if not pack.answerable and calculation is None:
            warnings.append("INSUFFICIENT_EVIDENCE")
        elif pack.coverage.has_gaps or pack.diagnostics.get("missing_document_ids"):
            warnings.append("PARTIAL_EVIDENCE_COVERAGE")
        diagnostics = {
            "query_plan": task.to_dict(),
            "answer_routing": {
                "strategy": "semantic_grounding",
                "task_summary": task.task_summary,
                "planner": task.planner,
            },
            "retrieval": {"strategy": retrieval.strategy, "query": retrieval.query, **retrieval.diagnostics},
            "evidence_pack": pack.to_dict(),
            "verified_calculation": calculation.text if calculation else None,
            "verified_calculation_facts": list(calculation.facts) if calculation else [],
            "verified_calculation_kind": calculation.kind if calculation else None,
            "verified_calculation_scope": calculation.scope if calculation else None,
            "chart_scope": calculation.scope if calculation and calculation.kind == "chart_analysis" else None,
        }
        grounding_context = self._render_grounding_context(task, evidence, calculation)
        safe_answer = self._render_safe_fallback(task, evidence, calculation, pack)
        return AnswerResult(
            safe_answer,
            evidence,
            list(dict.fromkeys(warnings)),
            diagnostics,
            grounding_context,
        )

    def _supplement_document_lanes(
        self,
        plan: QueryPlan,
        workspace_id: str,
        document_ids: list[str],
        retrieval: RetrievalResult,
    ) -> RetrievalResult:
        """Give every scoped document an independent retrieval lane before global evidence competition."""

        merged = {str(row["id"]): row for row in retrieval.items}
        lane_diagnostics: list[dict[str, Any]] = []
        lane_top_k = max(4, min(8, 16 // len(document_ids) + 2))
        for document_id in document_ids:
            lane = self.retriever.search_plan(plan, workspace_id, [document_id], top_k=lane_top_k)
            for row in lane.items:
                merged.setdefault(str(row["id"]), row)
            anchors = self.retriever.document_anchors(plan, workspace_id, document_id, limit=4)
            for row in anchors:
                merged.setdefault(str(row["id"]), row)
            lane_diagnostics.append(
                {
                    "document_id": document_id,
                    "candidate_count": len(lane.items),
                    "anchor_count": len(anchors),
                    "strategy": lane.strategy,
                }
            )
        return RetrievalResult(
            list(merged.values()),
            retrieval.strategy + "+document_lanes",
            retrieval.query,
            {**retrieval.diagnostics, "document_lanes": lane_diagnostics},
        )

    def _retry_missing_evidence(
        self,
        plan: QueryPlan,
        workspace_id: str,
        document_ids: list[str],
        retrieval: RetrievalResult,
        pack: EvidencePack,
    ) -> tuple[RetrievalResult, EvidencePack]:
        """Retry missing evidence for this deterministic answer engine."""
        followups = tuple(
            RetrievalQuery(f"gap{index}", requirement, "evidence_gap", 1.1)
            for index, requirement in enumerate(pack.coverage.gap_labels[:3], 1)
        )
        retry_plan = replace(plan, retrieval_queries=(*plan.retrieval_queries, *followups))
        retry = self.retriever.search_plan(retry_plan, workspace_id, document_ids, top_k=16)
        combined = {str(row["id"]): row for row in (*retrieval.items, *retry.items)}
        retry_pack = self.evidence_builder.build(plan, combined.values(), max_atoms=8)
        retry_quality = (
            len(retry_pack.diagnostics.get("covered_document_ids", [])),
            retry_pack.coverage.supported_count,
            len(retry_pack.atoms),
        )
        current_quality = (
            len(pack.diagnostics.get("covered_document_ids", [])),
            pack.coverage.supported_count,
            len(pack.atoms),
        )
        if retry_quality > current_quality:
            return retry, retry_pack
        return retrieval, pack

    def _load_chart_rows(self, workspace_id: str, document_ids: list[str]) -> list[dict[str, Any]]:
        """Load chart rows for this deterministic answer engine."""
        scope = ""
        args: list[Any] = [workspace_id]
        if document_ids:
            scope = f" AND d.id IN ({','.join('?' for _ in document_ids)})"
            args.extend(document_ids)
        with self.db.read() as conn:
            rows = conn.execute(
                f"""
                SELECT cp.*,cs.name series_name,cs.chart_type,cs.unit,cs.axis_id,cs.visual_json,
                  cs.confidence series_confidence,c.title chart_title,c.axes_json,
                  c.id chart_id,c.source_kind,c.confidence chart_confidence,e.id element_id,e.bbox_json,
                  s.id slide_id,s.slide_no,s.title slide_title,s.summary slide_summary,
                  dv.id document_version_id,d.id document_id,d.title document_title
                FROM chart_points cp JOIN chart_series cs ON cs.id=cp.series_id
                JOIN charts c ON c.id=cs.chart_id JOIN elements e ON e.id=c.element_id
                JOIN slides s ON s.id=e.slide_id JOIN document_versions dv ON dv.id=s.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE d.workspace_id=? AND d.deleted_at IS NULL
                  AND s.parser_run_id=dv.active_parser_run_id {scope}
                """,
                tuple(args),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _merge_evidence(
        calculation: VerifiedCalculation | None,
        pack_evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge evidence for this deterministic answer engine."""
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        scoped_pack = pack_evidence
        if calculation is not None:
            slide_id = str(calculation.scope.get("slide_id") or "")
            if slide_id:
                scoped_pack = [item for item in pack_evidence if str(item.get("slide_id") or "") == slide_id]
        for item in (*(calculation.evidence if calculation else ()), *scoped_pack):
            key = (
                str(item.get("slide_id") or ""),
                str(item.get("element_id") or item.get("chunk_id") or ""),
                str(item.get("quote") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _render_grounding_context(
        plan: QueryPlan,
        evidence: list[dict[str, Any]],
        calculation: VerifiedCalculation | None,
    ) -> str:
        """Render grounding context for this deterministic answer engine."""
        if not evidence:
            return "没有检索到可用于回答的文档证据。" if plan.answer_language == "zh" else "No document evidence was retrieved."
        sections: list[str] = []
        if calculation is not None:
            label = "确定性计算结果" if plan.answer_language == "zh" else "Verified calculation"
            sections.append(f"{label}:\n{calculation.text}")
        evidence_label = "编号证据" if plan.answer_language == "zh" else "Numbered evidence"
        lines = [f"[{index}] {item.get('quote', '')}" for index, item in enumerate(evidence, 1)]
        sections.append(evidence_label + ":\n" + "\n".join(lines))
        return "\n\n".join(sections)

    @staticmethod
    def _compact_chart_quote(quote: str) -> str:
        """Turn a raw chart-series dump into a small, replayable fact summary."""

        line = next((item.strip() for item in quote.splitlines() if item.strip()), quote.strip())
        points = re.findall(r"([^;=|]+)=\s*([-+]?\d+(?:\.\d+)?%?)", line)
        if len(points) < 4:
            return line
        series_name = line.split("|", 1)[0].strip()
        first_label, first_value = points[0][0].strip(), points[0][1]
        last_label, last_value = points[-1][0].strip(), points[-1][1]
        return f"{series_name}：{first_label}={first_value}，{last_label}={last_value}（图表共 {len(points)} 个观测点）"

    @classmethod
    def _render_safe_fallback(
        cls,
        plan: QueryPlan,
        evidence: list[dict[str, Any]],
        calculation: VerifiedCalculation | None,
        pack: EvidencePack,
    ) -> str:
        """Render a readable, evidence-preserving answer when model prose is unavailable."""

        if not evidence:
            return "当前文档范围内没有检索到足够证据回答这个问题。" if plan.answer_language == "zh" else (
                "The current document scope did not yield enough evidence to answer this question."
            )

        if calculation is not None and calculation.fallback_text:
            if not pack.coverage.has_gaps:
                return calculation.fallback_text
            gaps = pack.coverage.gap_labels[:3]
            limitation = (
                "\n\n当前资料暂未支持以下内容：" + "、".join(gaps) + "。"
                if plan.answer_language == "zh"
                else "\n\nThe current sources do not yet support: " + "; ".join(gaps) + "."
            )
            return calculation.fallback_text + limitation

        task_text = " ".join(
            [plan.original_question, plan.canonical_question, plan.task_summary, plan.answer_brief, *plan.operations]
        ).casefold()
        provenance_requested = any(
            marker in task_text
            for marker in ("来源", "出处", "公开披露", "数据源", "source", "provenance", "methodology", "口径")
        )
        labels_zh = {
            "risk_signal": "风险或压力信号",
            "management_insight": "管理关注事项",
            "chart": "图表事实",
            "table": "表格事实",
            "business_fact": "业务事实",
            "provenance": "来源说明",
            "methodology": "方法说明",
        }
        labels_en = {
            "risk_signal": "Risk or pressure signal",
            "management_insight": "Management attention",
            "chart": "Chart fact",
            "table": "Table fact",
            "business_fact": "Business fact",
            "provenance": "Source note",
            "methodology": "Methodology note",
        }

        lines: list[str] = []
        if calculation is not None:
            label = "已验证计算" if plan.answer_language == "zh" else "Verified calculation"
            lines.append(f"- {label}：{calculation.text}")
        for index, item in enumerate(evidence, 1):
            if calculation is not None and index <= len(calculation.evidence):
                continue
            role = str(item.get("content_role") or "business_fact")
            if role in {"provenance", "methodology"} and not provenance_requested:
                continue
            quote = str(item.get("quote") or "").strip()
            if not quote:
                continue
            if role == "chart":
                quote = cls._compact_chart_quote(quote)
            labels = labels_zh if plan.answer_language == "zh" else labels_en
            lines.append(f"- {labels.get(role, labels['business_fact'])}：{quote} [{index}]")
            if len(lines) >= 5:
                break

        if not lines:
            # A source-oriented task may legitimately contain only provenance;
            # otherwise expose one bounded fact instead of an unbounded dump.
            item = evidence[0]
            lines.append(f"- {str(item.get('quote') or '').strip()} [1]")

        if plan.answer_language == "zh":
            heading = "基于当前可核验证据，可以确认："
            gaps = "、".join(pack.coverage.gap_labels[:3])
            limitation = (
                f"\n\n当前资料暂未支持以下内容：{gaps}。其余回答仅保留当前文档能够直接确认的内容。"
                if pack.coverage.has_gaps
                else ""
            )
        else:
            heading = "Based on the currently verifiable evidence:"
            limitation = (
                "\n\nThe current sources do not yet support: "
                + "; ".join(pack.coverage.gap_labels[:3])
                + ". The rest of the answer includes only content directly supported by the documents."
                if pack.coverage.has_gaps
                else ""
            )
        return heading + "\n\n" + "\n".join(lines) + limitation

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        """Expose deterministic calculation as a tool; never use it as a prose router."""
        if not sources:
            return None
        loaded = self.skill_registry.load(self.table_reasoning_skill)
        return loaded.module.answer(question, sources)

    def _table_calculation(self, question: str, sources: list[dict[str, Any]]) -> VerifiedCalculation | None:
        """Convert a table skill result into the same auditable contract used by chart calculations."""

        result = self._table_reasoning_answer(question, sources)
        if result is None:
            return None
        source = dict(result.source)
        try:
            bbox = json.loads(str(source.get("bbox_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            bbox = {}
        scope = {
            "kind": "table_calculation",
            "operation": result.operation,
            "document_id": source.get("document_id"),
            "slide_id": source.get("slide_id"),
            "slide_no": source.get("slide_no"),
            "element_ids": [source["element_id"]] if source.get("element_id") else [],
            "chunk_ids": [source["id"]] if source.get("id") else [],
        }
        evidence = {
            "document_version_id": source.get("document_version_id"),
            "document_id": source.get("document_id"),
            "slide_id": source.get("slide_id"),
            "element_id": source.get("element_id"),
            "chunk_id": source.get("id"),
            "quote": str(source.get("content") or ""),
            "bbox": bbox if isinstance(bbox, dict) else {},
            "confidence": float(source.get("confidence") or source.get("element_confidence") or 1.0),
            "source_kind": source.get("source_kind") or "native_ooxml",
            "document_title": source.get("document_title"),
            "slide_no": source.get("slide_no"),
            "slide_title": source.get("slide_title"),
            "content_role": "table",
            "facet": "verified table calculation",
            "extraction": "native_table_calculation",
            "calculation_operation": result.operation,
            "calculation_scope": scope,
        }
        return VerifiedCalculation(
            text=result.answer,
            evidence=(evidence,),
            kind="table_calculation",
            scope=scope,
            fallback_text=result.answer,
        )
