from __future__ import annotations

import sqlite3
import threading
from typing import Any

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.application.answer_runs import AnswerRunExecutor
from packages.qbr_core.application.results import PublicResultReader
from packages.qbr_core.application.run_store import RunEventStore, RunRepository
from packages.qbr_core.application.runtime import answer_deltas as _runtime_answer_deltas
from packages.qbr_core.conversations.context import ConversationContextAssembler
from packages.qbr_core.conversations.summary import ConversationSummaryService
from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.foundation.errors import Conflict, ResourceNotFound
from packages.qbr_core.foundation.identifiers import new_id
from packages.qbr_core.foundation.leases import LeaseCoordinator
from packages.qbr_core.planning import QueryPlannerAgent
from packages.qbr_core.providers.llm import EvidenceQAAgent
from packages.qbr_core.retrieval.engine import EvidenceRetriever
from packages.qbr_core.skills.registry import SkillDescriptor, SkillRegistry


def _answer_deltas(answer: str, chunk_size: int = 48) -> tuple[str, ...]:
    """Return compatibility chunks using the shared runtime implementation."""
    return _runtime_answer_deltas(answer, chunk_size)


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
        conversation_settings = settings.conversation
        model_settings = settings.models
        self.context_assembler = ConversationContextAssembler(
            max_turns=conversation_settings.context_max_turns,
            token_budget=conversation_settings.context_token_budget,
            summary_token_budget=conversation_settings.summary_token_budget,
        )
        self.summary_service = ConversationSummaryService(
            db,
            max_recent_turns=conversation_settings.context_max_turns,
            model=summary_model if conversation_settings.summary_enabled else None,
            provider=model_settings.provider if summary_model is not None else None,
            model_name=(model_settings.planner_model or model_settings.model) if summary_model is not None else None,
        )
        self._run_lock = threading.Lock()
        self.answer_engine = DeterministicAnswerEngine(
            db=db,
            retriever=retriever,
            skill_registry=skill_registry,
            table_reasoning_skill=table_reasoning_skill,
        )
        self.run_events_store = RunEventStore(db)
        self.run_repository = RunRepository(db, self.run_events_store, self.context_assembler)
        self.result_reader = PublicResultReader(db, self.run_events_store)
        self.run_executor = AnswerRunExecutor(
            models=model_settings,
            conversation=conversation_settings,
            workers=settings.workers,
            db=db,
            repository=self.run_repository,
            events=self.run_events_store,
            query_planner=self.query_planner,
            answer_engine=self.answer_engine,
            qa_agent=qa_agent,
            deep_qa_agent=self.deep_qa_agent,
            summary_service=self.summary_service,
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
            self.run_repository.create(
                conn,
                run_id=run_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                cutoff_sequence=user_sequence,
                created_at=now,
                client_message_id=client_message_id,
            )
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
            run_id = self.run_repository.claim_next(
                worker_id=worker_id,
                max_attempts=self.settings.workers.max_attempts,
                lease_expires_at=self._lease_expiry(),
            )
            if not run_id:
                return None
            with self._lease_heartbeat("runs", run_id, worker_id):
                self.process_run(run_id)
            return run_id
        finally:
            self._run_lock.release()

    def process_run(self, run_id: str) -> None:
        """Execute one answer-generation run through completion."""
        self.run_executor.execute(run_id)

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
