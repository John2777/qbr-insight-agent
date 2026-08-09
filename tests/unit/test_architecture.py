from __future__ import annotations

from pathlib import Path

import pytest

from packages.qbr_core import DeterministicAnswerEngine, QAApplicationService, QBRService, Settings
from packages.qbr_core.lease import LeaseCoordinator, LeasePolicy


def test_composition_root_wires_explicit_application_boundaries(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))

    assert isinstance(service.qa_service, QAApplicationService)
    assert isinstance(service.qa_service.answer_engine, DeterministicAnswerEngine)
    assert isinstance(service.leases, LeaseCoordinator)
    assert service.qa_service.db is service.db
    assert service.qa_service.retriever is service.retriever
    assert service.qa_service.answer_engine.db is service.db
    assert service.qa_service.answer_engine.retriever is service.retriever
    assert service.qa_service.leases is service.leases


def test_lease_policy_rejects_unsafe_heartbeat_timing() -> None:
    with pytest.raises(ValueError, match="shorter than lease_seconds"):
        LeasePolicy(lease_seconds=30, heartbeat_seconds=30)


def test_facade_preserves_public_qa_contract(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    conversation = service.create_conversation("ws_demo", "user_demo")
    queued = service.ask(conversation["id"], "没有文档时能否回答？", "ws_demo", "user_demo")

    assert service.process_next_run("architecture-test") == queued["run_id"]
    result = service.get_run(queued["run_id"], "ws_demo")
    assert result["status"] == "completed"
    assert result["warnings"] == ["INSUFFICIENT_EVIDENCE"]
