from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query_id: str
    text: str
    kind: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "text": self.text,
            "kind": self.kind,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class QueryPlan:
    original_question: str
    canonical_question: str
    intent: str
    answer_language: str
    execution_profile: str
    document_ids: tuple[str, ...]
    retrieval_queries: tuple[RetrievalQuery, ...]
    hard_constraints: tuple[str, ...] = ()
    required_facets: tuple[str, ...] = ()
    allowed_content_roles: tuple[str, ...] = (
        "business_fact",
        "management_insight",
        "risk_signal",
        "table",
        "chart",
    )
    excluded_content_roles: tuple[str, ...] = ("boilerplate", "methodology")
    evaluation_polarity: str = "neutral"
    secondary_intents: tuple[str, ...] = ()
    operations: tuple[str, ...] = ("answer_from_evidence",)
    intent_confidence: float = 1.0
    planner: str = "deterministic"
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_question": self.original_question,
            "canonical_question": self.canonical_question,
            "intent": self.intent,
            "answer_language": self.answer_language,
            "execution_profile": self.execution_profile,
            "document_ids": list(self.document_ids),
            "retrieval_queries": [item.to_dict() for item in self.retrieval_queries],
            "hard_constraints": list(self.hard_constraints),
            "required_facets": list(self.required_facets),
            "allowed_content_roles": list(self.allowed_content_roles),
            "excluded_content_roles": list(self.excluded_content_roles),
            "evaluation_polarity": self.evaluation_polarity,
            "secondary_intents": list(self.secondary_intents),
            "active_intents": list(self.active_intents),
            "operations": list(self.operations),
            "intent_confidence": self.intent_confidence,
            "planner": self.planner,
            "warnings": list(self.warnings),
            "diagnostics": self.diagnostics,
        }

    @property
    def active_intents(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.intent, *self.secondary_intents)))

    @property
    def is_composite(self) -> bool:
        return len(self.active_intents) > 1
