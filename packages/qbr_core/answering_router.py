from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from .query_planning import QueryPlan, RetrievalQuery, deterministic_plan

AnswerPayload = tuple[str, list[dict[str, Any]], list[str]]
Contribution = tuple[str, AnswerPayload]


@dataclass(slots=True)
class AnswerRoutingState:
    question: str
    workspace_id: str
    document_ids: list[str]
    plan: QueryPlan | None
    diagnostics: dict[str, Any]
    intents: tuple[str, ...]
    contributions: list[Contribution] = field(default_factory=list)
    handled_intents: set[str] = field(default_factory=set)
    scoped_chunks: list[dict[str, Any]] = field(default_factory=list)
    chart_data: list[dict[str, Any]] = field(default_factory=list)

    @property
    def intent_set(self) -> set[str]:
        return set(self.intents)

    @property
    def primary_intent(self) -> str:
        return self.intents[0]

    @property
    def is_composite(self) -> bool:
        return len(self.intents) > 1

    def record(self, intent: str, result: AnswerPayload | None) -> AnswerPayload | None:
        if result is None:
            return None
        if not self.is_composite:
            return result
        self.contributions.append((intent, result))
        self.handled_intents.add(intent)
        return None


class AnswerRoutingMixin:
    """Routes an answer through focused deterministic and evidence strategies."""

    def _answer_internal(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        plan: QueryPlan | None,
        diagnostics: dict[str, Any],
    ) -> AnswerPayload:
        state = self._routing_state(question, workspace_id, document_ids, plan, diagnostics)
        scope_sql, scope_args = self._scope_clause(document_ids)
        with self.db.read() as conn:
            state.scoped_chunks = self._load_scoped_chunks(conn, workspace_id, scope_sql, scope_args)
            direct = self._route_content_strategies(state)
            if direct is not None:
                return direct
            state.chart_data = self._load_chart_data(conn, workspace_id, scope_sql, scope_args)
            structured = self._route_structured_strategies(state, conn, scope_sql, scope_args)
            if structured is not None:
                return structured
        if state.plan is not None:
            return self._route_planned_evidence(state)
        return self._legacy_retrieval_answer(state)

    @staticmethod
    def _routing_state(
        question: str,
        workspace_id: str,
        document_ids: list[str],
        plan: QueryPlan | None,
        diagnostics: dict[str, Any],
    ) -> AnswerRoutingState:
        fallback_plan = deterministic_plan(question)
        intents = plan.active_intents if plan is not None else fallback_plan.active_intents
        diagnostics["answer_routing"] = {
            "intent": intents[0],
            "active_intents": list(intents),
            "strategy": "composite" if len(intents) > 1 else "specialized",
            "source": "query_plan" if plan is not None else "deterministic_fallback",
        }
        return AnswerRoutingState(question, workspace_id, document_ids, plan, diagnostics, intents)

    @staticmethod
    def _load_scoped_chunks(conn: Any, workspace_id: str, scope_sql: str, scope_args: list[Any]) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT ch.*,s.slide_no,s.title slide_title,s.summary slide_summary,
              e.bbox_json,e.reading_order,d.title document_title,d.id document_id
            FROM chunks ch JOIN slides s ON s.id=ch.slide_id
            JOIN document_versions dv ON dv.id=ch.document_version_id
            JOIN documents d ON d.id=dv.document_id
            LEFT JOIN elements e ON e.id=ch.element_id
            WHERE ch.workspace_id=? AND ch.active=1
              AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
            ORDER BY d.updated_at DESC,s.slide_no,e.reading_order
            """,
            (workspace_id, *scope_args),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_chart_data(conn: Any, workspace_id: str, scope_sql: str, scope_args: list[Any]) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT cp.*,cs.name series_name,cs.chart_type,cs.unit,cs.axis_id,cs.visual_json,
              cs.confidence series_confidence,c.title chart_title,c.axes_json,
              c.source_kind,c.confidence chart_confidence,e.id element_id,e.bbox_json,s.id slide_id,s.slide_no,
              s.title slide_title,s.summary slide_summary,
              dv.id document_version_id,d.id document_id,d.title document_title
            FROM chart_points cp JOIN chart_series cs ON cs.id=cp.series_id JOIN charts c ON c.id=cs.chart_id
            JOIN elements e ON e.id=c.element_id JOIN slides s ON s.id=e.slide_id
            JOIN document_versions dv ON dv.id=s.document_version_id JOIN documents d ON d.id=dv.document_id
            WHERE d.workspace_id=? AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
            {scope_sql}
            """,
            (workspace_id, *scope_args),
        ).fetchall()
        return [dict(row) for row in rows]

    def _route_content_strategies(self, state: AnswerRoutingState) -> AnswerPayload | None:
        if "term_definition" in state.intent_set:
            direct = state.record("term_definition", self._term_definition_answer(state.question, state.scoped_chunks))
            if direct is not None:
                return direct
        if not state.is_composite and state.primary_intent == "evidence_answer":
            for strategy in (self._constraint_abstention, self._priority_answer):
                result = strategy(state.question, state.scoped_chunks)
                if result is not None:
                    return result
        if "provenance" in state.intent_set:
            direct = state.record("provenance", self._provenance_answer(state.question, state.scoped_chunks))
            if direct is not None:
                return direct
        table = self._table_strategy_result(state)
        return state.record("table_analysis", table)

    def _table_strategy_result(self, state: AnswerRoutingState) -> AnswerPayload | None:
        if self._has_explicit_chart_intent(state.question) or not ({"table_analysis", "evidence_answer"} & state.intent_set):
            return None
        rows = [row for row in state.scoped_chunks if row.get("chunk_type") == "table"]
        result = self._table_reasoning_answer(state.question, rows)
        if result is None:
            return None
        return result.answer, [self._chunk_evidence(result.source)], []

    def _route_structured_strategies(
        self,
        state: AnswerRoutingState,
        conn: Any,
        scope_sql: str,
        scope_args: list[Any],
    ) -> AnswerPayload | None:
        if "summary" in state.intent_set:
            slides = self._load_summary_slides(conn, state.workspace_id, scope_sql, scope_args)
            direct = state.record("summary", self._summary_answer(slides, state.chart_data))
            if direct is not None:
                return direct
        if not ({"chart_analysis", "evidence_answer"} & state.intent_set):
            return None
        return state.record("chart_analysis", self._generic_chart_result(state.question, state.chart_data))

    @staticmethod
    def _load_summary_slides(conn: Any, workspace_id: str, scope_sql: str, scope_args: list[Any]) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT s.id slide_id,s.slide_no,s.title,s.summary,s.document_version_id,
              d.id document_id,d.title document_title
            FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
            JOIN documents d ON d.id=dv.document_id
            WHERE d.workspace_id=? AND d.deleted_at IS NULL
              AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
            ORDER BY d.updated_at DESC,s.slide_no
            """,
            (workspace_id, *scope_args),
        ).fetchall()
        return [dict(row) for row in rows]

    def _generic_chart_result(self, question: str, chart_data: list[dict[str, Any]]) -> AnswerPayload | None:
        strategies = (
            self._composition_answer,
            self._max_drop_answer,
            self._chart_comparison_answer,
            self._chart_extreme_answer,
        )
        for strategy in strategies:
            result = strategy(question, chart_data)
            if result is not None:
                return result
        ranked = self._rank_chart_points(question, chart_data)
        if ranked and (self._has_explicit_chart_intent(question) or ranked[0][0] >= 6):
            return self._chart_answer(question, ranked)
        return None

    @staticmethod
    def _has_explicit_chart_intent(question: str) -> bool:
        folded = question.casefold()
        return any(term in folded for term in ("图", "月度", "控制图", "模拟值", "路径"))

    def _route_planned_evidence(self, state: AnswerRoutingState) -> AnswerPayload:
        plan = state.plan
        if plan is None:
            raise AssertionError("planned evidence routing requires a QueryPlan")
        pack = self._build_evidence_pack(state, plan)
        direct = self._route_business_evaluation(state, plan, pack)
        if direct is not None:
            return direct
        direct = self._route_negative_evaluation(state, plan, pack)
        if direct is not None:
            return direct
        if plan.is_composite:
            self._complete_composite_fallback(state, plan, pack)
            return self._merge_contributions(plan, state.contributions)
        return self._pack_fallback(plan, pack)

    def _build_evidence_pack(self, state: AnswerRoutingState, plan: QueryPlan) -> Any:
        retrieval = self.retriever.search_plan(plan, state.workspace_id, state.document_ids, top_k=16)
        pack = self.evidence_builder.build(plan, retrieval.items, max_atoms=8)
        state.diagnostics["retrieval"] = {"strategy": retrieval.strategy, "query": retrieval.query, **retrieval.diagnostics}
        state.diagnostics["evidence_pack"] = pack.to_dict()
        if plan.execution_profile != "deep" or not pack.missing_facets or len(pack.covered_facets) >= 2:
            return pack
        return self._retry_missing_facets(state, plan, retrieval, pack)

    def _retry_missing_facets(self, state: AnswerRoutingState, plan: QueryPlan, retrieval: Any, pack: Any) -> Any:
        followups = tuple(
            RetrievalQuery(
                query_id=f"gap{index}",
                text=facet.replace("_", " "),
                kind="coverage_gap",
                weight=1.1,
            )
            for index, facet in enumerate(pack.missing_facets[:3], 1)
        )
        retry_plan = replace(plan, retrieval_queries=(*plan.retrieval_queries, *followups))
        retry = self.retriever.search_plan(retry_plan, state.workspace_id, state.document_ids, top_k=16)
        combined = {str(row["id"]): row for row in (*retrieval.items, *retry.items)}
        retry_pack = self.evidence_builder.build(plan, combined.values(), max_atoms=8)
        state.diagnostics["coverage_retry"] = {
            "performed": True,
            "queries": [item.to_dict() for item in followups],
            "retrieval": retry.diagnostics,
            "evidence_pack": retry_pack.to_dict(),
        }
        if (len(retry_pack.covered_facets), len(retry_pack.atoms)) > (len(pack.covered_facets), len(pack.atoms)):
            state.diagnostics["evidence_pack"] = retry_pack.to_dict()
            return retry_pack
        return pack

    def _route_business_evaluation(self, state: AnswerRoutingState, plan: QueryPlan, pack: Any) -> AnswerPayload | None:
        if "business_evaluation" not in plan.active_intents:
            return None
        assessment = self.evaluation_analyzer.analyze(
            plan,
            explicit_pack=pack,
            chunks=state.scoped_chunks,
            chart_rows=state.chart_data,
        )
        state.diagnostics["evaluation_assessment"] = assessment.diagnostics
        answer, evidence, warnings = assessment.render(plan)
        result = answer, evidence, list(dict.fromkeys([*plan.warnings, *warnings]))
        return state.record("business_evaluation", result)

    def _route_negative_evaluation(self, state: AnswerRoutingState, plan: QueryPlan, pack: Any) -> AnswerPayload | None:
        requested = {"negative_signal_summary", "risk_explanation"} & set(plan.active_intents)
        if not requested:
            return None
        assessment = self.negative_analyzer.analyze(
            plan,
            explicit_pack=pack,
            chunks=state.scoped_chunks,
            chart_rows=state.chart_data,
        )
        state.diagnostics["negative_assessment"] = assessment.diagnostics
        renderers = {
            "risk_explanation": assessment.render_explanation,
            "negative_signal_summary": assessment.render,
        }
        for intent in plan.active_intents:
            if intent not in renderers:
                continue
            answer, evidence, warnings = renderers[intent](plan)
            result = answer, evidence, list(dict.fromkeys([*plan.warnings, *warnings]))
            direct = state.record(intent, result)
            if direct is not None:
                return direct
        return None

    @staticmethod
    def _complete_composite_fallback(state: AnswerRoutingState, plan: QueryPlan, pack: Any) -> None:
        unhandled = set(plan.active_intents) - state.handled_intents
        if unhandled or not state.contributions:
            warnings = [] if pack.answerable else ["INSUFFICIENT_EVIDENCE"]
            state.contributions.append(("evidence_answer", (pack.render_fallback(plan), pack.evidence, warnings)))

    @staticmethod
    def _pack_fallback(plan: QueryPlan, pack: Any) -> AnswerPayload:
        warnings = list(plan.warnings)
        if not pack.answerable:
            warnings.append("INSUFFICIENT_EVIDENCE")
        if pack.answerable and pack.missing_facets:
            warnings.append("PARTIAL_EVIDENCE_COVERAGE")
        return pack.render_fallback(plan), pack.evidence, list(dict.fromkeys(warnings))

    def _legacy_retrieval_answer(self, state: AnswerRoutingState) -> AnswerPayload:
        retrieval = self.retriever.search(state.question, state.workspace_id, state.document_ids, top_k=5)
        chunks = retrieval.items
        if not chunks:
            return (
                "当前文档范围内没有足够证据回答这个问题。请指定指标、季度、地区或相关页码；我不会用外部常识补齐内部业务事实。",
                [],
                ["INSUFFICIENT_EVIDENCE"],
            )
        selected = self._select_legacy_chunks(state.question, chunks)
        evidence = [self._chunk_evidence(dict(row)) for row in selected]
        statements = [f"- {row['content'][:350]} [{index}]" for index, row in enumerate(selected, 1)]
        return "根据文档中检索到的直接证据：\n\n" + "\n".join(statements), evidence, []

    @staticmethod
    def _select_legacy_chunks(question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        identifiers = {
            token.casefold()
            for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{1,}(?![A-Za-z0-9])", question)
            if token.casefold() not in {"qbr", "aia", "us", "the", "and"}
        }
        top_content = str(chunks[0].get("content") or "").casefold()
        if len(identifiers) >= 2 and all(identifier in top_content for identifier in identifiers):
            return chunks[:1]
        return chunks[:3]
