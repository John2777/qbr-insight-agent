from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.application.results import PublicResultReader
from packages.qbr_core.application.runtime import (
    answer_deltas as _answer_deltas,
)
from packages.qbr_core.application.runtime import (
    answer_metrics,
    document_vocabulary,
)
from packages.qbr_core.application.warnings import describe_warning
from packages.qbr_core.conversations.context import ConversationContextAssembler
from packages.qbr_core.conversations.summary import ConversationSummaryService
from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.foundation.errors import Conflict, InvalidState, ResourceNotFound
from packages.qbr_core.foundation.identifiers import new_id
from packages.qbr_core.foundation.leases import LeaseCoordinator
from packages.qbr_core.foundation.serialization import _loads
from packages.qbr_core.planning import QueryPlannerAgent
from packages.qbr_core.providers.llm import EvidenceQAAgent
from packages.qbr_core.retrieval.engine import EvidenceRetriever
from packages.qbr_core.skills.registry import SkillDescriptor, SkillRegistry

logger = logging.getLogger(__name__)


class QAApplicationService:
    """Application boundary for conversations, answer runs, and evidence policies."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        retriever: EvidenceRetriever,
        qa_agent: EvidenceQAAgent | None,
        deep_qa_agent: EvidenceQAAgent | None = None,
        skill_registry: SkillRegistry,
        table_reasoning_skill: SkillDescriptor,
        leases: LeaseCoordinator,
        query_planner: QueryPlannerAgent | None = None,
        summary_model: Any | None = None,
    ) -> None:
        """Initialize the QA application service and its workflow dependencies."""
        self.settings = settings
        self.db = db
        self.retriever = retriever
        self.qa_agent = qa_agent
        self.deep_qa_agent = deep_qa_agent or qa_agent
        self.skill_registry = skill_registry
        self.table_reasoning_skill = table_reasoning_skill
        self.leases = leases
        self.query_planner = query_planner or QueryPlannerAgent()
        self.context_assembler = ConversationContextAssembler(
            max_turns=settings.conversation_context_max_turns,
            token_budget=settings.conversation_context_token_budget,
            summary_token_budget=settings.conversation_summary_token_budget,
        )
        self.summary_service = ConversationSummaryService(
            db,
            max_recent_turns=settings.conversation_context_max_turns,
            model=summary_model if settings.conversation_summary_enabled else None,
            provider=settings.llm_provider if summary_model is not None else None,
            model_name=(settings.planner_model or settings.llm_model) if summary_model is not None else None,
        )
        self._run_lock = threading.Lock()
        self.result_reader = PublicResultReader(db)
        self.answer_engine = DeterministicAnswerEngine(
            db=db,
            retriever=retriever,
            skill_registry=skill_registry,
            table_reasoning_skill=table_reasoning_skill,
        )

    def _lease_expiry(self) -> str:
        """Return the expiry timestamp for a newly claimed answer run."""
        return self.leases.expiry()

    def _lease_heartbeat(self, table: str, item_id: str, owner: str):
        """Create a heartbeat context for an owned answer run."""
        return self.leases.heartbeat(table, item_id, owner)

    def create_conversation(
        self,
        workspace_id: str,
        user_id: str,
        document_ids: list[str] | None = None,
        title: str = "New QBR conversation",
    ) -> dict[str, Any]:
        """Create a user-scoped conversation with an optional document scope."""
        document_ids = document_ids or []
        with self.db.transaction(immediate=True) as conn:
            if document_ids:
                placeholders = ",".join("?" for _ in document_ids)
                count = conn.execute(
                    f"SELECT count(*) FROM documents WHERE workspace_id=? AND deleted_at IS NULL AND id IN ({placeholders})",
                    (workspace_id, *document_ids),
                ).fetchone()[0]
                if count != len(set(document_ids)):
                    raise ResourceNotFound("One or more scoped documents were not found")
            conversation_id = new_id("conv")
            now = utc_now()
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                (conversation_id, workspace_id, user_id, title[:200], self.db.json({"document_ids": document_ids}), now, now),
            )
        return self.get_conversation(conversation_id, workspace_id, user_id)

    def get_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Return a user-scoped conversation and its public artifacts."""
        return self.result_reader.get_conversation(conversation_id, workspace_id, user_id)

    def list_conversations(
        self,
        workspace_id: str,
        user_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return the user's conversations ordered by recent activity."""
        return self.result_reader.list_conversations(workspace_id, user_id, limit)

    def delete_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> None:
        """Delete a user's completed conversation and all of its dependent records."""
        with self.db.transaction(immediate=True) as conn:
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE id=? AND workspace_id=? AND user_id=?",
                (conversation_id, workspace_id, user_id),
            ).fetchone()
            if not conversation:
                raise ResourceNotFound("Conversation not found")

            active_run = conn.execute(
                "SELECT id FROM runs WHERE conversation_id=? AND status IN ('pending','running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active_run:
                raise Conflict("Conversation is still generating an answer")

            message_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM messages WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchall()
            ]
            run_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM runs WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchall()
            ]

            def delete_related(table: str, column: str, ids: list[str]) -> int:
                if not ids:
                    return 0
                placeholders = ",".join("?" for _ in ids)
                return max(
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                        ids,
                    ).rowcount,
                    0,
                )

            deleted = {
                "feedback": delete_related("feedback", "message_id", message_ids),
                "citations": delete_related("citations", "message_id", message_ids),
                "run_events": delete_related("run_events", "run_id", run_ids),
                "runs": delete_related("runs", "id", run_ids),
                "messages": delete_related("messages", "id", message_ids),
            }
            conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            self.db.audit(
                conn,
                workspace_id,
                user_id,
                "conversation.delete",
                "conversation",
                conversation_id,
                {"deleted": deleted},
            )

    def ask(
        self,
        conversation_id: str,
        content: str,
        workspace_id: str,
        user_id: str,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a question and return immediately; a worker completes the run."""
        content = content.strip()
        if not content:
            raise Conflict("Question cannot be empty")
        self.get_conversation(conversation_id, workspace_id, user_id)
        now = utc_now()
        user_message_id = new_id("msg")
        assistant_message_id = new_id("msg")
        run_id = new_id("run")
        with self.db.transaction(immediate=True) as conn:
            if client_message_id:
                existing = conn.execute(
                    """SELECT id,assistant_message_id FROM runs
                       WHERE conversation_id=? AND client_message_id=?""",
                    (conversation_id, client_message_id),
                ).fetchone()
                if existing:
                    return {
                        "user_message_id": None,
                        "assistant_message_id": existing["assistant_message_id"],
                        "run_id": existing["id"],
                        "events_url": f"/api/v1/runs/{existing['id']}/events",
                        "reused": True,
                    }
            user_sequence = int(
                conn.execute(
                    "SELECT coalesce(max(sequence_no),0)+1 FROM messages WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            assistant_sequence = user_sequence + 1
            conn.execute(
                """INSERT INTO messages(
                     id,conversation_id,role,content,status,run_id,metadata_json,sequence_no,created_at
                   ) VALUES (?,?,?,?,?,NULL,'{}',?,?)""",
                (user_message_id, conversation_id, "user", content, "completed", user_sequence, now),
            )
            conn.execute(
                """INSERT INTO messages(
                     id,conversation_id,role,content,status,run_id,metadata_json,sequence_no,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (assistant_message_id, conversation_id, "assistant", "", "running", run_id, "{}", assistant_sequence, now),
            )
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            conn.execute(
                """INSERT INTO runs(
                     id,workspace_id,conversation_id,user_message_id,assistant_message_id,context_cutoff_sequence,
                     status,warning_json,model_json,created_at,completed_at,client_message_id
                   ) VALUES (?,?,?,?,?,?,?,'[]','{}',?,NULL,?)""",
                (
                    run_id,
                    workspace_id,
                    conversation_id,
                    user_message_id,
                    assistant_message_id,
                    user_sequence,
                    "pending",
                    now,
                    client_message_id,
                ),
            )
            self.context_assembler.load_or_create(
                conn,
                run_id=run_id,
                conversation_id=conversation_id,
                current_user_message_id=user_message_id,
                cutoff_sequence=user_sequence,
                created_at=now,
            )
            self._run_event(conn, run_id, "queued", {"run_id": run_id})
        return {
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "run_id": run_id,
            "events_url": f"/api/v1/runs/{run_id}/events",
            "reused": False,
        }

    def process_next_run(self, worker_id: str = "worker-local") -> str | None:
        """Claim and execute the next eligible answer run."""
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            now = utc_now()
            with self.db.transaction(immediate=True) as conn:
                run = conn.execute(
                    """SELECT * FROM runs
                       WHERE attempts < ? AND (status='pending' OR (status='running' AND lease_expires_at < ?))
                       ORDER BY created_at,rowid LIMIT 1""",
                    (self.settings.job_max_attempts, now),
                ).fetchone()
                if not run:
                    return None
                updated = conn.execute(
                    """UPDATE runs SET status='running',started_at=coalesce(started_at,?),attempts=attempts+1,
                         lease_owner=?,lease_expires_at=?,error_detail=NULL
                       WHERE id=? AND (status='pending' OR lease_expires_at < ?)""",
                    (now, worker_id, self._lease_expiry(), run["id"], now),
                ).rowcount
                if not updated:
                    return None
                self._run_event(conn, run["id"], "run_started", {"run_id": run["id"]})
            with self._lease_heartbeat("runs", str(run["id"]), worker_id):
                self.process_run(str(run["id"]))
            return str(run["id"])
        finally:
            self._run_lock.release()

    def process_run(self, run_id: str) -> None:
        """Execute one answer-generation run through completion."""
        with self.db.transaction(immediate=True) as conn:
            run = conn.execute(
                """SELECT r.*,c.scope_json,c.user_id,m.sequence_no assistant_sequence
                   FROM runs r JOIN conversations c ON c.id=r.conversation_id
                   JOIN messages m ON m.id=r.assistant_message_id WHERE r.id=?""",
                (run_id,),
            ).fetchone()
            if not run:
                raise ResourceNotFound("Run not found")
            current_user_message_id = run["user_message_id"]
            if not current_user_message_id:
                legacy_user = conn.execute(
                    """SELECT id,sequence_no FROM messages
                       WHERE conversation_id=? AND role='user' AND sequence_no<?
                       ORDER BY sequence_no DESC LIMIT 1""",
                    (run["conversation_id"], run["assistant_sequence"]),
                ).fetchone()
                if not legacy_user:
                    raise InvalidState("Run has no user question")
                current_user_message_id = str(legacy_user["id"])
                cutoff_sequence = int(legacy_user["sequence_no"])
                conn.execute(
                    "UPDATE runs SET user_message_id=?,context_cutoff_sequence=? WHERE id=?",
                    (current_user_message_id, cutoff_sequence, run_id),
                )
            else:
                cutoff_sequence = int(run["context_cutoff_sequence"] or 0)
            question_row = conn.execute(
                """SELECT content,sequence_no FROM messages
                   WHERE id=? AND conversation_id=? AND role='user'""",
                (current_user_message_id, run["conversation_id"]),
            ).fetchone()
            if not question_row:
                raise InvalidState("Run has no user question")
            if cutoff_sequence < 1:
                cutoff_sequence = int(question_row["sequence_no"])
                conn.execute("UPDATE runs SET context_cutoff_sequence=? WHERE id=?", (cutoff_sequence, run_id))
            context = self.context_assembler.load_or_create(
                conn,
                run_id=run_id,
                conversation_id=str(run["conversation_id"]),
                current_user_message_id=str(current_user_message_id),
                cutoff_sequence=cutoff_sequence,
                created_at=str(run["created_at"]),
            )
        if not question_row:
            raise InvalidState("Run has no user question")
        question = str(question_row["content"])
        scope = _loads(run["scope_json"], {})
        document_ids = list(scope.get("document_ids", []))
        history = context.history_list()
        try:
            with self.db.transaction(immediate=True) as conn:
                self._run_event(
                    conn,
                    run_id,
                    "status",
                    {"node": "query_planning", "message": "Understanding the question and building a retrieval plan"},
                )
            plan = self.query_planner.plan(
                question,
                history=history,
                conversation_summary=context.summary,
                document_ids=document_ids,
                document_vocabulary=document_vocabulary(self.db, str(run["workspace_id"]), document_ids),
                run_id=run_id,
            )
            with self.db.transaction(immediate=True) as conn:
                self._run_event(
                    conn,
                    run_id,
                    "query_plan",
                    {
                        "task_summary": plan.task_summary,
                        "answer_brief": plan.answer_brief,
                        "operations": list(plan.operations),
                        "evidence_requirements": list(plan.evidence_requirements),
                        "planner_confidence": plan.planner_confidence,
                        "profile": plan.execution_profile,
                        "planner": plan.planner,
                        "queries": [item.to_dict() for item in plan.retrieval_queries],
                    },
                )
                self._run_event(
                    conn,
                    run_id,
                    "status",
                    {"node": "retrieval", "message": "Searching multiple sources and selecting business evidence"},
                )
            answer_result = self.answer_engine.answer_result(
                question,
                run["workspace_id"],
                document_ids,
                plan=plan,
            )
            answer = answer_result.answer
            evidence = answer_result.evidence
            warnings = answer_result.warnings
            model_info: dict[str, Any] = {
                "provider": self.settings.llm_provider if self.settings.llm_enabled else None,
                "model": self.settings.llm_model if self.settings.llm_enabled else None,
                "status": "disabled" if not self.settings.llm_enabled else "pending",
                "thinking": "disabled",
                "planner": plan.planner,
                "answer_source": "safe_fallback" if not self.settings.llm_enabled else "pending",
            }
            verification_info: dict[str, Any] = {"disposition": "not_run"}
            selected_agent = self.deep_qa_agent if plan.execution_profile == "deep" else self.qa_agent
            if selected_agent:
                with self.db.transaction(immediate=True) as conn:
                    self._run_event(conn, run_id, "status", {"node": "answer_generation", "message": "Generating an evidence-based answer"})
                generated = selected_agent.answer(
                    question=question,
                    grounding_context=answer_result.grounding_context or answer,
                    safe_fallback=answer,
                    evidence=evidence,
                    history=history,
                    conversation_summary=context.summary,
                    task_frame=plan.to_dict(),
                    run_id=run_id,
                )
                answer = generated.answer
                warnings = list(dict.fromkeys([*warnings, *generated.warnings]))
                model_info = {**generated.model, "planner": plan.planner}
                verification_info = generated.diagnostics
            message_metadata = {
                "show_visuals": plan.needs_visuals,
                "knowledge_source": "document_evidence",
                "pipeline_version": "semantic-task-frame-v3-polished",
                "query_plan": plan.to_dict(),
                "answer_routing": answer_result.diagnostics.get("answer_routing", {}),
                "retrieval": answer_result.diagnostics.get("retrieval", {}),
                "evidence_pack": answer_result.diagnostics.get("evidence_pack", {}),
                "verified_calculation": answer_result.diagnostics.get("verified_calculation"),
                "verified_calculation_facts": answer_result.diagnostics.get("verified_calculation_facts", []),
                "verified_calculation_kind": answer_result.diagnostics.get("verified_calculation_kind"),
                "verified_calculation_scope": answer_result.diagnostics.get("verified_calculation_scope"),
                "chart_scope": answer_result.diagnostics.get("chart_scope"),
                "verification": verification_info,
                "conversation_context": {
                    **context.diagnostics,
                    "original_question": question,
                    "canonical_question": plan.canonical_question,
                },
            }
            self._complete_run(run, answer, evidence, warnings, model_info, message_metadata)
            if self.settings.conversation_summary_enabled:
                self.summary_service.refresh_safely(str(run["conversation_id"]), run_id=run_id)
        except Exception as exc:
            with self.db.transaction(immediate=True) as conn:
                current = conn.execute("SELECT attempts FROM runs WHERE id=?", (run_id,)).fetchone()
                retrying = bool(current and int(current["attempts"]) < self.settings.job_max_attempts)
                conn.execute(
                    """UPDATE runs SET status=?,error_detail=?,lease_owner=NULL,lease_expires_at=NULL,
                         completed_at=? WHERE id=?""",
                    ("pending" if retrying else "failed", str(exc)[:1000], None if retrying else utc_now(), run_id),
                )
                if not retrying:
                    conn.execute(
                        "UPDATE messages SET status='failed',content=? WHERE id=?",
                        ("Answer generation failed. Try again later.", run["assistant_message_id"]),
                    )
                self._run_event(
                    conn,
                    run_id,
                    "warning" if retrying else "error",
                    {"code": "RUN_RETRY" if retrying else "RUN_FAILED", "detail": type(exc).__name__},
                )
            raise

    def _complete_run(
        self,
        run: sqlite3.Row,
        answer: str,
        evidence: list[dict[str, Any]],
        warnings: list[str],
        model_info: dict[str, Any],
        message_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist the final answer, citations, events, warnings, and model metadata."""
        run_id = str(run["id"])
        completed_at = utc_now()
        metadata = dict(message_metadata or {})
        query_plan = metadata.get("query_plan") if isinstance(metadata.get("query_plan"), dict) else {}
        planner_diagnostics = (
            query_plan.get("diagnostics") if isinstance(query_plan.get("diagnostics"), dict) else {}
        )
        metadata["answer_metrics"] = answer_metrics(
            created_at=str(run["created_at"]),
            completed_at=completed_at,
            planner_diagnostics=planner_diagnostics,
            answer_model=model_info,
        )
        if warnings:
            severity_rank = {"info": 0, "warning": 1, "degraded": 2, "error": 3}
            max_severity = max((describe_warning(code).severity for code in warnings), key=severity_rank.__getitem__)
            log = logger.info if max_severity == "info" else logger.warning
            log(
                json.dumps(
                    {
                        "event": "answer_run_completed_with_signals",
                        "run_id": run_id,
                        "warning_codes": list(dict.fromkeys(warnings)),
                        "max_severity": max_severity,
                        "model_status": model_info.get("status"),
                        "answer_source": model_info.get("answer_source"),
                        "verification_disposition": metadata.get("verification", {}).get("disposition"),
                        "planner": model_info.get("planner"),
                    },
                    separators=(",", ":"),
                )
            )
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET content=?,status='completed',metadata_json=? WHERE id=?",
                (answer, self.db.json(metadata), run["assistant_message_id"]),
            )
            for index, evidence_item in enumerate(evidence, 1):
                citation_id = new_id("cit")
                conn.execute(
                    "INSERT INTO citations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        citation_id,
                        run["assistant_message_id"],
                        index,
                        evidence_item["document_version_id"],
                        evidence_item["slide_id"],
                        evidence_item.get("element_id"),
                        evidence_item.get("chunk_id"),
                        evidence_item["quote"],
                        self.db.json(evidence_item.get("bbox", {})),
                        evidence_item["confidence"],
                        evidence_item["source_kind"],
                    ),
                )
                self._run_event(conn, run_id, "citation", {"id": citation_id, "label": f"[{index}]"})
            for piece in _answer_deltas(answer):
                self._run_event(conn, run_id, "answer_delta", {"delta": piece})
            for warning in warnings:
                self._run_event(conn, run_id, "warning", {"message": warning})
            self._run_event(conn, run_id, "model", model_info)
            conn.execute(
                """UPDATE runs SET status='completed',warning_json=?,model_json=?,completed_at=?,
                     lease_owner=NULL,lease_expires_at=NULL WHERE id=?""",
                (self.db.json(warnings), self.db.json(model_info), completed_at, run_id),
            )
            self._run_event(conn, run_id, "completed", {"message_id": run["assistant_message_id"]})

    def _run_event(self, conn: sqlite3.Connection, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Persist one ordered event for an answer run."""
        conn.execute(
            "INSERT INTO run_events(run_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
            (run_id, event_type, self.db.json(data), utc_now()),
        )

    def _answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Return the deterministic answer tuple used by compatible callers."""
        return self.answer_engine.answer(question, workspace_id, document_ids)

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        """Return a deterministic structured-table reasoning result."""
        return self.answer_engine._table_reasoning_answer(question, sources)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        """Return a workspace-scoped answer run and its public artifacts."""
        return self.result_reader.get_run(run_id, workspace_id)

    def run_events(self, run_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return ordered events emitted after the supplied event identifier."""
        return self.result_reader.run_events(run_id, workspace_id, after)

    def _citation_public(self, item: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
        """Build the public citation representation used by legacy callers."""
        return self.result_reader.citation(item, conn)

    def add_feedback(
        self,
        message_id: str,
        workspace_id: str,
        user_id: str,
        rating: int,
        category: str | None,
        comment: str | None,
    ) -> dict[str, Any]:
        """Persist user feedback for an assistant message."""
        if rating not in {-1, 1}:
            raise Conflict("rating must be -1 or 1")
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE m.id=? AND c.workspace_id=? AND c.user_id=?
                """,
                (message_id, workspace_id, user_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Message not found")
            feedback_id = new_id("feedback")
            conn.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?,?,?)",
                (feedback_id, message_id, user_id, rating, category, comment, utc_now()),
            )
        return {"id": feedback_id, "rating": rating}
