from __future__ import annotations

from pathlib import Path

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.run_warnings import describe_warning, has_degraded_warning, warning_details


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


def test_unknown_warning_remains_a_diagnostic_without_becoming_a_raw_ui_error() -> None:
    detail = warning_details(["NEW_DIAGNOSTIC"])[0]

    assert detail == {
        "code": "NEW_DIAGNOSTIC",
        "severity": "warning",
        "category": "uncategorized",
        "label": "运行提示",
        "description": "该运行产生了尚未分类的诊断信号；请使用代码在服务日志中检索。",
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
    assert summary["recent_runs"][0]["warnings"] == warnings
    details = summary["recent_runs"][0]["warning_details"]
    assert [item["severity"] for item in details] == ["degraded", "info", "degraded"]
    assert [item["label"] for item in details] == ["查询规划已降级", "文档含模拟数据", "模型生成已降级"]
