from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.db import utc_now
from packages.qbr_core.evidence import EvidencePack, EvidencePackBuilder, extract_relevant_quote
from packages.qbr_core.negative_analysis import NegativeSignalAnalyzer
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


def test_negative_question_plan_is_bilingual_and_excludes_source_notes() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    queries = " ".join(item.text for item in plan.retrieval_queries).casefold()

    assert plan.intent == "negative_signal_summary"
    assert plan.execution_profile == "deep"
    assert plan.retrieval_queries[0].text == "what is the bad news in this ppt"
    assert "risk concentration" in queries
    assert "风险" in queries
    assert "provenance" in plan.excluded_content_roles


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


def test_planner_provider_failure_has_deterministic_fallback() -> None:
    plan = QueryPlannerAgent(PlannerModel(RuntimeError("offline"))).plan("主要风险是什么？")

    assert plan.intent == "negative_signal_summary"
    assert plan.planner == "deterministic_fallback"
    assert plan.warnings == ("QUERY_PLANNER_PROVIDER_ERROR",)


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


def test_negative_analyzer_derives_trends_and_distinguishes_green_gates() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "financial-table",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s6",
            "slide_no": 6,
            "element_id": "e6",
            "chunk_type": "table",
            "content": (
                "核心财务指标 | 2023A | 2024A | 2025A | 同比/变化 | 单位\nShareholder capital ratio | 269% | 236% | 221% | -15ppt | %"
            ),
        },
        {
            "id": "green-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s7",
            "slide_no": 7,
            "element_id": "e7",
            "chunk_type": "table",
            "content": ("风险闸门 | 绿 | 黄 | 红 | 当前\nVONB增速 | >12% | 5-12% | <5% | 15%\n资本比率 | >210% | 190-210% | <190% | 221%"),
        },
        {
            "id": "management-action",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s7",
            "slide_no": 7,
            "element_id": "e8",
            "chunk_type": "text",
            "content": "强化高净值、保障与跨境服务，控制产品集中度。",
        },
    ]
    chart_rows = []
    for series_id, series_name, previous, current in (
        ("digital-stp", "Digital STP", 73.8, 72.1),
        ("persistency", "Persistency", 90.8, 90.1),
        ("other-markets", "其他市场", 134.0, 129.0),
    ):
        for point_order, category, value in ((1, "26/02", previous), (2, "26/03", current)):
            chart_rows.append(
                {
                    "series_id": series_id,
                    "series_name": series_name,
                    "point_order": point_order,
                    "category": category,
                    "y_value": value,
                    "display_value": str(value),
                    "document_version_id": "dv",
                    "document_id": "doc",
                    "slide_id": "s3",
                    "slide_no": 3,
                    "element_id": "shared-chart-element",
                    "chart_title": "Monthly KPI trends",
                }
            )

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=chart_rows)
    answer, evidence, warnings = assessment.render(plan)

    assert assessment.explicit_red_flags is False
    assert "No red/amber threshold breach" in answer
    assert "236%" in answer and "221%" in answer and "15 percentage points" in answer
    assert "Digital STP" in answer and "Persistency" in answer
    assert "其他市场" not in answer
    assert "控制产品集中度" in answer
    assert "enough relevant business evidence" not in answer
    assert {item["analysis_method"] for item in evidence} == {
        "structured_table_trend",
        "structured_chart_trend",
        "management_action_inference",
    }
    assert warnings == []


def test_negative_analyzer_reports_no_red_flags_instead_of_insufficient_evidence() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "green-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "table",
            "content": "Risk gate | Green | Amber | Red | Current\nCapital ratio | >210% | 190-210% | <190% | 221%",
        }
    ]

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert answer.startswith("No explicit negative result or breached threshold")
    assert "misleading to manufacture bad news" in answer
    assert len(evidence) == 1
    assert warnings == []


def test_negative_analyzer_distinguishes_no_adverse_signal_from_no_business_content() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "positive-business-content",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "text",
            "content": "Revenue growth remained above target and the capital ratio stayed within the green range.",
        }
    ]

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert answer.startswith("No explicit negative statement")
    assert "does not support a specific bad-news claim" in answer
    assert "enough relevant business evidence" not in answer
    assert evidence == []
    assert warnings == []


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
    assert message["metadata"]["pipeline_version"] == "planned-evidence-v1"
    assert message["metadata"]["negative_assessment"]["signal_count"] == 2
    assert {citation["slide_no"] for citation in message["citations"]} == {1, 2}


def test_query_planning_benchmark_contract() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_query_pipeline/cases.json").read_text(encoding="utf-8"))

    assert dataset["dataset"] == "qbr-query-pipeline"
    assert len(dataset["cases"]) >= 20
    for case in dataset["cases"]:
        plan = deterministic_plan(case["question"])
        query_corpus = " ".join(item.text for item in plan.retrieval_queries).casefold()
        assert plan.intent == case["expected_intent"], case["id"]
        assert plan.answer_language == case["expected_language"], case["id"]
        assert plan.retrieval_queries[0].text == case["question"], case["id"]
        assert all(term.casefold() in query_corpus for term in case.get("required_query_terms", [])), case["id"]
        assert all(role in plan.excluded_content_roles for role in case.get("excluded_roles", [])), case["id"]
        assert list(plan.hard_constraints) == case.get("hard_constraints", []), case["id"]
