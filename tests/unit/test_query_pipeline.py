from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from pytest import LogCaptureFixture

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.foundation.database import utc_now
from packages.qbr_core.planning import QueryPlannerAgent, deterministic_plan
from packages.qbr_core.retrieval.engine import EvidenceRetriever, query_terms
from packages.qbr_core.retrieval.evidence import EvidencePackBuilder, extract_relevant_quote


class PlannerModel:
    def __init__(
        self,
        content: str | Exception,
        *,
        finish_reason: str | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.messages: Any = None

    def invoke(self, messages: Any) -> AIMessage:
        self.messages = messages
        if isinstance(self.content, Exception):
            raise self.content
        metadata: dict[str, Any] = {}
        if self.finish_reason is not None:
            metadata["finish_reason"] = self.finish_reason
        usage: dict[str, Any] | None = None
        if self.output_tokens is not None:
            usage = {
                "input_tokens": 10,
                "output_tokens": self.output_tokens,
                "total_tokens": 10 + self.output_tokens,
                "output_token_details": {"reasoning": self.reasoning_tokens or 0},
            }
        return AIMessage(content=self.content, response_metadata=metadata, usage_metadata=usage)


def _seed_pipeline_document(service: QBRService) -> str:
    now = utc_now()
    document_id = "doc_pipeline"
    version_id = "dv_pipeline"
    parser_run_id = "run_pipeline"
    chunks = (
        (
            "slide_risk",
            1,
            "Risk concentration",
            "chunk_risk",
            "text",
            "Risk concentration increased to 48%, above the 45% limit. Credit exposure deteriorated and entered the red warning zone.",
        ),
        (
            "slide_margin",
            2,
            "Margin pressure",
            "chunk_margin",
            "text",
            "Product mix volatility increased during Q2. Margin fell below target by 3 percentage points and requires management action.",
        ),
        (
            "slide_sources",
            3,
            "Sources",
            "chunk_sources",
            "notes",
            "[Sources] Generated visual: OpenAI ImageGen. All monthly management data are illustrative synthetic test data.",
        ),
    )
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            """INSERT INTO documents(
                 id,workspace_id,title,status,metadata_json,deleted_at,created_by,created_at,updated_at
               ) VALUES (?,?,?,'ready','{}',NULL,'user_demo',?,?)""",
            (document_id, "ws_demo", "Pipeline QBR", now, now),
        )
        conn.execute(
            """INSERT INTO document_versions(
                 id,document_id,version_no,sha256,mime_type,size_bytes,original_uri,active_parser_run_id,created_at
               ) VALUES (?,?,1,?,'application/vnd.openxmlformats-officedocument.presentationml.presentation',1,?,?,?)""",
            (version_id, document_id, version_id, f"/{version_id}.pptx", parser_run_id, now),
        )
        conn.execute(
            """INSERT INTO parser_runs(
                 id,document_version_id,skill_name,skill_version,schema_version,status,quality_json,started_at,completed_at
               ) VALUES (?,?,?,'1','1','ready','{}',?,?)""",
            (parser_run_id, version_id, "test", now, now),
        )
        for slide_id, slide_no, title, chunk_id, chunk_type, content in chunks:
            element_id = f"element_{slide_no}"
            conn.execute(
                """INSERT INTO slides(
                     id,parser_run_id,document_version_id,slide_no,title,summary,notes_text,
                     width_emu,height_emu,render_uri,quality_score
                   ) VALUES (?,?,?,?,?,?,NULL,1,1,NULL,1)""",
                (slide_id, parser_run_id, version_id, slide_no, title, content),
            )
            conn.execute(
                """INSERT INTO elements(
                     id,slide_id,parent_id,element_type,reading_order,bbox_json,text_content,
                     structured_json,provenance_json,confidence,review_status
                   ) VALUES (?,?,NULL,?,1,'{}',?,'{}','{}',1,'accepted')""",
                (element_id, slide_id, chunk_type, content),
            )
            conn.execute(
                """INSERT INTO chunks(
                     id,workspace_id,document_version_id,slide_id,element_id,chunk_type,
                     content,metadata_json,content_hash,active
                   ) VALUES (?,?,?,?,?,?,?,'{}',?,1)""",
                (chunk_id, "ws_demo", version_id, slide_id, element_id, chunk_type, content, chunk_id),
            )
            conn.execute("INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)", (chunk_id, "ws_demo", content))
    return document_id


def test_language_fallback_builds_task_frame_without_intent_labels() -> None:
    plan = deterministic_plan("请总结公司的优势和潜在问题。")
    payload = plan.to_dict()
    query_corpus = " ".join(item.text for item in plan.retrieval_queries).casefold()

    assert plan.planner == "linguistic_fallback"
    assert plan.task_summary == "请总结公司的优势和潜在问题。"
    assert "intent" not in payload
    assert "secondary_intents" not in payload
    assert "优势" in query_corpus and "风险" in query_corpus


def test_language_fallback_preserves_hard_constraints_without_classifying() -> None:
    plan = deterministic_plan("How did sales perform in Q2 2025?")
    assert plan.hard_constraints == ("2025", "Q2")
    assert plan.retrieval_queries[0].text == "How did sales perform in Q2 2025?"
    assert plan.operations == ("answer from evidence",)


def test_llm_planner_owns_semantic_task_frame_and_keeps_language_guard_queries() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "Evaluate Q2 2025 sales against prior period and target",
                "task_summary": "Explain Q2 2025 sales performance and its drivers.",
                "answer_brief": "Lead with the result, then compare the relevant periods and explain supported drivers.",
                "operations": ["compare periods", "explain drivers"],
                "evidence_requirements": ["Q2 2025 sales", "prior-period comparator", "documented drivers"],
                "retrieval_queries": [
                    {"text": "Q2 2025 revenue trend target variance", "kind": "metric_hypothesis"},
                    {"text": "2025年第二季度 营收 趋势 目标 偏差", "kind": "cross_language"},
                ],
                "execution_profile": "deep",
                "needs_visuals": True,
                "visual_structure": {
                    "series_group_counts": [5, 5],
                    "point_counts": [24],
                    "chart_families": ["bar", "line", "unknown-family"],
                },
                "planner_confidence": 0.93,
            }
        ),
        output_tokens=120,
    )
    plan = QueryPlannerAgent(model).plan("How did sales perform in Q2 2025?")
    corpus = " ".join(item.text for item in plan.retrieval_queries)

    assert plan.planner == "llm_semantic"
    assert plan.task_summary.startswith("Explain Q2 2025")
    assert plan.operations == ("compare periods", "explain drivers")
    assert plan.execution_profile == "deep"
    assert plan.hard_constraints == ("2025", "Q2")
    assert plan.retrieval_queries[0].kind == "literal"
    assert "2025年第二季度" in corpus
    assert plan.visual_structure == {
        "series_group_counts": [5, 5],
        "point_counts": [24],
        "chart_families": ["bar", "line"],
    }
    assert plan.diagnostics["input_tokens"] == 10
    assert plan.diagnostics["output_tokens"] == 120
    assert plan.diagnostics["total_tokens"] == 130


def test_planner_prompt_uses_structured_summary_only_for_reference_resolution() -> None:
    model = PlannerModel(json.dumps({"task_summary": "Resolve it from evidence."}))

    QueryPlannerAgent(model).plan(
        "它怎么样？",
        conversation_summary={"entities": ["ACME"], "conversation_goals": ["分析收入"]},
    )

    prompt = "\n".join(str(getattr(item, "content", "")) for item in model.messages or [])
    assert '"entities":["ACME"]' in prompt
    assert "reference resolution only, never business evidence" in prompt


def test_llm_planner_accepts_multi_part_goal_without_collapsing_to_one_category() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "Summarize strengths and potential issues",
                "task_summary": "Provide a balanced synthesis of supported strengths and potential issues.",
                "answer_brief": "Cover both sides and distinguish observed facts from possible concerns.",
                "operations": ["synthesize strengths", "identify concerns", "state evidence boundaries"],
                "evidence_requirements": ["positive performance evidence", "adverse or threshold evidence"],
                "retrieval_queries": [{"text": "strengths risks thresholds performance", "kind": "balanced_hypothesis"}],
                "execution_profile": "deep",
                "needs_visuals": True,
                "planner_confidence": 0.9,
            }
        )
    )
    plan = QueryPlannerAgent(model).plan("请总结公司的优势和潜在问题。")
    assert plan.operations == ("synthesize strengths", "identify concerns", "state evidence boundaries")
    assert not hasattr(plan, "active_intents")


def test_llm_planner_realigns_creation_drift_for_existing_chart_analysis() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "设计一张五组柱线双轴图",
                "task_summary": "创建并设计双轴监控图方案。",
                "answer_brief": "说明如何绘制左右轴并设计图例。",
                "operations": ["设计坐标轴", "生成图表", "假设可能的关联"],
                "evidence_requirements": ["图表设计规范"],
                "retrieval_queries": [
                    {"text": "双轴图 设计 绘制规范", "kind": "design_hypothesis"},
                    {"text": "现有图表 五组柱线 趋势 异常", "kind": "analysis_hypothesis"},
                ],
                "needs_visuals": True,
            }
        )
    )

    plan = QueryPlannerAgent(model).plan("请解读五组柱状与五组折线构成的现有双轴图，分析并推理")
    query_corpus = " ".join(item.text for item in plan.retrieval_queries)

    assert plan.task_summary == plan.original_question
    assert plan.operations == ("识别用户指定的现有证据", "比较结构、变化与异常", "形成有依据的推断并说明验证边界")
    assert "设计 绘制规范" not in query_corpus
    assert "现有图表" in query_corpus
    assert plan.warnings == ()
    assert plan.diagnostics["action_realigned"] is True


def test_planner_provider_failure_uses_non_classifying_language_fallback(caplog: LogCaptureFixture) -> None:
    plan = QueryPlannerAgent(PlannerModel(RuntimeError("offline")), provider="test", model_name="planner").plan(
        "当前文档有哪些潜在问题？", run_id="run_planner_failure"
    )
    assert plan.planner == "linguistic_fallback"
    assert plan.warnings == ("QUERY_PLANNER_PROVIDER_ERROR",)
    assert not hasattr(plan, "intent")
    assert '"component":"semantic_task_planner"' in caplog.text


def test_invalid_planner_output_falls_back_without_inventing_a_category() -> None:
    plan = QueryPlannerAgent(
        PlannerModel(
            "",
            finish_reason="length",
            output_tokens=1202,
            reasoning_tokens=1200,
        )
    ).plan("VONB是什么意思？")
    assert plan.planner == "linguistic_fallback"
    assert plan.warnings == ("QUERY_PLANNER_OUTPUT_INVALID",)
    assert plan.diagnostics == {
        "content_length": 0,
        "finish_reason": "length",
        "input_tokens": 10,
        "output_tokens": 1202,
        "total_tokens": 1212,
        "reasoning_tokens": 1200,
    }


def test_evidence_pack_uses_semantic_requirements_and_rejects_off_task_notes() -> None:
    plan = deterministic_plan("主要风险是什么？")
    candidates = [
        {
            "id": "notes",
            "document_version_id": "dv",
            "slide_id": "s1",
            "slide_no": 1,
            "chunk_type": "notes",
            "content": "[Sources] Generated visual: OpenAI ImageGen; synthetic test data.",
        },
        {
            "id": "risk",
            "document_version_id": "dv",
            "slide_id": "s2",
            "slide_no": 2,
            "chunk_type": "text",
            "content": "Margin fell below target and management action is required.",
            "retrieval_score": 2,
        },
    ]
    pack = EvidencePackBuilder().build(plan, candidates)
    quotes = "\n".join(atom.quote for atom in pack.atoms)
    assert "Margin fell below target" in quotes
    assert "[Sources]" not in quotes


def test_evidence_pack_rejects_headings_and_semantic_duplicates_without_an_intent_router() -> None:
    plan = deterministic_plan("当前文档有哪些潜在问题？")
    candidates = [
        {
            "id": "heading",
            "document_version_id": "dv",
            "slide_id": "s1",
            "slide_no": 1,
            "chunk_type": "text",
            "content": "NEXT-QUARTER PRIORITIES & RISK GATES",
            "retrieval_score": 5,
        },
        {
            "id": "fact",
            "document_version_id": "dv",
            "slide_id": "s2",
            "slide_no": 2,
            "chunk_type": "text",
            "content": "股东资本比率为221%，高于210%的绿色阈值。",
            "retrieval_score": 4,
        },
        {
            "id": "duplicate",
            "document_version_id": "dv",
            "slide_id": "s3",
            "slide_no": 3,
            "chunk_type": "text",
            "content": "股东资本比率为221%，高于210%的绿色阈值！",
            "retrieval_score": 3,
        },
    ]

    pack = EvidencePackBuilder().build(plan, candidates)

    assert len(pack.atoms) == 1
    assert "221%" in pack.atoms[0].quote
    assert pack.diagnostics["rejected_quality"]["heading_like"] == 1


def test_multiroute_retrieval_uses_paraphrase_bridges_without_intent_router(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_pipeline_document(service)
    plan = deterministic_plan("what is the bad news in this ppt", [document_id])
    retrieval = EvidenceRetriever(service.db).search_plan(plan, "ws_demo", [document_id])
    ids = {row["id"] for row in retrieval.items}
    assert {"chunk_risk", "chunk_margin"} <= ids
    assert "chunk_sources" not in ids
    assert retrieval.diagnostics["task_summary"] == "what is the bad news in this ppt"


def test_end_to_end_metadata_exposes_task_frame_not_intent_taxonomy(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_pipeline_document(service)
    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(conversation["id"], "当前文档里有哪些潜在问题？", "ws_demo", "user_demo")
    service.process_next_run("semantic-task-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])
    plan = message["metadata"]["query_plan"]

    assert plan["task_summary"] == "当前文档里有哪些潜在问题？"
    assert "intent" not in plan and "active_intents" not in plan
    assert message["metadata"]["answer_routing"]["strategy"] == "semantic_grounding"
    assert message["metadata"]["answer_metrics"]["duration_ms"] >= 0
    assert message["metadata"]["answer_metrics"]["token_usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert "文档中的核心图表指标" not in message["content"]
    assert not message["content"].startswith("编号证据")
    assert "可核验证据" in message["content"]


def test_extract_relevant_quote_keeps_complete_semantic_units() -> None:
    plan = deterministic_plan("Digital STP 有何变化？")
    content = "背景说明。 Digital STP 从73.8降至72.1。 管理层要求修复流程。"
    quote = extract_relevant_quote(content, plan)
    assert "Digital STP 从73.8降至72.1。" in quote
    assert not quote.endswith("从73.8")


def test_query_terms_keep_domain_identifiers_and_years() -> None:
    assert {"vonb", "2025", "q2"} <= set(query_terms("VONB performance in 2025 Q2"))
