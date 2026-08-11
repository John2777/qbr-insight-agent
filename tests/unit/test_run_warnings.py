from __future__ import annotations

from pathlib import Path

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.application.warnings import describe_warning, has_degraded_warning, warning_details


def test_warning_catalog_separates_data_caveats_from_provider_fallbacks() -> None:
    synthetic = describe_warning("SYNTHETIC_DATA_SIGNAL")
    planner = describe_warning("QUERY_PLANNER_PROVIDER_ERROR")
    truncated = describe_warning("LLM_OUTPUT_TRUNCATED")

    assert synthetic.severity == "info"
    assert synthetic.category == "data_caveat"
    assert planner.severity == "degraded"
    assert planner.category == "provider_fallback"
    assert truncated.severity == "degraded"
    assert truncated.category == "answer_fallback"
    assert has_degraded_warning([synthetic.code, planner.code])


def test_partial_source_support_notice_describes_a_document_gap_not_a_system_error() -> None:
    detail = warning_details(["PARTIAL_EVIDENCE_COVERAGE"], language="en")[0]

    assert detail["label"] == "Some details lack source support"
    assert detail["description"] == (
        "Other parts of the answer remain supported; the current documents do not yet provide the information needed "
        "for some requested details."
    )

    chinese = warning_details(["PARTIAL_EVIDENCE_COVERAGE"], language="zh")[0]
    assert chinese["label"] == "部分内容暂无资料支持"
    assert chinese["description"] == "回答中的其余内容仍有资料支持；当前文档暂未提供问题中部分内容所需的信息。"


def test_unknown_warning_remains_a_diagnostic_without_becoming_a_raw_ui_error() -> None:
    detail = warning_details(["NEW_DIAGNOSTIC"])[0]

    assert detail == {
        "code": "NEW_DIAGNOSTIC",
        "severity": "warning",
        "category": "uncategorized",
        "label": "Run notice",
        "description": "This run produced an unclassified diagnostic signal. Use the code to search the service logs.",
    }


def test_analytics_exposes_warning_details_and_degraded_run_count(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    conversation = service.create_conversation("ws_demo", "user_demo", title="Degraded answer")
    queued = service.ask(conversation["id"], "公司的优势在哪里", "ws_demo", "user_demo")
    warnings = ["QUERY_PLANNER_PROVIDER_ERROR", "SYNTHETIC_DATA_SIGNAL", "LLM_PROVIDER_ERROR"]
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE runs SET status='completed',warning_json=?,completed_at=created_at WHERE id=?",
            (service.db.json(warnings), queued["run_id"]),
        )

    summary = service.analytics_summary("ws_demo")

    assert summary["runs"]["degraded"] == 1
    assert summary["recent_runs"][0]["title"] == "Degraded answer"
    assert summary["recent_runs"][0]["question"] == "公司的优势在哪里"
    assert summary["recent_runs"][0]["warnings"] == warnings
    details = summary["recent_runs"][0]["warning_details"]
    assert [item["severity"] for item in details] == ["degraded", "info", "degraded"]
    assert [item["label"] for item in details] == [
        "Query planning degraded",
        "Document contains synthetic data",
        "Model generation degraded",
    ]


def test_analytics_shows_each_run_question_instead_of_repeating_the_conversation_title(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    conversation = service.create_conversation("ws_demo", "user_demo", title="Quarterly performance")
    first = service.ask(conversation["id"], "VONB 同比增长多少？", "ws_demo", "user_demo")
    second = service.ask(conversation["id"], "增长主要来自哪些业务板块？", "ws_demo", "user_demo")
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE runs SET created_at='2026-08-10T12:00:00.000Z' WHERE id IN (?,?)",
            (first["run_id"], second["run_id"]),
        )

    recent_runs = service.analytics_summary("ws_demo")["recent_runs"]

    assert [run["question"] for run in recent_runs] == [
        "增长主要来自哪些业务板块？",
        "VONB 同比增长多少？",
    ]
    assert {run["title"] for run in recent_runs} == {"Quarterly performance"}
