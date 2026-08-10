from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .calculations import ChartCalculator, VerifiedCalculation
from .db import Database
from .evidence import EvidencePack, EvidencePackBuilder
from .query_planning import QueryPlan, RetrievalQuery, deterministic_plan
from .retrieval import EvidenceRetriever, RetrievalResult
from .skill_registry import SkillDescriptor, SkillRegistry


@dataclass(slots=True)
class AnswerResult:
    """Grounding material for the answer model plus auditable evidence."""

    answer: str
    evidence: list[dict[str, Any]]
    warnings: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def legacy(self) -> tuple[str, list[dict[str, Any]], list[str]]:
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
        self.db = db
        self.retriever = retriever
        self.skill_registry = skill_registry
        self.table_reasoning_skill = table_reasoning_skill
        self.evidence_builder = EvidencePackBuilder()
        self.chart_calculator = ChartCalculator()

    def answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer_result(question, workspace_id, document_ids).legacy()

    def answer_result(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        plan: QueryPlan | None = None,
    ) -> AnswerResult:
        task = plan or deterministic_plan(question, document_ids)
        retrieval = self.retriever.search_plan(task, workspace_id, document_ids, top_k=16)
        pack = self.evidence_builder.build(task, retrieval.items, max_atoms=8)
        if task.execution_profile == "deep" and pack.missing_facets and len(pack.covered_facets) < 2:
            retrieval, pack = self._retry_missing_evidence(task, workspace_id, document_ids, retrieval, pack)

        calculation = self.chart_calculator.analyze(
            question,
            self._load_chart_rows(workspace_id, document_ids),
        )
        evidence = self._merge_evidence(calculation, pack.evidence)
        warnings = list(task.warnings)
        if not pack.answerable and calculation is None:
            warnings.append("INSUFFICIENT_EVIDENCE")
        elif pack.missing_facets and calculation is None:
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
        }
        return AnswerResult(
            self._render_grounding_context(task, evidence, calculation),
            evidence,
            list(dict.fromkeys(warnings)),
            diagnostics,
        )

    def _retry_missing_evidence(
        self,
        plan: QueryPlan,
        workspace_id: str,
        document_ids: list[str],
        retrieval: RetrievalResult,
        pack: EvidencePack,
    ) -> tuple[RetrievalResult, EvidencePack]:
        followups = tuple(
            RetrievalQuery(f"gap{index}", requirement, "evidence_gap", 1.1)
            for index, requirement in enumerate(pack.missing_facets[:3], 1)
        )
        retry_plan = replace(plan, retrieval_queries=(*plan.retrieval_queries, *followups))
        retry = self.retriever.search_plan(retry_plan, workspace_id, document_ids, top_k=16)
        combined = {str(row["id"]): row for row in (*retrieval.items, *retry.items)}
        retry_pack = self.evidence_builder.build(plan, combined.values(), max_atoms=8)
        if (len(retry_pack.covered_facets), len(retry_pack.atoms)) > (len(pack.covered_facets), len(pack.atoms)):
            return retry, retry_pack
        return retrieval, pack

    def _load_chart_rows(self, workspace_id: str, document_ids: list[str]) -> list[dict[str, Any]]:
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
                  c.source_kind,c.confidence chart_confidence,e.id element_id,e.bbox_json,
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
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in (*(calculation.evidence if calculation else ()), *pack_evidence):
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

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        """Expose deterministic calculation as a tool; never use it as a prose router."""
        if not sources:
            return None
        loaded = self.skill_registry.load(self.table_reasoning_skill)
        return loaded.module.answer(question, sources)
