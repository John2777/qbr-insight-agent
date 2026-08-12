"""Selectable policies for combining lexical and vector retrieval lanes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from packages.qbr_core.retrieval.ranking import ReciprocalRankFusion


@dataclass(frozen=True, slots=True)
class RetrievalCandidates:
    """Contain candidate lanes and labels supplied to a retrieval strategy."""

    lexical: list[dict[str, Any]]
    vector: list[dict[str, Any]]
    lexical_label: str
    vector_backend: str | None
    fusion_limit: int


@dataclass(frozen=True, slots=True)
class StrategySelection:
    """Contain rows and the observable label selected by a strategy."""

    rows: list[dict[str, Any]]
    label: str


class RetrievalStrategy(Protocol):
    """Define how candidate lanes become one pre-rerank result."""

    def select(self, candidates: RetrievalCandidates) -> StrategySelection:
        """Select and combine candidate rows."""
        ...


class FtsRetrievalStrategy:
    """Use only deterministic lexical candidates."""

    def select(self, candidates: RetrievalCandidates) -> StrategySelection:
        """Return the lexical lane unchanged."""
        return StrategySelection(candidates.lexical, candidates.lexical_label)


class VectorRetrievalStrategy:
    """Prefer vector candidates and fall back safely to lexical evidence."""

    def select(self, candidates: RetrievalCandidates) -> StrategySelection:
        """Return vector rows when available or a labelled lexical fallback."""
        if not candidates.vector:
            return StrategySelection(candidates.lexical, f"{candidates.lexical_label}+vector_fallback")
        rows = self._with_vector_scores(candidates.vector)
        return StrategySelection(rows, f"vector:{candidates.vector_backend}")

    @staticmethod
    def _with_vector_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize vector similarity as the observable retrieval score."""
        for row in rows:
            row["retrieval_score"] = round(float(row.get("vector_score") or 0.0), 6)
        return rows


class HybridRetrievalStrategy:
    """Fuse lexical and vector lanes while preserving safe fallbacks."""

    def __init__(self, fusion: ReciprocalRankFusion) -> None:
        """Initialize hybrid retrieval with an explicit fusion policy."""
        self._fusion = fusion

    def select(self, candidates: RetrievalCandidates) -> StrategySelection:
        """Fuse both lanes or return the one lane that remains available."""
        if not candidates.vector:
            return StrategySelection(candidates.lexical, f"{candidates.lexical_label}+vector_fallback")
        if not candidates.lexical:
            rows = VectorRetrievalStrategy._with_vector_scores(candidates.vector)
            return StrategySelection(rows, f"vector:{candidates.vector_backend}+lexical_empty")
        rows = self._fusion.fuse(candidates.lexical, candidates.vector, candidates.fusion_limit)
        return StrategySelection(
            rows,
            f"hybrid_rrf:{candidates.lexical_label}+{candidates.vector_backend}",
        )
