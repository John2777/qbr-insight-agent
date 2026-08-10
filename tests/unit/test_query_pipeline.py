from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from pytest import LogCaptureFixture

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.db import utc_now
from packages.qbr_core.evidence import EvidencePackBuilder, extract_relevant_quote
from packages.qbr_core.query_planning import QueryPlannerAgent, deterministic_plan
from packages.qbr_core.retrieval import EvidenceRetriever, query_terms

ROOT = Path(__file__).parents[2]


class PlannerModel:
    def __init__(self, content: str | Exception) -> None:
        self.content = content

    def invoke(self, _messages: Any) -> AIMessage:
        if isinstance(self.content, Exception):
            raise self.content
        return AIMessage(content=self.content)


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
            "[Sources] Generated visual: OpenAI ImageGen. All monthly management data in this deck are illustrative synthetic test data.",
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
            conn.execute(
                "INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)",
                (chunk_id, "ws_demo", content),
            )
    return document_id


def _seed_strength_document(service: QBRService) -> str:
    now = utc_now()
    document_id = "doc_strength"
    version_id = "dv_strength"
    parser_run_id = "run_strength"
    chunks = (
        (
            "slide_growth",
            1,
            "Record growth",
            "chunk_growth",
            "text",
            "Record value growth continued: VONB, profit and cash generation all improved, with momentum extending into Q1.",
        ),
        (
            "slide_financials",
            2,
            "Financial data table",
            "chunk_financials",
            "table",
            (
                "Metric | 2024A | 2025A | Change | Unit\n"
                "VONB | 4,712 | 5,516 | +15% CER | US$m\n"
                "Net FSG | 4,020 | 4,451 | +14% per share | US$m\n"
                "Shareholder capital ratio | 236% | 221% | -15ppt | %"
            ),
        ),
        (
            "slide_mix",
            3,
            "Portfolio gates",
            "chunk_mix",
            "table",
            "Metric | Current | Threshold\nLargest market share | 41% | <45%\nPortfolio growth | +15% | >10%",
        ),
    )
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            """INSERT INTO documents(
                 id,workspace_id,title,status,metadata_json,deleted_at,created_by,created_at,updated_at
               ) VALUES (?,?,?,'ready','{}',NULL,'user_demo',?,?)""",
            (document_id, "ws_demo", "Strength QBR", now, now),
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
            element_id = f"strength_element_{slide_no}"
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
            conn.execute(
                "INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)",
                (chunk_id, "ws_demo", content),
            )
    return document_id


def test_negative_question_plan_is_bilingual_and_excludes_source_notes() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    queries = " ".join(item.text for item in plan.retrieval_queries).casefold()

    assert plan.intent == "negative_signal_summary"
    assert plan.execution_profile == "deep"
    assert plan.retrieval_queries[0].text == "what is the bad news in this ppt"
    assert "risk concentration" in queries
    assert "风险" in queries
    assert "provenance" in plan.excluded_content_roles


def test_latent_issue_phrasings_share_negative_discovery_intent_without_summary_drift() -> None:
    negative_questions = (
        "当前文档里能找到哪些公司潜在的问题？",
        "这家公司可能存在哪些经营隐患？",
        "哪些方面看起来需要警惕？",
        "Where are the weak spots in this business?",
        "What potential issues should management investigate in this deck?",
    )

    for question in negative_questions:
        plan = deterministic_plan(question)
        assert plan.intent == "negative_signal_summary", question
        assert plan.evaluation_polarity == "negative", question

    assert deterministic_plan("当前文档有哪些核心增长指标？").intent == "evidence_answer"
    assert deterministic_plan("请总结当前文档的核心指标。").intent == "summary"
    assert deterministic_plan("公司计划如何解决这些问题？").intent == "evidence_answer"


def test_task_frame_preserves_multiple_user_goals_and_builds_multi_route_queries() -> None:
    term_and_chart = deterministic_plan("VONB是什么含义，并分析图中的趋势？")
    balanced_summary = deterministic_plan("请总结公司的优势和潜在问题。")
    provenance_and_risk = deterministic_plan("哪些数据是公开披露或模拟数据，同时有什么主要风险？")

    assert term_and_chart.active_intents == ("term_definition", "chart_analysis")
    assert term_and_chart.operations == ("define_term", "analyze_chart")
    assert "value of new business" in " ".join(item.text for item in term_and_chart.retrieval_queries).casefold()

    assert balanced_summary.active_intents == ("business_evaluation", "negative_signal_summary", "summary")
    assert balanced_summary.evaluation_polarity == "balanced"
    assert {"evaluate_business", "assess_downside", "synthesize_summary"} <= set(balanced_summary.operations)

    assert provenance_and_risk.active_intents == ("negative_signal_summary", "provenance")
    assert "provenance" in {item.kind for item in provenance_and_risk.retrieval_queries}
    assert "provenance" in provenance_and_risk.allowed_content_roles
    assert "provenance" not in provenance_and_risk.excluded_content_roles


def test_execution_risk_explanation_has_dedicated_intent_and_retrieval_routes() -> None:
    plan = deterministic_plan("所谓的执行风险具体是指什么？")
    queries = " ".join(item.text for item in plan.retrieval_queries).casefold()

    assert plan.intent == "risk_explanation"
    assert plan.execution_profile == "deep"
    assert plan.answer_language == "zh"
    assert "execution risk" in queries
    assert "风险闸门" in queries
    assert "优先事项" in queries
    assert "provenance" in plan.excluded_content_roles


def test_strength_question_plan_is_evaluative_bilingual_and_deep() -> None:
    plan = deterministic_plan("公司的优势在哪些点上")
    queries = " ".join(item.text for item in plan.retrieval_queries).casefold()

    assert plan.intent == "business_evaluation"
    assert plan.evaluation_polarity == "positive"
    assert plan.execution_profile == "deep"
    assert plan.retrieval_queries[0].text == "公司的优势在哪些点上"
    assert "strength advantage" in queries
    assert "增长 价值 盈利" in queries
    assert "继续率" in queries
    assert "portfolio" in queries
    assert plan.required_facets == (
        "growth_momentum",
        "profitability_value",
        "cash_capital",
        "operating_quality",
        "portfolio_resilience",
        "execution_delivery",
    )


def test_balanced_and_opportunity_questions_keep_evaluation_polarity() -> None:
    balanced = deterministic_plan("公司的优劣势分别是什么？")
    opportunity = deterministic_plan("下一阶段有哪些增长机会？")

    assert (balanced.intent, balanced.evaluation_polarity) == ("business_evaluation", "balanced")
    assert (opportunity.intent, opportunity.evaluation_polarity) == ("business_evaluation", "opportunity")


def test_exact_risk_table_question_is_not_misclassified_as_global_bad_news() -> None:
    plan = deterministic_plan("Which allocation has the lowest risk limit utilization in Q2 2025?")

    assert plan.intent == "evidence_answer"
    assert plan.hard_constraints == ("2025", "Q2")


def test_model_planner_merges_expansions_without_losing_literal_query_or_constraints() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "Q2 2025 sales performance",
                "intent": "chart_analysis",
                "retrieval_queries": [
                    {"text": "Q2 2025 revenue trend target variance", "kind": "metric_synonym"},
                    {"text": "2025年第二季度 营收 趋势 目标 偏差", "kind": "cross_language"},
                ],
            }
        )
    )

    plan = QueryPlannerAgent(model).plan("How did sales perform in Q2 2025?")

    assert plan.planner == "llm"
    assert plan.intent == "chart_analysis"
    assert plan.retrieval_queries[0].text == "How did sales perform in Q2 2025?"
    assert plan.hard_constraints == ("2025", "Q2")
    assert any("营收" in item.text for item in plan.retrieval_queries)


def test_model_planner_can_promote_ambiguous_evaluative_question_with_polarity() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "company strengths supported by comparative evidence",
                "intent": "business_evaluation",
                "evaluation_polarity": "positive",
                "retrieval_queries": [{"text": "record growth above target capital buffer", "kind": "evaluation_hypothesis"}],
            }
        )
    )

    plan = QueryPlannerAgent(model).plan("Where does the company stand out?")

    assert plan.planner == "llm"
    assert plan.intent == "business_evaluation"
    assert plan.evaluation_polarity == "positive"
    assert plan.execution_profile == "deep"
    assert "growth_momentum" in plan.required_facets
    assert plan.retrieval_queries[0].text == "Where does the company stand out?"
    assert any("capital buffer" in item.text for item in plan.retrieval_queries)


def test_model_planner_can_correct_a_non_generic_baseline_without_erasing_original_route() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "verify sources while retaining the requested overview",
                "intent": "provenance",
                "secondary_intents": ["summary"],
                "operations": ["verify_provenance", "synthesize_summary"],
                "intent_confidence": 0.94,
                "retrieval_queries": [{"text": "official disclosure source boundary", "kind": "source_check"}],
            }
        )
    )

    plan = QueryPlannerAgent(model).plan("请总结这份文档，并说明哪些结论可以追溯核验。")

    assert plan.intent == "provenance"
    assert plan.secondary_intents == ("summary",)
    assert plan.operations == ("verify_provenance", "synthesize_summary")
    assert plan.diagnostics["model_intent_promoted"] is True
    assert "official disclosure" in " ".join(item.text for item in plan.retrieval_queries)


def test_model_planner_does_not_dilute_high_confidence_term_intent_without_explicit_secondary_task() -> None:
    model = PlannerModel(
        json.dumps(
            {
                "canonical_question": "define VONB",
                "intent": "summary",
                "intent_confidence": 0.99,
                "retrieval_queries": [],
            }
        )
    )

    plan = QueryPlannerAgent(model).plan("VONB是什么意思？")

    assert plan.active_intents == ("term_definition",)
    assert plan.operations == ("define_term",)


def test_planner_provider_failure_has_deterministic_fallback(caplog: LogCaptureFixture) -> None:
    plan = QueryPlannerAgent(PlannerModel(RuntimeError("offline")), provider="test-provider", model_name="test-model").plan(
        "主要风险是什么？",
        run_id="run_planner_failure",
    )

    assert plan.intent == "negative_signal_summary"
    assert plan.planner == "deterministic_fallback"
    assert plan.warnings == ("QUERY_PLANNER_PROVIDER_ERROR",)
    assert '"component":"query_planner"' in caplog.text
    assert '"run_id":"run_planner_failure"' in caplog.text


def test_query_terms_remove_question_scaffolding() -> None:
    assert query_terms("what is the bad news in this ppt") == ["bad", "news"]


def test_semantic_extraction_keeps_complete_relevant_sentence_without_arbitrary_cutoff() -> None:
    plan = deterministic_plan("What are the main risks in this deck?")
    content = (
        "Background information is stable and operational detail is unchanged. "
        + "Context " * 80
        + ". Risk concentration increased to 48%, above the approved 45% threshold. "
        + "The mitigation owner will report next quarter."
    )

    quote = extract_relevant_quote(content, plan)

    assert "Risk concentration increased to 48%, above the approved 45% threshold." in quote
    assert not quote.endswith("thresh")


def test_evidence_pack_filters_provenance_and_preserves_table_rows() -> None:
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
            "id": "table",
            "document_version_id": "dv",
            "slide_id": "s2",
            "slide_no": 2,
            "chunk_type": "table",
            "content": "Metric | Current | Limit\nRisk concentration | 48% | 45%\nRevenue growth | 15% | 10%",
            "retrieval_score": 3,
        },
        {
            "id": "risk",
            "document_version_id": "dv",
            "slide_id": "s3",
            "slide_no": 3,
            "chunk_type": "text",
            "content": "Margin fell below target and management action is required.",
            "retrieval_score": 2,
        },
        {
            "id": "heading",
            "document_version_id": "dv",
            "slide_id": "s4",
            "slide_no": 4,
            "chunk_type": "text",
            "content": "RISK CONCENTRATION",
            "retrieval_score": 5,
        },
        {
            "id": "green-table",
            "document_version_id": "dv",
            "slide_id": "s5",
            "slide_no": 5,
            "chunk_type": "table",
            "content": (
                "Risk gate | Green | Amber | Red | Current\n"
                "Capital ratio | >210% | 190-210% | <190% | 226%\n"
                "Liquidity index | >150 | 125-150 | <125 | 176"
            ),
            "retrieval_score": 5,
        },
        {
            "id": "chart",
            "document_version_id": "dv",
            "slide_id": "s6",
            "slide_no": 6,
            "chunk_type": "chart_series",
            "content": "Chart\nRisk capital allocation | Market=28; Credit=22; Insurance=19",
            "retrieval_score": 5,
        },
        {
            "id": "management-action",
            "document_version_id": "dv",
            "slide_id": "s7",
            "slide_no": 7,
            "chunk_type": "text",
            "content": "强化高净值、保障与跨境服务，控制产品集中度。",
            "retrieval_score": 4,
        },
    ]

    pack = EvidencePackBuilder().build(plan, candidates)
    quotes = "\n".join(atom.quote for atom in pack.atoms)

    assert pack.answerable is True
    assert "[Sources]" not in quotes
    assert "RISK CONCENTRATION" not in quotes
    assert "Capital ratio" not in quotes
    assert "Risk capital allocation" not in quotes
    assert "控制产品集中度" in quotes
    assert "Metric | Current | Limit" in quotes
    assert "Risk concentration | 48% | 45%" in quotes
    assert "Revenue growth | 15% | 10%" not in quotes
    assert pack.diagnostics["rejected_quality"] == {
        "heading_like": 1,
        "all_green_status_table": 1,
        "uninterpreted_chart": 1,
    }


def test_multiroute_retrieval_and_end_to_end_answer_reject_badcase_sources(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_pipeline_document(service)
    plan = deterministic_plan("what is the bad news in this ppt", [document_id])

    retrieval = EvidenceRetriever(service.db).search_plan(plan, "ws_demo", [document_id])
    assert {row["id"] for row in retrieval.items} == {"chunk_risk", "chunk_margin"}

    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(conversation["id"], "what is the bad news in this ppt", "ws_demo", "user_demo")
    service.process_next_run("query-pipeline-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])

    assert "Risk concentration" in message["content"]
    assert "below target" in message["content"]
    assert "[Sources]" not in message["content"]
    assert "ImageGen" not in message["content"]
    assert message["metadata"]["query_plan"]["intent"] == "negative_signal_summary"
    assert message["metadata"]["pipeline_version"] == "planned-evidence-v2"
    assert message["metadata"]["negative_assessment"]["signal_count"] == 2
    assert {citation["slide_no"] for citation in message["citations"]} == {1, 2}


def test_latent_issue_question_cannot_be_overridden_by_document_summary_route(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_pipeline_document(service)
    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(
        conversation["id"],
        "当前文档里能找到哪些公司潜在的问题？",
        "ws_demo",
        "user_demo",
    )

    service.process_next_run("latent-issue-routing-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])

    assert message["metadata"]["query_plan"]["intent"] == "negative_signal_summary"
    assert message["metadata"]["answer_routing"]["intent"] == "negative_signal_summary"
    assert message["metadata"]["answer_routing"]["active_intents"] == ["negative_signal_summary"]
    assert message["metadata"]["answer_routing"]["strategy"] == "specialized"
    assert message["metadata"]["answer_routing"]["source"] == "query_plan"
    assert "Risk concentration" in message["content"]
    assert "Margin" in message["content"]
    assert "## 总体判断" not in message["content"]
    assert "核心图表指标整体上行" not in message["content"]
    assert message["metadata"]["negative_assessment"]["signal_count"] == 2


def test_execution_risk_question_returns_explanation_not_retrieval_dump(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_pipeline_document(service)
    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(conversation["id"], "所谓的执行风险具体是指什么？", "ws_demo", "user_demo")

    service.process_next_run("execution-risk-explanation-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])

    assert message["metadata"]["query_plan"]["intent"] == "risk_explanation"
    assert "## 直接解释" in message["content"]
    assert "既定经营目标和优先事项在落地过程中偏离计划" in message["content"]
    assert "Risk concentration" in message["content"]
    assert "与问题直接相关的文档证据如下" not in message["content"]
    assert "illustrative synthetic test data" not in message["content"]
    assert message["metadata"]["negative_assessment"]["signal_count"] >= 1


def test_strength_question_runs_multiroute_retrieval_and_structured_evaluation(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_strength_document(service)
    plan = deterministic_plan("公司的优势在哪些点上", [document_id])

    retrieval = EvidenceRetriever(service.db).search_plan(plan, "ws_demo", [document_id])
    assert retrieval.diagnostics["query_count"] > 1
    assert retrieval.diagnostics["unique_candidates"] >= 2

    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(conversation["id"], "公司的优势在哪些点上", "ws_demo", "user_demo")
    service.process_next_run("strength-query-pipeline-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])

    assert "公司的优势主要体现在" in message["content"]
    assert "VONB" in message["content"]
    assert "Net FSG" in message["content"]
    assert "Largest market share" in message["content"]
    assert "Shareholder capital ratio" in message["content"]
    assert "没有足够" not in message["content"]
    assert message["metadata"]["query_plan"]["intent"] == "business_evaluation"
    assert message["metadata"]["query_plan"]["evaluation_polarity"] == "positive"
    assert message["metadata"]["pipeline_version"] == "planned-evidence-v2"
    assert message["metadata"]["evaluation_assessment"]["signal_count"] >= 3
    assert message["metadata"]["evaluation_assessment"]["caveat_count"] == 1
    assert {citation["slide_no"] for citation in message["citations"]} >= {1, 2, 3}


def test_composite_business_question_combines_summary_strength_and_downside_routes(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    document_id = _seed_strength_document(service)
    conversation = service.create_conversation("ws_demo", "user_demo", [document_id])
    queued = service.ask(conversation["id"], "请总结公司的优势和潜在问题。", "ws_demo", "user_demo")

    service.process_next_run("composite-business-task-frame-test")
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    message = next(item for item in result["messages"] if item["id"] == queued["assistant_message_id"])

    assert message["metadata"]["query_plan"]["active_intents"] == [
        "business_evaluation",
        "negative_signal_summary",
        "summary",
    ]
    assert message["metadata"]["answer_routing"]["strategy"] == "composite"
    assert "## 优势与经营评价" in message["content"]
    assert "## 潜在问题与风险" in message["content"]
    assert "## 文档概览" in message["content"]
    assert "VONB" in message["content"]
    assert message["citations"]


def test_query_planning_benchmark_contract() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_query_pipeline/cases.json").read_text(encoding="utf-8"))

    assert dataset["dataset"] == "qbr-query-pipeline"
    assert len(dataset["cases"]) >= 20
    for case in dataset["cases"]:
        plan = deterministic_plan(case["question"])
        query_corpus = " ".join(item.text for item in plan.retrieval_queries).casefold()
        assert plan.intent == case["expected_intent"], case["id"]
        if "expected_evaluation_polarity" in case:
            assert plan.evaluation_polarity == case["expected_evaluation_polarity"], case["id"]
        if "expected_secondary_intents" in case:
            assert list(plan.secondary_intents) == case["expected_secondary_intents"], case["id"]
        if "required_operations" in case:
            assert all(operation in plan.operations for operation in case["required_operations"]), case["id"]
        assert plan.answer_language == case["expected_language"], case["id"]
        assert plan.retrieval_queries[0].text == case["question"], case["id"]
        assert all(term.casefold() in query_corpus for term in case.get("required_query_terms", [])), case["id"]
        assert all(role in plan.excluded_content_roles for role in case.get("excluded_roles", [])), case["id"]
        assert list(plan.hard_constraints) == case.get("hard_constraints", []), case["id"]
