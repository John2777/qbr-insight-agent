from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    assert "relative change=200.0%" in run["message"]["content"]
    assert [citation["quote"].split(":")[-1].strip() for citation in run["citations"][:2]] == ["Q1=10", "Q3=30"]


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

    assert "没有检索到" not in run["message"]["content"]
    assert "Revenue" in run["message"]["content"]
    assert "文档中的核心图表指标" not in run["message"]["content"]
    assert "## 总体判断" not in run["message"]["content"]
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


def test_purge_retry_resumes_after_index_cleanup_failure(tmp_path: Path, synthetic_pptx: Path) -> None:
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
    attempts = 0

    def flaky_rebuild(workspace_id: str) -> None:
        nonlocal attempts
        assert workspace_id == "ws_demo"
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary index failure")

    service.document_purges.rebuild_workspace_index = flaky_rebuild
    first = service.purge_document(uploaded["document"]["id"], "ws_demo", "user_demo")
    assert first["status"] == "partial"
    assert first["already_purged"] is False

    second = service.purge_document(uploaded["document"]["id"], "ws_demo", "user_demo")
    assert second["status"] == "completed"
    assert second["resumed"] is True
    assert attempts == 2

    third = service.purge_document(uploaded["document"]["id"], "ws_demo", "user_demo")
    assert third["status"] == "completed"
    assert third["already_purged"] is True
    assert attempts == 2


def test_concurrent_purge_requests_execute_external_cleanup_once(tmp_path: Path, synthetic_pptx: Path) -> None:
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
    attempts = 0
    attempts_lock = threading.Lock()

    def counted_rebuild(workspace_id: str) -> None:
        nonlocal attempts
        assert workspace_id == "ws_demo"
        with attempts_lock:
            attempts += 1
        time.sleep(0.1)

    service.document_purges.rebuild_workspace_index = counted_rebuild
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.purge_document,
                uploaded["document"]["id"],
                "ws_demo",
                "user_demo",
            )
            for _ in range(2)
        ]
    results = [future.result() for future in futures]

    assert attempts == 1
    assert sorted(result["already_purged"] for result in results) == [False, True]
    assert {result["status"] for result in results} == {"completed"}
