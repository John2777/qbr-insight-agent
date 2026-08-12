from __future__ import annotations

import logging
import threading
from pathlib import Path
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

    def __init__(self, settings: Settings) -> None:
        """Initialize the QBR composition root and its application dependencies."""
        self.settings = settings
        settings.ensure_directories()
        storage = settings.storage
        skills = settings.skills
        workers = settings.workers
        models = settings.models
        retrieval = settings.retrieval
        self.skill_registry = SkillRegistry(skills.paths or default_skill_paths())
        self.parser_skill = self.skill_registry.resolve(
            kind=PARSER_SKILL_KIND,
            name=skills.parser_name or None,
            capability=NATIVE_CHART_CAPABILITY,
            accepts=OOXML_MIME,
        )
        self.table_reasoning_skill = self.skill_registry.resolve(
            kind=REASONING_SKILL_KIND,
            name=skills.table_reasoning_name or None,
            capability=STRUCTURED_TABLE_REASONING_CAPABILITY,
            accepts="application/x-qbr-structured-table",
        )
        self.db = Database(storage.database_path)
        self.db.initialize()
        self._process_lock = threading.Lock()
        self.leases = LeaseCoordinator(
            self.db,
            LeasePolicy(workers.lease_seconds, workers.heartbeat_seconds),
        )
        vector_store = create_vector_store(self.db, settings)
        reranker = create_reranker(settings)
        self.retriever = EvidenceRetriever(
            self.db,
            mode=retrieval.strategy,
            vector_store=vector_store,
            lexical_candidate_k=retrieval.lexical_candidate_k,
            vector_candidate_k=retrieval.vector_candidate_k,
            rrf_k=retrieval.rrf_k,
            lexical_weight=retrieval.lexical_weight,
            vector_weight=retrieval.vector_weight,
            reranker=reranker,
            rerank_candidate_k=retrieval.rerank_candidate_k,
            rerank_top_n=retrieval.rerank_top_n,
        )
        self.document_purges = DocumentPurgeService(settings, self.db, self.retriever.rebuild_workspace)
        self.qa_agent = EvidenceQAAgent(settings) if models.configured else None
        self.deep_qa_agent = (
            EvidenceQAAgent(settings, model_name=models.deep_model)
            if models.configured and models.deep_model and models.deep_model != models.model
            else self.qa_agent
        )
        planner_model = None
        if self.qa_agent is not None:
            planner_model = build_chat_model(
                settings,
                models.planner_model or models.model,
                # The planner must emit a compact JSON object. Hidden reasoning can
                # consume the entire completion budget before any JSON is emitted
                # on OpenAI-compatible reasoning models (notably Qwen), so keep it
                # disabled for this structured-output role.
                thinking_enabled=False,
            )
        self.query_planner = QueryPlannerAgent(
            planner_model,
            provider=models.provider if planner_model is not None else None,
            model_name=(models.planner_model or models.model) if planner_model is not None else None,
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
        self.persistence = ParsedPersistenceService(
            db=self.db,
            parser_skill=self.parser_skill,
            retriever=self.retriever,
        )
        self.ingestion = IngestionService(
            settings=self.settings,
            db=self.db,
            process_lock=self._process_lock,
            leases=self.leases,
            document_purges=self.document_purges,
            parser_skill=self.parser_skill,
            skill_registry=self.skill_registry,
            vision_enricher=self.vision_enricher,
            persistence=self.persistence,
        )
        self.resources = ResourceService(
            db=self.db,
            document_purges=self.document_purges,
            retriever=self.retriever,
            persistence=self.persistence,
        )

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
                "answer_polishing_enabled": self.settings.answer_polishing_enabled,
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

    def import_document(
        self,
        temp_path: Path,
        *,
        filename: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        deduplication: str,
        workspace_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Queue a presentation through the explicit ingestion boundary."""
        return self.ingestion.import_document(
            temp_path,
            filename=filename,
            title=title,
            metadata=metadata,
            deduplication=deduplication,
            workspace_id=workspace_id,
            user_id=user_id,
        )

    def process_next_job(self, worker_id: str = "worker-local") -> str | None:
        """Claim and execute the next ingestion job."""
        return self.ingestion.process_next_job(worker_id)

    def process_job(self, job_id: str, *, worker_id: str = "worker-local") -> None:
        """Execute a specific ingestion job."""
        self.ingestion.process_job(job_id, worker_id=worker_id)

    def list_documents(self, workspace_id: str) -> list[dict[str, Any]]:
        """Return active documents visible to a workspace."""
        return self.resources.list_documents(workspace_id)

    def get_document(self, document_id: str, workspace_id: str) -> dict[str, Any]:
        """Return one workspace-scoped document."""
        return self.resources.get_document(document_id, workspace_id)

    def list_slides(self, version_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """Return slides for a workspace-scoped document version."""
        return self.resources.list_slides(version_id, workspace_id)

    def get_slide(self, slide_id: str, workspace_id: str) -> dict[str, Any]:
        """Return one workspace-scoped slide."""
        return self.resources.get_slide(slide_id, workspace_id)

    def preview_path(self, slide_id: str, workspace_id: str) -> Path:
        """Return the authorized slide preview path."""
        return self.resources.preview_path(slide_id, workspace_id)

    def thumbnail_path(self, slide_id: str, workspace_id: str) -> Path:
        """Return the authorized slide thumbnail path."""
        return self.resources.thumbnail_path(slide_id, workspace_id)

    def get_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        """Return one workspace-scoped ingestion job."""
        return self.resources.get_job(job_id, workspace_id)

    def job_events(self, job_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return ordered ingestion events after a cursor."""
        return self.resources.job_events(job_id, workspace_id, after)

    def cancel_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        """Cancel a workspace-scoped ingestion job."""
        return self.resources.cancel_job(job_id, workspace_id)

    def retry_job(self, job_id: str, workspace_id: str) -> dict[str, Any]:
        """Retry a workspace-scoped ingestion job."""
        return self.resources.retry_job(job_id, workspace_id)

    def delete_document(self, document_id: str, workspace_id: str, user_id: str) -> None:
        """Soft-delete a workspace document."""
        self.resources.delete_document(document_id, workspace_id, user_id)

    def purge_document(self, document_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Permanently purge a workspace document."""
        return self.resources.purge_document(document_id, workspace_id, user_id)

    def list_reviews(self, workspace_id: str) -> list[dict[str, Any]]:
        """Return review tasks for a workspace."""
        return self.resources.list_reviews(workspace_id)

    def claim_review(self, review_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Claim a review task for a workspace user."""
        return self.resources.claim_review(review_id, workspace_id, user_id)

    def resolve_review(
        self,
        review_id: str,
        workspace_id: str,
        user_id: str,
        corrected: dict[str, Any] | None,
        resolution: str = "resolved",
    ) -> dict[str, Any]:
        """Resolve or dismiss a chart review task."""
        return self.resources.resolve_review(review_id, workspace_id, user_id, corrected, resolution)

    def analytics_summary(self, workspace_id: str) -> dict[str, Any]:
        """Return aggregate workspace analytics."""
        return self.resources.analytics_summary(workspace_id)

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

    def _series_axis_metadata(self, chart: dict[str, Any], series: dict[str, Any]) -> dict[str, Any]:
        """Resolve chart axis metadata through the explicitly injected persistence service."""
        return self.persistence._series_axis_metadata(chart, series)

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
