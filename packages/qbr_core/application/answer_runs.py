"""Answer-run orchestration independent from the public application facade."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.application.contracts import RunMetadata, RunResult
from packages.qbr_core.application.run_store import RunEventStore, RunRepository
from packages.qbr_core.application.runtime import document_vocabulary
from packages.qbr_core.conversations.summary import ConversationSummaryService
from packages.qbr_core.foundation.database import Database
from packages.qbr_core.foundation.settings_domains import ConversationSettings, ModelSettings, WorkerSettings
from packages.qbr_core.planning import QueryPlannerAgent, RetrievalQuery
from packages.qbr_core.providers.llm import EvidenceQAAgent


class AnswerRunExecutor:
    """Coordinate planning, retrieval, generation, and run persistence."""

    def __init__(
        self,
        *,
        models: ModelSettings,
        conversation: ConversationSettings,
        workers: WorkerSettings,
        db: Database,
        repository: RunRepository,
        events: RunEventStore,
        query_planner: QueryPlannerAgent,
        answer_engine: DeterministicAnswerEngine,
        qa_agent: EvidenceQAAgent | None,
        deep_qa_agent: EvidenceQAAgent | None,
        summary_service: ConversationSummaryService,
    ) -> None:
        """Initialize the executor with explicit workflow collaborators."""
        self._models = models
        self._conversation = conversation
        self._workers = workers
        self._db = db
        self._repository = repository
        self._events = events
        self._query_planner = query_planner
        self._answer_engine = answer_engine
        self._qa_agent = qa_agent
        self._deep_qa_agent = deep_qa_agent or qa_agent
        self._summary_service = summary_service

    def execute(self, run_id: str) -> None:
        """Execute one answer run and persist its terminal or retry state."""
        prepared = self._repository.prepare(run_id)
        run = prepared.row
        question = prepared.question
        document_ids = list(prepared.document_ids)
        context = prepared.context
        history = context.history_list()
        try:
            self._emit_status(
                run_id,
                "query_planning",
                "Understanding the question and building a retrieval plan",
            )
            plan = self._query_planner.plan(
                question,
                history=history,
                conversation_summary=context.summary,
                document_ids=document_ids,
                document_vocabulary=document_vocabulary(self._db, str(run["workspace_id"]), document_ids),
                run_id=run_id,
            )
            with self._db.transaction(immediate=True) as conn:
                self._events.append(
                    conn,
                    run_id,
                    "query_plan",
                    {
                        "task_summary": plan.task_summary,
                        "answer_brief": plan.answer_brief,
                        "operations": list(plan.operations),
                        "delivery_requirements": list(plan.delivery_requirements),
                        "evidence_requirements": list(plan.evidence_requirements),
                        "planner_confidence": plan.planner_confidence,
                        "profile": plan.execution_profile,
                        "planner": plan.planner,
                        "queries": [item.to_dict() for item in plan.retrieval_queries],
                    },
                )
                self._events.append(
                    conn,
                    run_id,
                    "status",
                    {"node": "retrieval", "message": "Searching multiple sources and selecting business evidence"},
                )
            answer_result = self._answer_engine.answer_result(
                question,
                str(run["workspace_id"]),
                document_ids,
                plan=plan,
            )
            answer = answer_result.answer
            evidence = answer_result.evidence
            warnings = answer_result.warnings
            model_info: dict[str, Any] = {
                "provider": self._models.provider if self._models.enabled else None,
                "model": self._models.model if self._models.enabled else None,
                "status": "disabled" if not self._models.enabled else "pending",
                "thinking": "disabled",
                "planner": plan.planner,
                "answer_source": "safe_fallback" if not self._models.enabled else "pending",
            }
            verification_info: dict[str, Any] = {"disposition": "not_run"}
            selected_agent = self._deep_qa_agent if plan.execution_profile == "deep" else self._qa_agent
            if selected_agent:
                self._emit_status(run_id, "answer_generation", "Generating an evidence-based answer")
                generated = selected_agent.answer(
                    question=question,
                    grounding_context=answer_result.grounding_context or answer,
                    safe_fallback=answer,
                    evidence=evidence,
                    history=history,
                    conversation_summary=context.summary,
                    run_id=run_id,
                )
                answer = generated.answer
                warnings = list(dict.fromkeys([*warnings, *generated.warnings]))
                model_info = {**generated.model, "planner": plan.planner}
                adequacy = selected_agent.assess_adequacy(
                    question=question,
                    answer=answer,
                    evidence=evidence,
                    run_id=run_id,
                )
                verification_info = {**generated.diagnostics, "adequacy": adequacy.diagnostics}
                if adequacy.gap_query:
                    self._emit_status(run_id, "retrieval", "Filling an answer coverage gap against the original question")
                    gap_query = RetrievalQuery(
                        f"q{len(plan.retrieval_queries) + 1}",
                        adequacy.gap_query,
                        "answer_gap",
                        1.15,
                    )
                    retry_plan = replace(plan, retrieval_queries=(*plan.retrieval_queries, gap_query))
                    retry_result = self._answer_engine.answer_result(
                        question,
                        str(run["workspace_id"]),
                        document_ids,
                        plan=retry_plan,
                    )
                    previous_evidence = {
                        (
                            str(item.get("slide_id") or ""),
                            str(item.get("element_id") or item.get("chunk_id") or ""),
                            str(item.get("quote") or ""),
                        )
                        for item in evidence
                    }
                    retry_evidence = {
                        (
                            str(item.get("slide_id") or ""),
                            str(item.get("element_id") or item.get("chunk_id") or ""),
                            str(item.get("quote") or ""),
                        )
                        for item in retry_result.evidence
                    }
                    if retry_evidence != previous_evidence:
                        regenerated = selected_agent.answer(
                            question=question,
                            grounding_context=retry_result.grounding_context or retry_result.answer,
                            safe_fallback=retry_result.answer,
                            evidence=retry_result.evidence,
                            history=history,
                            conversation_summary=context.summary,
                            run_id=run_id,
                        )
                        final_adequacy = selected_agent.assess_adequacy(
                            question=question,
                            answer=regenerated.answer,
                            evidence=retry_result.evidence,
                            run_id=run_id,
                        )
                        answer_result = retry_result
                        answer = regenerated.answer
                        evidence = retry_result.evidence
                        warnings = list(dict.fromkeys([*retry_result.warnings, *regenerated.warnings]))
                        model_info = {**regenerated.model, "planner": retry_plan.planner}
                        verification_info = {
                            **regenerated.diagnostics,
                            "adequacy": {
                                **final_adequacy.diagnostics,
                                "retried": True,
                                "initial_gap_query": adequacy.gap_query,
                            },
                        }
                        plan = retry_plan
                    else:
                        verification_info["adequacy"] = {
                            **adequacy.diagnostics,
                            "retried": True,
                            "new_evidence": False,
                        }
            metadata = self._message_metadata(
                question=question,
                plan=plan,
                diagnostics=answer_result.diagnostics,
                verification=verification_info,
                context_diagnostics=context.diagnostics,
            )
            result = RunResult.create(
                answer=answer,
                evidence=evidence,
                warnings=warnings,
                model=model_info,
                metadata=metadata,
            )
            self._repository.complete(prepared, result)
            if self._conversation.summary_enabled:
                self._summary_service.refresh_safely(str(run["conversation_id"]), run_id=run_id)
        except Exception as exc:
            self._repository.fail(prepared, exc, max_attempts=self._workers.max_attempts)
            raise

    def _emit_status(self, run_id: str, node: str, message: str) -> None:
        """Persist one progress status in its own short transaction."""
        with self._db.transaction(immediate=True) as conn:
            self._events.append(conn, run_id, "status", {"node": node, "message": message})

    @staticmethod
    def _message_metadata(
        *,
        question: str,
        plan: Any,
        diagnostics: dict[str, Any],
        verification: dict[str, Any],
        context_diagnostics: dict[str, Any],
    ) -> RunMetadata:
        """Build the auditable message metadata emitted by the answer pipeline."""
        return RunMetadata(
            show_visuals=plan.needs_visuals,
            query_plan=plan.to_dict(),
            answer_routing=dict(diagnostics.get("answer_routing", {})),
            retrieval=dict(diagnostics.get("retrieval", {})),
            evidence_pack=dict(diagnostics.get("evidence_pack", {})),
            calculation={
                "verified_calculation": diagnostics.get("verified_calculation"),
                "verified_calculation_facts": diagnostics.get("verified_calculation_facts", []),
                "verified_calculation_kind": diagnostics.get("verified_calculation_kind"),
                "verified_calculation_scope": diagnostics.get("verified_calculation_scope"),
                "chart_scope": diagnostics.get("chart_scope"),
            },
            verification=verification,
            conversation_context={
                **context_diagnostics,
                "original_question": question,
                "canonical_question": plan.canonical_question,
            },
        )
