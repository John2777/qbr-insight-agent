from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .answering_chart import ChartAnswerMixin
from .answering_policy import AnswerPolicyMixin
from .answering_router import AnswerRoutingMixin
from .db import Database
from .evaluation_analysis import EvaluativeSignalAnalyzer
from .evidence import EvidencePackBuilder
from .negative_analysis import NegativeSignalAnalyzer
from .query_planning import QueryPlan, deterministic_plan
from .retrieval import EvidenceRetriever
from .skill_registry import SkillDescriptor, SkillRegistry


@dataclass(slots=True)
class AnswerResult:
    answer: str
    evidence: list[dict[str, Any]]
    warnings: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def legacy(self) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer, self.evidence, self.warnings


class DeterministicAnswerEngine(AnswerRoutingMixin, AnswerPolicyMixin, ChartAnswerMixin):
    """Evidence selection and replayable business reasoning without model calls."""

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
        self.evaluation_analyzer = EvaluativeSignalAnalyzer()
        self.negative_analyzer = NegativeSignalAnalyzer()

    def _scope_clause(self, document_ids: list[str]) -> tuple[str, list[Any]]:
        if not document_ids:
            return "", []
        placeholders = ",".join("?" for _ in document_ids)
        return f" AND d.id IN ({placeholders})", list(document_ids)

    @staticmethod
    def answer_mode(question: str) -> str:
        return deterministic_plan(question).intent

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
        diagnostics: dict[str, Any] = {"query_plan": plan.to_dict() if plan else None}
        answer, evidence, warnings = self._answer_internal(
            question,
            workspace_id,
            document_ids,
            plan=plan,
            diagnostics=diagnostics,
        )
        return AnswerResult(answer, evidence, warnings, diagnostics)
