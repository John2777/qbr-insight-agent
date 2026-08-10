from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from typing import Any

from .answering import DeterministicAnswerEngine
from .config import Settings
from .db import Database, utc_now
from .errors import Conflict, InvalidState, ResourceNotFound
from .ids import new_id
from .lease import LeaseCoordinator
from .llm import EvidenceQAAgent
from .query_planning import QueryPlannerAgent
from .retrieval import EvidenceRetriever
from .run_warnings import describe_warning
from .skill_registry import SkillDescriptor, SkillRegistry

logger = logging.getLogger(__name__)


def _answer_deltas(answer: str, chunk_size: int = 48) -> tuple[str, ...]:
    """Split an answer without dropping long runs of non-whitespace text."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    return tuple(answer[offset : offset + chunk_size] for offset in range(0, len(answer), chunk_size)) or ("",)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


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
    ) -> None:
        self.settings = settings
        self.db = db
        self.retriever = retriever
        self.qa_agent = qa_agent
        self.deep_qa_agent = deep_qa_agent or qa_agent
        self.skill_registry = skill_registry
        self.table_reasoning_skill = table_reasoning_skill
        self.leases = leases
        self.query_planner = query_planner or QueryPlannerAgent()
        self._run_lock = threading.Lock()
        self.answer_engine = DeterministicAnswerEngine(
            db=db,
            retriever=retriever,
            skill_registry=skill_registry,
            table_reasoning_skill=table_reasoning_skill,
        )

    def _lease_expiry(self) -> str:
        return self.leases.expiry()

    def _lease_heartbeat(self, table: str, item_id: str, owner: str):
        return self.leases.heartbeat(table, item_id, owner)

    def create_conversation(
        self,
        workspace_id: str,
        user_id: str,
        document_ids: list[str] | None = None,
        title: str = "New QBR conversation",
    ) -> dict[str, Any]:
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
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=? AND workspace_id=? AND user_id=?",
                (conversation_id, workspace_id, user_id),
            ).fetchone()
            if not row:
                raise ResourceNotFound("Conversation not found")
            messages = conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()
            message_data = []
            for message in messages:
                item = dict(message)
                item["metadata"] = _loads(item.pop("metadata_json", None), {})
                citations = conn.execute("SELECT * FROM citations WHERE message_id=? ORDER BY claim_no", (message["id"],)).fetchall()
                item["citations"] = [self._citation_public(dict(citation), conn) for citation in citations]
                message_data.append(item)
        result = dict(row)
        result["scope"] = _loads(result.pop("scope_json"), {})
        result["messages"] = message_data
        return result

    def list_conversations(
        self,
        workspace_id: str,
        user_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.db.read() as conn:
            rows = conn.execute(
                """SELECT c.id,c.title,c.scope_json,c.created_at,c.updated_at,
                     count(m.id) message_count,max(m.created_at) last_message_at,
                     (SELECT latest.content FROM messages latest
                       WHERE latest.conversation_id=c.id AND latest.role='user'
                       ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1) last_question
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
                   WHERE c.workspace_id=? AND c.user_id=?
                   GROUP BY c.id
                   ORDER BY coalesce(max(m.created_at),c.updated_at) DESC,c.id DESC
                   LIMIT ?""",
                (workspace_id, user_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["scope"] = _loads(item.pop("scope_json"), {})
            item["last_activity_at"] = item["last_message_at"] or item["updated_at"]
            items.append(item)
        return items

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
            conn.execute(
                """INSERT INTO messages(id,conversation_id,role,content,status,run_id,metadata_json,created_at)
                   VALUES (?,?,?,?,?,NULL,'{}',?)""",
                (user_message_id, conversation_id, "user", content, "completed", now),
            )
            conn.execute(
                """INSERT INTO messages(id,conversation_id,role,content,status,run_id,metadata_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (assistant_message_id, conversation_id, "assistant", "", "running", run_id, "{}", now),
            )
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            conn.execute(
                """INSERT INTO runs(
                     id,workspace_id,conversation_id,assistant_message_id,status,warning_json,model_json,
                     created_at,completed_at,client_message_id
                   ) VALUES (?,?,?,?,?,'[]','{}',?,NULL,?)""",
                (run_id, workspace_id, conversation_id, assistant_message_id, "pending", now, client_message_id),
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
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            now = utc_now()
            with self.db.transaction(immediate=True) as conn:
                run = conn.execute(
                    """SELECT * FROM runs
                       WHERE attempts < ? AND (status='pending' OR (status='running' AND lease_expires_at < ?))
                       ORDER BY created_at LIMIT 1""",
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
        with self.db.read() as conn:
            run = conn.execute(
                """SELECT r.*,c.scope_json,c.user_id,m.created_at assistant_created_at
                   FROM runs r JOIN conversations c ON c.id=r.conversation_id
                   JOIN messages m ON m.id=r.assistant_message_id WHERE r.id=?""",
                (run_id,),
            ).fetchone()
            if not run:
                raise ResourceNotFound("Run not found")
            question_row = conn.execute(
                """SELECT content FROM messages WHERE conversation_id=? AND role='user' AND created_at<=?
                   ORDER BY created_at DESC LIMIT 1""",
                (run["conversation_id"], run["assistant_created_at"]),
            ).fetchone()
            history_rows = conn.execute(
                """SELECT role,content FROM messages WHERE conversation_id=? AND status='completed'
                   ORDER BY created_at DESC LIMIT 8""",
                (run["conversation_id"],),
            ).fetchall()
        if not question_row:
            raise InvalidState("Run has no user question")
        question = str(question_row["content"])
        scope = _loads(run["scope_json"], {})
        document_ids = list(scope.get("document_ids", []))
        history = [dict(row) for row in reversed(history_rows)]
        try:
            with self.db.transaction(immediate=True) as conn:
                self._run_event(conn, run_id, "status", {"node": "query_planning", "message": "正在理解问题并生成检索计划"})
            plan = self.query_planner.plan(
                question,
                history=history,
                document_ids=document_ids,
                document_vocabulary=self._document_vocabulary(str(run["workspace_id"]), document_ids),
                run_id=run_id,
            )
            answer_mode = plan.intent
            with self.db.transaction(immediate=True) as conn:
                self._run_event(
                    conn,
                    run_id,
                    "query_plan",
                    {
                        "intent": plan.intent,
                        "secondary_intents": list(plan.secondary_intents),
                        "operations": list(plan.operations),
                        "intent_confidence": plan.intent_confidence,
                        "profile": plan.execution_profile,
                        "planner": plan.planner,
                        "queries": [item.to_dict() for item in plan.retrieval_queries],
                    },
                )
                self._run_event(conn, run_id, "status", {"node": "retrieval", "message": "正在执行多路检索并筛选业务证据"})
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
                "status": "disabled" if not self.settings.llm_enabled else "skipped_no_evidence",
                "answer_mode": answer_mode,
                "planner": plan.planner,
            }
            selected_agent = self.deep_qa_agent if plan.execution_profile == "deep" else self.qa_agent
            single_term_definition = plan.active_intents == ("term_definition",)
            if selected_agent and evidence and not single_term_definition:
                with self.db.transaction(immediate=True) as conn:
                    self._run_event(conn, run_id, "status", {"node": "answer_generation", "message": "正在基于证据生成回答"})
                generated = selected_agent.answer(
                    question=question,
                    deterministic_answer=answer,
                    evidence=evidence,
                    history=history,
                    answer_mode=answer_mode,
                    query_plan=plan.to_dict(),
                    run_id=run_id,
                )
                answer = generated.answer
                warnings = list(dict.fromkeys([*warnings, *generated.warnings]))
                model_info = {**generated.model, "answer_mode": answer_mode, "planner": plan.planner}
            elif single_term_definition:
                model_info["status"] = "skipped_curated_glossary"
            message_metadata = {
                "answer_mode": answer_mode,
                "show_visuals": not single_term_definition,
                "knowledge_source": (
                    "curated_glossary+document"
                    if "term_definition" in plan.active_intents and evidence
                    else "curated_glossary"
                    if single_term_definition
                    else "document_evidence"
                ),
                "pipeline_version": "planned-evidence-v2",
                "query_plan": plan.to_dict(),
                "answer_routing": answer_result.diagnostics.get("answer_routing", {}),
                "retrieval": answer_result.diagnostics.get("retrieval", {}),
                "evidence_pack": answer_result.diagnostics.get("evidence_pack", {}),
                "evaluation_assessment": answer_result.diagnostics.get("evaluation_assessment", {}),
                "negative_assessment": answer_result.diagnostics.get("negative_assessment", {}),
            }
            self._complete_run(run, answer, evidence, warnings, model_info, message_metadata)
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
                        ("回答生成失败，请稍后重试。", run["assistant_message_id"]),
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
        run_id = str(run["id"])
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
                        "planner": model_info.get("planner"),
                    },
                    separators=(",", ":"),
                )
            )
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET content=?,status='completed',metadata_json=? WHERE id=?",
                (answer, self.db.json(message_metadata or {}), run["assistant_message_id"]),
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
                (self.db.json(warnings), self.db.json(model_info), utc_now(), run_id),
            )
            self._run_event(conn, run_id, "completed", {"message_id": run["assistant_message_id"]})

    def _document_vocabulary(self, workspace_id: str, document_ids: list[str]) -> list[str]:
        scope_sql = ""
        scope_args: list[Any] = []
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            scope_sql = f" AND d.id IN ({placeholders})"
            scope_args.extend(document_ids)
        with self.db.read() as conn:
            rows = conn.execute(
                f"""
                SELECT d.title document_title,s.title slide_title
                FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE d.workspace_id=? AND d.deleted_at IS NULL
                  AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
                ORDER BY d.updated_at DESC,s.slide_no LIMIT 80
                """,
                (workspace_id, *scope_args),
            ).fetchall()
        terms: list[str] = []
        for row in rows:
            for value in (row["document_title"], row["slide_title"]):
                terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9&/_-]{1,40}|[\u4e00-\u9fff]{2,16}", str(value or "")))
        return list(dict.fromkeys(terms))[:120]

    def _run_event(self, conn: sqlite3.Connection, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO run_events(run_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
            (run_id, event_type, self.db.json(data), utc_now()),
        )

    # Transitional private-policy shims. Public orchestration stays here while
    # deterministic evidence reasoning belongs to DeterministicAnswerEngine.
    def _answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer_engine.answer(question, workspace_id, document_ids)

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        return self.answer_engine._table_reasoning_answer(question, sources)

    def _constraint_abstention(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        return self.answer_engine._constraint_abstention(question, chunks)

    def _chart_comparison_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        return self.answer_engine._chart_comparison_answer(question, chart_rows)

    def _rank_chart_points(
        self,
        question: str,
        rows: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        return self.answer_engine._rank_chart_points(question, rows)

    def _axis_descriptor(self, row: dict[str, Any]) -> str:
        return self.answer_engine._axis_descriptor(row)

    def _chart_answer(
        self,
        question: str,
        ranked: list[tuple[int, dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer_engine._chart_answer(question, ranked)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=? AND workspace_id=?", (run_id, workspace_id)).fetchone()
            if not row:
                raise ResourceNotFound("Run not found")
            message = conn.execute("SELECT * FROM messages WHERE id=?", (row["assistant_message_id"],)).fetchone()
            citations = conn.execute(
                "SELECT * FROM citations WHERE message_id=? ORDER BY claim_no",
                (row["assistant_message_id"],),
            ).fetchall()
            result = dict(row)
            result["warnings"] = _loads(result.pop("warning_json"), [])
            result["model"] = _loads(result.pop("model_json"), {})
            if message:
                public_message = dict(message)
                public_message["metadata"] = _loads(public_message.pop("metadata_json", None), {})
                result["message"] = public_message
            else:
                result["message"] = None
            result["citations"] = [self._citation_public(dict(citation), conn) for citation in citations]
            return result

    def run_events(self, run_id: str, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.get_run(run_id, workspace_id)
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)).fetchall()
        return [{"id": row["id"], "event": row["event_type"], "data": _loads(row["data_json"], {})} for row in rows]

    def _citation_public(self, item: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
        slide = conn.execute(
            """
            SELECT s.slide_no,d.id document_id,d.title document_title,e.element_type
            FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
            JOIN documents d ON d.id=dv.document_id LEFT JOIN elements e ON e.id=?
            WHERE s.id=?
            """,
            (item.get("element_id"), item["slide_id"]),
        ).fetchone()
        item["bbox"] = _loads(item.pop("bbox_json"), {})
        item["quote"] = item.pop("quote_text")
        item["label"] = f"[{item['claim_no']}]"
        if slide:
            item.update(dict(slide))
        item["preview_url"] = f"/api/v1/slides/{item['slide_id']}/preview"
        return item

    def add_feedback(
        self,
        message_id: str,
        workspace_id: str,
        user_id: str,
        rating: int,
        category: str | None,
        comment: str | None,
    ) -> dict[str, Any]:
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
