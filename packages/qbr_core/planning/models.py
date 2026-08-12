from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """Describe one weighted query used by the evidence retrieval pipeline."""
    query_id: str
    text: str
    kind: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "query_id": self.query_id,
            "text": self.text,
            "kind": self.kind,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A retrieval plan whose original question remains the answer contract.

    The descriptive fields remain for compatibility and auditability, but are
    derived from the raw question rather than authored by the retrieval model.
    """

    original_question: str
    canonical_question: str
    task_summary: str
    answer_brief: str
    answer_language: str
    execution_profile: str
    document_ids: tuple[str, ...]
    retrieval_queries: tuple[RetrievalQuery, ...]
    hard_constraints: tuple[str, ...] = ()
    delivery_requirements: tuple[str, ...] = ("answer the user's request",)
    evidence_requirements: tuple[str, ...] = ("directly relevant evidence",)
    operations: tuple[str, ...] = ("answer from evidence",)
    allowed_content_roles: tuple[str, ...] = (
        "business_fact",
        "management_insight",
        "risk_signal",
        "table",
        "chart",
        "provenance",
        "methodology",
    )
    excluded_content_roles: tuple[str, ...] = ("boilerplate",)
    needs_visuals: bool = False
    visual_structure: dict[str, Any] = field(default_factory=dict)
    planner_confidence: float = 0.0
    planner: str = "linguistic_fallback"
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "original_question": self.original_question,
            "canonical_question": self.canonical_question,
            "task_summary": self.task_summary,
            "answer_brief": self.answer_brief,
            "answer_language": self.answer_language,
            "execution_profile": self.execution_profile,
            "document_ids": list(self.document_ids),
            "retrieval_queries": [item.to_dict() for item in self.retrieval_queries],
            "hard_constraints": list(self.hard_constraints),
            "delivery_requirements": list(self.delivery_requirements),
            "evidence_requirements": list(self.evidence_requirements),
            "operations": list(self.operations),
            "allowed_content_roles": list(self.allowed_content_roles),
            "excluded_content_roles": list(self.excluded_content_roles),
            "needs_visuals": self.needs_visuals,
            "visual_structure": self.visual_structure,
            "planner_confidence": self.planner_confidence,
            "planner": self.planner,
            "warnings": list(self.warnings),
            "diagnostics": self.diagnostics,
        }
