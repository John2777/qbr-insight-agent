from __future__ import annotations

import logging
import threading
from typing import Any

from packages.qbr_core.application.ingestion import IngestionService
from packages.qbr_core.application.persistence import ParsedPersistenceService
from packages.qbr_core.application.purge import DocumentPurgeService
from packages.qbr_core.application.qa_service import QAApplicationService
from packages.qbr_core.application.resources import ResourceService
from packages.qbr_core.documents.vision import create_vision_enricher
from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.foundation.database import Database
from packages.qbr_core.foundation.leases import LeaseCoordinator, LeasePolicy
from packages.qbr_core.planning import QueryPlannerAgent
from packages.qbr_core.providers.llm import EvidenceQAAgent, build_chat_model
from packages.qbr_core.retrieval.engine import EvidenceRetriever
from packages.qbr_core.retrieval.reranking import create_reranker
from packages.qbr_core.retrieval.vector_store import create_vector_store
from packages.qbr_core.security.archive import OOXML_MIME
from packages.qbr_core.skills.registry import (
    NATIVE_CHART_CAPABILITY,
    PARSER_SKILL_KIND,
    REASONING_SKILL_KIND,
    STRUCTURED_TABLE_REASONING_CAPABILITY,
    SkillRegistry,
    default_skill_paths,
)

logger = logging.getLogger(__name__)


class QBRService:
    """Compose and expose the framework-independent QBR application facade."""
    _COMPONENT_NAMES = ("ingestion", "persistence", "resources")

    def __init__(self, settings: Settings) -> None:
        """Initialize the QBR composition root and its application dependencies."""
        self.settings = settings
        settings.ensure_directories()
        self.skill_registry = SkillRegistry(settings.skill_paths or default_skill_paths())
        self.parser_skill = self.skill_registry.resolve(
            kind=PARSER_SKILL_KIND,
            name=settings.parser_skill_name or None,
            capability=NATIVE_CHART_CAPABILITY,
            accepts=OOXML_MIME,
        )
        self.table_reasoning_skill = self.skill_registry.resolve(
            kind=REASONING_SKILL_KIND,
            name=settings.table_reasoning_skill_name or None,
            capability=STRUCTURED_TABLE_REASONING_CAPABILITY,
            accepts="application/x-qbr-structured-table",
        )
        self.db = Database(settings.database_path)
        self.db.initialize()
        self._process_lock = threading.Lock()
        self.leases = LeaseCoordinator(
            self.db,
            LeasePolicy(settings.job_lease_seconds, settings.job_heartbeat_seconds),
        )
        vector_store = create_vector_store(self.db, settings)
        reranker = create_reranker(settings)
        self.retriever = EvidenceRetriever(
            self.db,
            mode=settings.retrieval_strategy,
            vector_store=vector_store,
            lexical_candidate_k=settings.lexical_candidate_k,
            vector_candidate_k=settings.vector_candidate_k,
            rrf_k=settings.retrieval_rrf_k,
            lexical_weight=settings.retrieval_lexical_weight,
            vector_weight=settings.retrieval_vector_weight,
            reranker=reranker,
            rerank_candidate_k=settings.rerank_candidate_k,
            rerank_top_n=settings.rerank_top_n,
        )
        self.document_purges = DocumentPurgeService(settings, self.db, self.retriever.rebuild_workspace)
        self.qa_agent = EvidenceQAAgent(settings) if settings.llm_configured else None
        self.deep_qa_agent = (
            EvidenceQAAgent(settings, model_name=settings.deep_llm_model)
            if settings.llm_configured and settings.deep_llm_model and settings.deep_llm_model != settings.llm_model
            else self.qa_agent
        )
        planner_model = None
        if self.qa_agent is not None:
            planner_model = build_chat_model(
                settings,
                settings.planner_model or settings.llm_model,
                # The planner must emit a compact JSON object. Hidden reasoning can
                # consume the entire completion budget before any JSON is emitted
                # on OpenAI-compatible reasoning models (notably Qwen), so keep it
                # disabled for this structured-output role.
                thinking_enabled=False,
            )
        self.query_planner = QueryPlannerAgent(
            planner_model,
            provider=settings.llm_provider if planner_model is not None else None,
            model_name=(settings.planner_model or settings.llm_model) if planner_model is not None else None,
        )
        self.vision_enricher = create_vision_enricher(settings)
        self.qa_service = QAApplicationService(
            settings=settings,
            db=self.db,
            retriever=self.retriever,
            qa_agent=self.qa_agent,
            deep_qa_agent=self.deep_qa_agent,
            skill_registry=self.skill_registry,
            table_reasoning_skill=self.table_reasoning_skill,
            leases=self.leases,
            query_planner=self.query_planner,
            summary_model=planner_model,
        )
        self.ingestion = IngestionService(self)
        self.persistence = ParsedPersistenceService(self)
        self.resources = ResourceService(self)

    def __getattr__(self, name: str) -> Any:
        """Delegate unresolved attributes to the composed root service."""
        for component_name in self._COMPONENT_NAMES:
            component = self.__dict__.get(component_name)
            if component is not None and hasattr(type(component), name):
                return getattr(component, name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        """Include delegated component attributes in interactive discovery."""
        names = set(super().__dir__())
        for component_name in self._COMPONENT_NAMES:
            component = self.__dict__.get(component_name)
            if component is not None:
                names.update(name for name in dir(type(component)) if not name.startswith("__"))
        return sorted(names)

    def health(self) -> dict[str, Any]:
        """Return the current service and dependency health summary."""
        with self.db.read() as conn:
            schema = conn.execute("SELECT max(version) FROM schema_meta").fetchone()[0]
            fts = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
            pending_jobs = conn.execute("SELECT count(*) FROM ingestion_jobs WHERE status='pending'").fetchone()[0]
            pending_runs = conn.execute("SELECT count(*) FROM runs WHERE status='pending'").fetchone()[0]
        return {
            "status": "ready",
            "database": "ok",
            "schema_version": schema,
            "fts5": bool(fts),
            "parser_skill": self.parser_skill.reference,
            "skills": self.skill_registry.status(),
            "queues": {"ingestion_pending": pending_jobs, "answer_pending": pending_runs},
            "auth": {"mode": self.settings.auth_mode, "environment": self.settings.app_env},
            "llm": {
                "enabled": self.settings.llm_enabled,
                "configured": self.settings.llm_configured,
                "provider": self.settings.llm_provider if self.settings.llm_enabled else None,
                "model": self.settings.llm_model if self.settings.llm_enabled else None,
                "planner_model": (self.settings.planner_model or self.settings.llm_model) if self.settings.llm_enabled else None,
                "deep_model": (self.settings.deep_llm_model or self.settings.llm_model) if self.settings.llm_enabled else None,
            },
            "retrieval": {
                "strategy": self.settings.retrieval_strategy,
                "vector_configured": self.settings.vector_configured,
                "vector_available": self.retriever.vector_available,
                "vector_backend": self.retriever.vector_backend,
                "embedding_model": self.settings.embedding_model if self.settings.vector_configured else None,
                "rerank_configured": self.settings.rerank_configured,
                "rerank_available": self.retriever.rerank_available,
                "rerank_model": self.settings.rerank_model if self.settings.rerank_configured else None,
            },
            "vision": {
                "configured": self.settings.vision_configured,
                "model": self.settings.vision_model if self.settings.vision_configured else None,
            },
        }

    # Keep the original facade stable for API, worker, and scripts while the
    # conversation/answer use cases live behind their own application boundary.
    def create_conversation(
        self,
        workspace_id: str,
        user_id: str,
        document_ids: list[str] | None = None,
        title: str = "New QBR conversation",
    ) -> dict[str, Any]:
        """Create a user-scoped conversation with an optional document scope."""
        return self.qa_service.create_conversation(workspace_id, user_id, document_ids, title)

    def get_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Return a user-scoped conversation and its public artifacts."""
        return self.qa_service.get_conversation(conversation_id, workspace_id, user_id)

    def list_conversations(
        self,
        workspace_id: str,
        user_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return the user's conversations ordered by recent activity."""
        return self.qa_service.list_conversations(workspace_id, user_id, limit)

    def delete_conversation(self, conversation_id: str, workspace_id: str, user_id: str) -> None:
        """Delete a completed conversation and its dependent records."""
        self.qa_service.delete_conversation(conversation_id, workspace_id, user_id)

    def ask(
        self,
        conversation_id: str,
        content: str,
        workspace_id: str,
        user_id: str,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue a user question and return identifiers for the answer run."""
        return self.qa_service.ask(conversation_id, content, workspace_id, user_id, client_message_id)

    def process_next_run(self, worker_id: str = "worker-local") -> str | None:
        """Claim and execute the next eligible answer run."""
        return self.qa_service.process_next_run(worker_id)

    def process_run(self, run_id: str) -> None:
        """Execute one answer-generation run through completion."""
        self.qa_service.process_run(run_id)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        """Return a workspace-scoped answer run and its public artifacts."""
        return self.qa_service.get_run(run_id, workspace_id)

    def run_events(self, run_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return ordered events emitted after the supplied event identifier."""
        return self.qa_service.run_events(run_id, workspace_id, after)

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
        return self.qa_service.add_feedback(message_id, workspace_id, user_id, rating, category, comment)

    def _answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Delegate deterministic answer generation to the QA service."""
        return self.qa_service._answer(question, workspace_id, document_ids)

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        """Delegate structured table reasoning to the QA service."""
        return self.qa_service._table_reasoning_answer(question, sources)
