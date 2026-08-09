from __future__ import annotations

from pathlib import Path

import pytest

from packages.qbr_core import DeterministicAnswerEngine, QAApplicationService, QBRService, Settings
from packages.qbr_core.errors import Conflict, ResourceNotFound
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


def test_conversation_history_is_user_scoped_and_orders_recent_activity_first(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    older = service.create_conversation("ws_demo", "user_demo", title="Older question")
    newer = service.create_conversation("ws_demo", "user_demo", title="Latest question")
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO users(id,email,display_name,created_at) VALUES (?,?,?,?)",
            ("another_user", "another@example.com", "Another User", newer["created_at"]),
        )
    service.create_conversation("ws_demo", "another_user", title="Private question")
    queued = service.ask(newer["id"], "最新的问题内容", "ws_demo", "user_demo")

    items = service.list_conversations("ws_demo", "user_demo", limit=1)

    assert [item["id"] for item in items] == [newer["id"]]
    assert items[0]["last_question"] == "最新的问题内容"
    assert items[0]["message_count"] == 2
    assert items[0]["scope"] == {"document_ids": []}
    assert older["id"] != items[0]["id"]
    assert queued["run_id"]


def test_conversation_deletion_is_user_scoped_and_rejects_active_runs(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    conversation = service.create_conversation("ws_demo", "user_demo", title="Delete me")
    queued = service.ask(conversation["id"], "仍在生成的问题", "ws_demo", "user_demo")
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO users(id,email,display_name,created_at) VALUES (?,?,?,?)",
            ("another_user", "another@example.com", "Another User", conversation["created_at"]),
        )
    private = service.create_conversation("ws_demo", "another_user", title="Private")

    with pytest.raises(Conflict, match="still generating"):
        service.delete_conversation(conversation["id"], "ws_demo", "user_demo")
    with pytest.raises(ResourceNotFound):
        service.delete_conversation(private["id"], "ws_demo", "user_demo")

    with service.db.transaction(immediate=True) as conn:
        conn.execute("UPDATE runs SET status='failed' WHERE id=?", (queued["run_id"],))
    assert [run["id"] for run in service.analytics_summary("ws_demo")["recent_runs"]] == [queued["run_id"]]
    service.delete_conversation(conversation["id"], "ws_demo", "user_demo")

    with pytest.raises(ResourceNotFound):
        service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    assert service.analytics_summary("ws_demo")["recent_runs"] == []
    assert service.analytics_summary("ws_demo")["runs"]["total"] == 0
    assert service.get_conversation(private["id"], "ws_demo", "another_user")["title"] == "Private"


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
