from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

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
    ]

    pack = EvidencePackBuilder().build(plan, candidates)
    quotes = "\n".join(atom.quote for atom in pack.atoms)

    assert pack.answerable is True
    assert "[Sources]" not in quotes
    assert "RISK CONCENTRATION" not in quotes
    assert "Capital ratio" not in quotes
    assert "Risk capital allocation" not in quotes
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
    assert message["metadata"]["pipeline_version"] == "planned-evidence-v1"
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
