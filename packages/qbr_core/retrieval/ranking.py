"""Independent ranking, fusion, and semantic reranking policies."""

from __future__ import annotations

import logging
from typing import Any

from packages.qbr_core.retrieval.reranking import Reranker

logger = logging.getLogger(__name__)


class LexicalRanker:
    """Rank lexical candidates using BM25 position and deterministic overlap."""

    def rank(
        self,
        rows: list[dict[str, Any]],
        terms: list[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Return lexical candidates in deterministic relevance order."""
        for row in rows:
            content = str(row.get("content", "")).casefold()
            title = str(row.get("document_title", "")).casefold()
            overlap = sum(2 for term in terms if term in content) + sum(1 for term in terms if term in title)
            bm25_score = -float(row.get("lexical_rank") or 0)
            row["retrieval_score"] = round(bm25_score + overlap, 6)
            row["lexical_score"] = row["retrieval_score"]
        return sorted(
            rows,
            key=lambda item: (-float(item["retrieval_score"]), int(item.get("slide_no") or 0)),
        )[:top_k]


class ReciprocalRankFusion:
    """Fuse lexical and vector rankings with configurable RRF weights."""

    def __init__(self, *, k: int, lexical_weight: float, vector_weight: float) -> None:
        """Initialize reciprocal-rank fusion constants."""
        self.k = k
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight

    def fuse(
        self,
        lexical_rows: list[dict[str, Any]],
        vector_rows: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Return a deterministic fused candidate ranking."""
        fused: dict[str, dict[str, Any]] = {}
        scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}
        for source, weight, rows in (
            ("lexical", self.lexical_weight, lexical_rows),
            ("vector", self.vector_weight, vector_rows),
        ):
            for rank, row in enumerate(rows, 1):
                chunk_id = str(row["id"])
                if chunk_id not in fused or source == "lexical":
                    fused[chunk_id] = dict(row)
                elif row.get("vector_score") is not None:
                    fused[chunk_id]["vector_score"] = row["vector_score"]
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (self.k + rank)
                sources.setdefault(chunk_id, []).append(source)
        for chunk_id, row in fused.items():
            row["retrieval_score"] = round(scores[chunk_id], 8)
            row["retrieval_sources"] = sources[chunk_id]
        return sorted(
            fused.values(),
            key=lambda item: (-float(item["retrieval_score"]), int(item.get("slide_no") or 0), str(item["id"])),
        )[:top_k]


class SemanticRerankPolicy:
    """Apply an optional semantic reranker without compromising retrieval."""

    def __init__(self, reranker: Reranker | None, *, candidate_k: int, top_n: int) -> None:
        """Initialize the policy and its bounded candidate sizes."""
        self.reranker = reranker
        self.candidate_k = candidate_k
        self.top_n = top_n

    def apply(
        self,
        question: str,
        rows: list[dict[str, Any]],
        top_k: int,
        diagnostics: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Rerank candidates and return stable fallback diagnostics on failure."""
        details = dict(diagnostics or {})
        details["rerank_model"] = self.reranker.model if self.reranker else None
        if not enabled or self.reranker is None or len(rows) < 2:
            details["rerank_status"] = "disabled" if self.reranker is None else "skipped"
            return rows, details
        candidates = [dict(row) for row in rows[: self.candidate_k]]
        try:
            scores = self.reranker.rerank(
                question,
                [str(row.get("content") or "") for row in candidates],
                top_n=min(max(top_k, self.top_n), len(candidates)),
            )
        except Exception as exc:  # semantic rerank must never take down evidence retrieval
            logger.warning("Semantic rerank failed; keeping deterministic retrieval order", exc_info=True)
            details.update({"rerank_status": "fallback", "rerank_error": type(exc).__name__})
            return rows, details
        ranked: list[dict[str, Any]] = []
        used: set[int] = set()
        for score in sorted(scores, key=lambda item: (-item.relevance_score, item.index)):
            row = candidates[score.index]
            row["rerank_score"] = round(score.relevance_score, 8)
            row["pre_rerank_score"] = row.get("retrieval_score")
            ranked.append(row)
            used.add(score.index)
        ranked.extend(row for index, row in enumerate(candidates) if index not in used)
        ranked.extend(dict(row) for row in rows[len(candidates) :])
        details.update(
            {
                "rerank_status": "completed",
                "rerank_candidates": len(candidates),
                "rerank_results": len(scores),
            }
        )
        return ranked, details
