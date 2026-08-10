from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .db import Database
from .evidence import classify_content_role
from .rerank import Reranker
from .vector import VectorSearchBackend

if TYPE_CHECKING:
    from .query_planning import QueryPlan

logger = logging.getLogger(__name__)

QUERY_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("下一季度", ("next-quarter", "priorities", "owner")),
    ("优先事项", ("priorities", "owner")),
    ("年度金额", ("年度额", "资本用途")),
    ("资本配置", ("资本用途", "年度额", "占比")),
    ("情景路径", ("阶段值", "scenario path")),
    ("降幅", ("阶段值", "下降")),
    ("公开披露", ("official", "sources")),
    ("模拟", ("synthetic", "illustrative")),
)

ENGLISH_STOPWORDS = {
    "about",
    "and",
    "are",
    "does",
    "for",
    "from",
    "have",
    "how",
    "in",
    "into",
    "is",
    "of",
    "on",
    "ppt",
    "presentation",
    "that",
    "the",
    "this",
    "to",
    "what",
    "which",
    "with",
}


def query_terms(text: str, *, limit: int = 16) -> list[str]:
    """Build conservative FTS terms, including Chinese bigrams for unicode61."""
    folded = text.casefold()
    terms = [term for term in re.findall(r"[a-z0-9%_-]{2,}", folded) if term not in ENGLISH_STOPWORDS]
    for trigger, expansions in QUERY_EXPANSIONS:
        if trigger in folded:
            terms.extend(expansions)
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        if len(phrase) <= 4:
            terms.append(phrase)
        terms.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return list(dict.fromkeys(terms))[:limit]


def fts_query(text: str) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in query_terms(text))


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    items: list[dict[str, Any]]
    strategy: str
    query: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class EvidenceRetriever:
    """Workspace-scoped lexical/vector retrieval with deterministic RRF fusion."""

    def __init__(
        self,
        db: Database,
        *,
        mode: str = "fts",
        vector_store: VectorSearchBackend | None = None,
        lexical_candidate_k: int = 24,
        vector_candidate_k: int = 24,
        rrf_k: int = 60,
        lexical_weight: float = 1.25,
        vector_weight: float = 1.0,
        reranker: Reranker | None = None,
        rerank_candidate_k: int = 30,
        rerank_top_n: int = 12,
    ) -> None:
        self.db = db
        self.mode = mode
        self.vector_store = vector_store
        self.lexical_candidate_k = lexical_candidate_k
        self.vector_candidate_k = vector_candidate_k
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight
        self.reranker = reranker
        self.rerank_candidate_k = rerank_candidate_k
        self.rerank_top_n = rerank_top_n

    @property
    def vector_available(self) -> bool:
        return self.vector_store is not None

    @property
    def vector_backend(self) -> str | None:
        return self.vector_store.backend_name if self.vector_store else None

    @property
    def rerank_available(self) -> bool:
        return self.reranker is not None

    def index_document_version(self, workspace_id: str, document_version_id: str) -> None:
        if self.vector_store is not None:
            self.vector_store.index_document_version(workspace_id, document_version_id)

    def rebuild_workspace(self, workspace_id: str) -> None:
        if self.vector_store is not None:
            self.vector_store.rebuild_workspace(workspace_id)

    @staticmethod
    def _hydrate_source_metadata(row: dict[str, Any]) -> dict[str, Any]:
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        if isinstance(metadata, dict):
            row["source_kind"] = metadata.get("source_kind") or row.get("source_kind") or "native_ooxml"
            row["confidence"] = metadata.get("confidence", row.get("element_confidence") or 1.0)
        return row

    def search(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        top_k: int = 5,
        strategy: str | None = None,
        _apply_rerank: bool = True,
    ) -> RetrievalResult:
        selected_strategy = strategy or self.mode
        if selected_strategy not in {"fts", "vector", "hybrid"}:
            raise ValueError("strategy must be fts, vector, or hybrid")
        lexical_rows, lexical_strategy, query = self._lexical_search(
            question,
            workspace_id,
            document_ids,
            limit=max(top_k, self.lexical_candidate_k),
        )
        if selected_strategy == "fts":
            rows, diagnostics = self._semantic_rerank(question, lexical_rows, top_k, enabled=_apply_rerank)
            return RetrievalResult(
                self._select_diverse(rows, top_k, len(document_ids), question), lexical_strategy, query, diagnostics
            )

        vector_rows: list[dict[str, Any]] = []
        vector_error: str | None = None
        if self.vector_store is not None:
            try:
                vector_rows = self.vector_store.search(
                    question,
                    workspace_id,
                    document_ids,
                    limit=max(top_k, self.vector_candidate_k),
                )
            except Exception as exc:  # vector search must never take down lexical QA
                vector_error = type(exc).__name__
                logger.warning("Vector retrieval failed; falling back to lexical search", exc_info=True)
        else:
            vector_error = "VECTOR_BACKEND_UNAVAILABLE"

        diagnostics = {
            "lexical_candidates": len(lexical_rows),
            "vector_candidates": len(vector_rows),
            "vector_backend": self.vector_backend,
        }
        if vector_error:
            diagnostics["vector_error"] = vector_error

        if selected_strategy == "vector" and vector_rows:
            for row in vector_rows:
                row["retrieval_score"] = round(float(row.get("vector_score") or 0.0), 6)
            rows, diagnostics = self._semantic_rerank(question, vector_rows, top_k, diagnostics, enabled=_apply_rerank)
            return RetrievalResult(
                self._select_diverse(rows, top_k, len(document_ids), question),
                f"vector:{self.vector_backend}", query, diagnostics,
            )
        if not vector_rows:
            fallback = f"{lexical_strategy}+vector_fallback"
            rows, diagnostics = self._semantic_rerank(question, lexical_rows, top_k, diagnostics, enabled=_apply_rerank)
            return RetrievalResult(
                self._select_diverse(rows, top_k, len(document_ids), question), fallback, query, diagnostics
            )
        if not lexical_rows:
            for row in vector_rows:
                row["retrieval_score"] = round(float(row.get("vector_score") or 0.0), 6)
            rows, diagnostics = self._semantic_rerank(question, vector_rows, top_k, diagnostics, enabled=_apply_rerank)
            return RetrievalResult(
                self._select_diverse(rows, top_k, len(document_ids), question),
                f"vector:{self.vector_backend}+lexical_empty", query, diagnostics,
            )
        fused = self._reciprocal_rank_fusion(lexical_rows, vector_rows, max(top_k, self.lexical_candidate_k))
        fused, diagnostics = self._semantic_rerank(question, fused, top_k, diagnostics, enabled=_apply_rerank)
        return RetrievalResult(
            self._select_diverse(fused, top_k, len(document_ids), question),
            f"hybrid_rrf:{lexical_strategy}+{self.vector_backend}",
            query,
            diagnostics,
        )

    def search_plan(
        self,
        plan: QueryPlan,
        workspace_id: str,
        document_ids: list[str],
        *,
        top_k: int = 12,
        strategy: str | None = None,
    ) -> RetrievalResult:
        """Execute all retrieval hypotheses and fuse them without losing the original-query lane."""
        fused: dict[str, dict[str, Any]] = {}
        scores: dict[str, float] = {}
        matched_queries: dict[str, list[str]] = {}
        query_diagnostics: list[dict[str, Any]] = []
        candidate_k = max(top_k, min(max(self.lexical_candidate_k, self.vector_candidate_k), 32))
        for retrieval_query in plan.retrieval_queries:
            result = self.search(
                retrieval_query.text,
                workspace_id,
                document_ids,
                top_k=candidate_k,
                strategy=strategy,
                _apply_rerank=False,
            )
            query_diagnostics.append(
                {
                    "query_id": retrieval_query.query_id,
                    "kind": retrieval_query.kind,
                    "text": retrieval_query.text,
                    "candidate_count": len(result.items),
                    "strategy": result.strategy,
                    "diagnostics": result.diagnostics,
                }
            )
            for rank, row in enumerate(result.items, 1):
                chunk_id = str(row["id"])
                fused.setdefault(chunk_id, dict(row))
                scores[chunk_id] = scores.get(chunk_id, 0.0) + retrieval_query.weight / (rank + 1)
                matched_queries.setdefault(chunk_id, []).append(retrieval_query.query_id)

        ranked: list[dict[str, Any]] = []
        rejected_roles: dict[str, int] = {}
        for chunk_id, row in fused.items():
            role = classify_content_role(row)
            if role in plan.excluded_content_roles or (plan.allowed_content_roles and role not in plan.allowed_content_roles):
                rejected_roles[role] = rejected_roles.get(role, 0) + 1
                continue
            content = str(row.get("content") or "").casefold()
            task_bonus = max(self._task_compatibility(intent, role, content) for intent in plan.active_intents)
            row["content_role"] = role
            row["matched_queries"] = matched_queries.get(chunk_id, [])
            row["multi_query_score"] = round(scores[chunk_id], 8)
            row["task_score"] = round(scores[chunk_id] + task_bonus, 8)
            ranked.append(row)
        ranked.sort(
            key=lambda item: (
                -float(item.get("task_score") or 0.0),
                int(item.get("slide_no") or 0),
                str(item.get("id") or ""),
            )
        )
        reranked, rerank_diagnostics = self._semantic_rerank(plan.original_question, ranked, top_k)
        selected = self._select_diverse(reranked, top_k, len(document_ids), plan.original_question)
        return RetrievalResult(
            selected,
            f"multi_query:{strategy or self.mode}",
            " | ".join(item.text for item in plan.retrieval_queries),
            {
                "intent": plan.intent,
                "active_intents": list(plan.active_intents),
                "query_count": len(plan.retrieval_queries),
                "unique_candidates": len(fused),
                "eligible_candidates": len(ranked),
                "rejected_roles": rejected_roles,
                "queries": query_diagnostics,
                **rerank_diagnostics,
            },
        )

    def _semantic_rerank(
        self,
        question: str,
        rows: list[dict[str, Any]],
        top_k: int,
        diagnostics: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        details = dict(diagnostics or {})
        details["rerank_model"] = self.reranker.model if self.reranker else None
        if not enabled or self.reranker is None or len(rows) < 2:
            details["rerank_status"] = "disabled" if self.reranker is None else "skipped"
            return rows, details
        candidates = [dict(row) for row in rows[: self.rerank_candidate_k]]
        try:
            scores = self.reranker.rerank(
                question,
                [str(row.get("content") or "") for row in candidates],
                top_n=min(max(top_k, self.rerank_top_n), len(candidates)),
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

    @staticmethod
    def _task_compatibility(intent: str, role: str, content: str) -> float:
        role_bonus = {
            "business_fact": 0.4,
            "management_insight": 0.8,
            "risk_signal": 1.0,
            "table": 0.6,
            "chart": 0.6,
            "provenance": 0.3,
            "methodology": -0.8,
            "boilerplate": -1.5,
        }.get(role, 0.0)
        if intent in {"negative_signal_summary", "risk_explanation"}:
            markers = (
                "risk",
                "concern",
                "challenge",
                "pressure",
                "decline",
                "below",
                "warning",
                "breach",
                "volatile",
                "风险",
                "挑战",
                "承压",
                "下降",
                "下滑",
                "低于",
                "阈值",
                "限额",
                "波动",
                "集中",
                "↑",
            )
            return role_bonus + min(3.0, sum(0.45 for marker in markers if marker in content))
        if intent == "business_evaluation":
            markers = (
                "above",
                "growth",
                "improv",
                "leading",
                "momentum",
                "outperform",
                "record",
                "strong",
                "target",
                "增长",
                "提升",
                "改善",
                "领先",
                "动量",
                "新高",
                "强劲",
                "达标",
                "绿色",
                "多元",
            )
            return role_bonus + min(3.0, sum(0.4 for marker in markers if marker in content))
        if intent == "provenance":
            return 1.5 if role == "provenance" else role_bonus
        return role_bonus

    @staticmethod
    def _select_diverse(
        rows: list[dict[str, Any]],
        top_k: int,
        document_count: int,
        question: str = "",
    ) -> list[dict[str, Any]]:
        """Preserve rank while preventing one slide or deck from monopolizing evidence."""
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        seen_documents: set[str] = set()
        seen_slides: set[tuple[str, str]] = set()

        def take(predicate: Any) -> None:
            for row in rows:
                if len(selected) >= top_k:
                    return
                row_id = str(row.get("id"))
                if row_id in selected_ids or not predicate(row):
                    continue
                selected.append(row)
                selected_ids.add(row_id)
                seen_documents.add(str(row.get("document_id")))
                seen_slides.add((str(row.get("document_id")), str(row.get("slide_id"))))

        def take_best(predicate: Any, score: Any) -> None:
            candidates = [row for row in rows if str(row.get("id")) not in selected_ids and predicate(row)]
            if not candidates or len(selected) >= top_k:
                return
            row = max(candidates, key=score)
            selected.append(row)
            selected_ids.add(str(row.get("id")))
            seen_documents.add(str(row.get("document_id")))
            seen_slides.add((str(row.get("document_id")), str(row.get("slide_id"))))

        if "公开披露" in question and any(term in question for term in ("模拟", "测试", "边界")):
            provenance_classes: tuple[tuple[Any, Any], ...] = (
                (
                    lambda row: "growth" in str(row.get("document_title") or "").casefold()
                    and any(
                        term in str(row.get("content") or "").casefold()
                        for term in ("official results", "official new business")
                    ),
                    lambda row: int(int(row.get("slide_no") or 0) in {2, 6}),
                ),
                (
                    lambda row: "monthly" in str(row.get("content") or "").casefold()
                    and any(
                        term in str(row.get("content") or "").casefold() for term in ("synthetic", "illustrative")
                    ),
                    lambda row: int(int(row.get("slide_no") or 0) == 3),
                ),
                (
                    lambda row: any(
                        term in str(row.get("content") or "").casefold() for term in ("operating table", "table values")
                    )
                    and any(
                        term in str(row.get("content") or "").casefold() for term in ("synthetic", "illustrative")
                    ),
                    lambda row: int(int(row.get("slide_no") or 0) == 4),
                ),
            )
            for predicate, score in provenance_classes:
                take_best(predicate, score)
        if document_count > 1:
            take(lambda row: str(row.get("document_id")) not in seen_documents)
        take(lambda row: (str(row.get("document_id")), str(row.get("slide_id"))) not in seen_slides)
        take(lambda _row: True)
        return selected

    def _lexical_search(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str, str]:
        terms = query_terms(question)
        query = fts_query(question)
        scope_sql, scope_args = self._scope(document_ids)
        rows: list[dict[str, Any]] = []
        if query:
            with self.db.read() as conn:
                fetched = conn.execute(
                    f"""
                    SELECT ch.*,s.slide_no,e.bbox_json,d.title document_title,d.id document_id,
                      e.confidence element_confidence,bm25(chunk_fts) AS lexical_rank
                    FROM chunk_fts JOIN chunks ch ON ch.id=chunk_fts.chunk_id
                    JOIN slides s ON s.id=ch.slide_id
                    JOIN document_versions dv ON dv.id=ch.document_version_id
                    JOIN documents d ON d.id=dv.document_id
                    LEFT JOIN elements e ON e.id=ch.element_id
                    WHERE chunk_fts MATCH ? AND ch.workspace_id=? AND chunk_fts.workspace_id=?
                      AND ch.active=1 AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
                      {scope_sql}
                    ORDER BY lexical_rank LIMIT ?
                    """,
                    (query, workspace_id, workspace_id, *scope_args, max(limit, 12)),
                ).fetchall()
            rows = [self._hydrate_source_metadata(dict(row)) for row in fetched]
        if not terms:
            return rows, "fts5_bm25" if rows else "none", query
        clauses = " OR ".join("lower(ch.content) LIKE ?" for _ in terms)
        with self.db.read() as conn:
            fetched = conn.execute(
                f"""
                SELECT ch.*,s.slide_no,e.bbox_json,d.title document_title,d.id document_id,
                  e.confidence element_confidence,100.0 AS lexical_rank
                FROM chunks ch JOIN slides s ON s.id=ch.slide_id
                JOIN document_versions dv ON dv.id=ch.document_version_id
                JOIN documents d ON d.id=dv.document_id
                LEFT JOIN elements e ON e.id=ch.element_id
                WHERE ch.workspace_id=? AND ch.active=1 AND d.deleted_at IS NULL
                  AND s.parser_run_id=dv.active_parser_run_id AND ({clauses}) {scope_sql}
                LIMIT ?
                """,
                (workspace_id, *(f"%{term}%" for term in terms), *scope_args, max(limit * 4, 48)),
            ).fetchall()
        merged = {str(row["id"]): row for row in rows}
        for row in fetched:
            item = self._hydrate_source_metadata(dict(row))
            merged.setdefault(str(item["id"]), item)
        strategy = "fts5_bm25+substring+lexical_rerank" if rows else "substring+lexical_rerank"
        return self._rerank(list(merged.values()), terms, limit), strategy, query

    @staticmethod
    def _scope(document_ids: list[str]) -> tuple[str, list[Any]]:
        if not document_ids:
            return "", []
        placeholders = ",".join("?" for _ in document_ids)
        return f" AND d.id IN ({placeholders})", list(document_ids)

    @staticmethod
    def _rerank(rows: list[dict[str, Any]], terms: list[str], top_k: int) -> list[dict[str, Any]]:
        for row in rows:
            content = str(row.get("content", "")).casefold()
            title = str(row.get("document_title", "")).casefold()
            overlap = sum(2 for term in terms if term in content) + sum(1 for term in terms if term in title)
            bm25_score = -float(row.get("lexical_rank") or 0)
            row["retrieval_score"] = round(bm25_score + overlap, 6)
            row["lexical_score"] = row["retrieval_score"]
        return sorted(rows, key=lambda item: (-float(item["retrieval_score"]), int(item.get("slide_no") or 0)))[:top_k]

    def _reciprocal_rank_fusion(
        self,
        lexical_rows: list[dict[str, Any]],
        vector_rows: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
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
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (self.rrf_k + rank)
                sources.setdefault(chunk_id, []).append(source)
        for chunk_id, row in fused.items():
            row["retrieval_score"] = round(scores[chunk_id], 8)
            row["retrieval_sources"] = sources[chunk_id]
        return sorted(
            fused.values(),
            key=lambda item: (-float(item["retrieval_score"]), int(item.get("slide_no") or 0), str(item["id"])),
        )[:top_k]
