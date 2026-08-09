from __future__ import annotations

from pathlib import Path

import pytest

from packages.qbr_core import DeterministicAnswerEngine, QAApplicationService, QBRService, Settings
from packages.qbr_core.lease import LeaseCoordinator, LeasePolicy

ROOT = Path(__file__).parents[2]


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


@pytest.mark.parametrize(
    "relative_path",
    ["deploy/docker/nginx.conf", "cloud-deployment-runbook/templates/nginx-qbr.conf"],
)
def test_public_proxy_accepts_the_document_upload_limit(relative_path: str) -> None:
    config = (ROOT / relative_path).read_text(encoding="utf-8")

    # The application accepts a 10 MiB file, so the HTTP body limit must also
    # allow the small amount of multipart framing around that file.
    assert "client_max_body_size 11m;" in config


def test_upload_limit_defaults_are_consistent() -> None:
    settings = Settings(Path("data"), Path("data/app.sqlite3"), Path("data/objects"))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert settings.max_upload_mib == 10
    assert "MAX_UPLOAD_MIB: ${MAX_UPLOAD_MIB:-10}" in compose
