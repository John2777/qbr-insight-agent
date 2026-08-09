from __future__ import annotations

from pathlib import Path

from packages.qbr_core import QBRService, Settings


def service_at(root: Path) -> QBRService:
    return QBRService(Settings(root, root / "app.sqlite3", root / "objects", run_inline_worker=False))


def test_deduplication_reuses_existing_document(tmp_path: Path, synthetic_pptx: Path) -> None:
    service = service_at(tmp_path)
    first = service.import_document(
        synthetic_pptx,
        filename="one.pptx",
        title="One",
        metadata={},
        deduplication="reuse",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    duplicate = tmp_path / "duplicate.pptx"
    stored = next((service.settings.object_dir / "ws_demo" / first["version"]["id"]).glob("*.pptx"))
    duplicate.write_bytes(stored.read_bytes())
    second = service.import_document(
        duplicate,
        filename="two.pptx",
        title="Two",
        metadata={},
        deduplication="reuse",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    assert second["reused"] is True
    assert second["document"]["id"] == first["document"]["id"]


def test_growth_answer_is_replayable(tmp_path: Path, synthetic_pptx: Path) -> None:
    service = service_at(tmp_path)
    uploaded = service.import_document(
        synthetic_pptx,
        filename="qbr.pptx",
        title="QBR",
        metadata={},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    assert service.process_next_job() == uploaded["job"]["id"]
    conversation = service.create_conversation("ws_demo", "user_demo", [uploaded["document"]["id"]])
    sent = service.ask(conversation["id"], "Revenue 从 Q1 到 Q3 增长多少？", "ws_demo", "user_demo")
    assert service.process_next_run() == sent["run_id"]
    run = service.get_run(sent["run_id"], "ws_demo")
    assert "200.0%" in run["message"]["content"]
    assert [citation["quote"].split(":")[-1].strip() for citation in run["citations"]] == ["Q1=10.0", "Q3=30.0"]


def test_broad_performance_question_summarizes_current_qbr(tmp_path: Path, synthetic_pptx: Path) -> None:
    service = service_at(tmp_path)
    uploaded = service.import_document(
        synthetic_pptx,
        filename="qbr.pptx",
        title="QBR",
        metadata={},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    assert service.process_next_job() == uploaded["job"]["id"]
    conversation = service.create_conversation("ws_demo", "user_demo", [uploaded["document"]["id"]])

    sent = service.ask(conversation["id"], "当前文档里体现的业绩情况如何", "ws_demo", "user_demo")
    assert service.process_next_run() == sent["run_id"]
    run = service.get_run(sent["run_id"], "ws_demo")

    assert "没有足够证据" not in run["message"]["content"]
    assert "## 总体判断" in run["message"]["content"]
    assert "## 关键趋势" in run["message"]["content"]
    assert "Revenue" in run["message"]["content"]
    assert run["citations"]
    assert "INSUFFICIENT_EVIDENCE" not in run["warnings"]


def test_pending_job_can_be_cancelled_and_retried(tmp_path: Path, synthetic_pptx: Path) -> None:
    service = service_at(tmp_path)
    uploaded = service.import_document(
        synthetic_pptx,
        filename="qbr.pptx",
        title="QBR",
        metadata={},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    job_id = uploaded["job"]["id"]
    assert service.cancel_job(job_id, "ws_demo")["status"] == "cancelled"
    assert service.process_next_job() is None
    assert service.retry_job(job_id, "ws_demo")["status"] == "pending"
    assert service.process_next_job() == job_id
    assert service.get_job(job_id, "ws_demo")["status"] in {"ready", "partial"}
