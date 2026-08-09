from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.qbr_core import QBRService, Settings
from packages.qbr_core.auth import create_hs256_token, create_password_hash, verify_password
from packages.qbr_core.db import utc_now
from packages.qbr_core.ids import new_id
from packages.qbr_core.retrieval import fts_query


def _ready_service(root: Path, presentation: Path) -> tuple[QBRService, dict[str, object]]:
    service = QBRService(Settings(root, root / "app.sqlite3", root / "objects", run_inline_worker=False))
    uploaded = service.import_document(
        presentation,
        filename="qbr.pptx",
        title="QBR",
        metadata={},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    assert service.process_next_job("test-worker") == uploaded["job"]["id"]
    return service, uploaded


def test_question_is_queued_then_completed_by_worker(tmp_path: Path, synthetic_pptx: Path) -> None:
    service, uploaded = _ready_service(tmp_path, synthetic_pptx)
    conversation = service.create_conversation("ws_demo", "user_demo", [uploaded["document"]["id"]])
    sent = service.ask(conversation["id"], "Q2 Revenue 是多少？", "ws_demo", "user_demo", "client-1")
    duplicate = service.ask(conversation["id"], "Q2 Revenue 是多少？", "ws_demo", "user_demo", "client-1")
    assert duplicate["run_id"] == sent["run_id"]
    assert duplicate["reused"] is True

    pending = service.get_run(sent["run_id"], "ws_demo")
    assert pending["status"] == "pending"
    assert pending["message"]["content"] == ""
    assert service.run_events(sent["run_id"], "ws_demo")[0]["event"] == "queued"

    assert service.process_next_run("test-worker") == sent["run_id"]
    completed = service.get_run(sent["run_id"], "ws_demo")
    assert completed["status"] == "completed"
    assert "20" in completed["message"]["content"]
    assert service.run_events(sent["run_id"], "ws_demo")[-1]["event"] == "completed"


def test_fts_query_is_safe_and_searchable(tmp_path: Path, synthetic_pptx: Path) -> None:
    service, uploaded = _ready_service(tmp_path, synthetic_pptx)
    for question in ["Quarterly metrics", "Revenue Q2"]:
        result = service.retriever.search(question, "ws_demo", [uploaded["document"]["id"]])
        assert result.items
        assert result.strategy.startswith("fts5")


@pytest.mark.parametrize("question", ["营收是多少？", 'Revenue OR "*"', "Q1/Q2 growth"])
def test_fts_query_removes_operators(question: str) -> None:
    assert "*" not in fts_query(question)


def test_review_resolution_creates_revision_and_rebuilds_chart(tmp_path: Path, synthetic_pptx: Path) -> None:
    service, _ = _ready_service(tmp_path, synthetic_pptx)
    with service.db.transaction(immediate=True) as conn:
        row = conn.execute(
            """SELECT e.id element_id,e.structured_json FROM elements e JOIN charts c ON c.element_id=e.id
               ORDER BY e.id LIMIT 1"""
        ).fetchone()
        assert row
        review_id = new_id("review")
        conn.execute(
            """INSERT INTO review_tasks(
                 id,workspace_id,element_id,status,reason,original_json,created_at
               ) VALUES (?,? ,?,'pending','MANUAL_QA',?,?)""",
            (review_id, "ws_demo", row["element_id"], row["structured_json"], utc_now()),
        )
        corrected = json.loads(row["structured_json"])

    service.claim_review(review_id, "ws_demo", "user_demo")
    corrected["series"][0]["points"][0]["value"] = 99
    resolved = service.resolve_review(review_id, "ws_demo", "user_demo", corrected)
    assert resolved["status"] == "resolved"

    with service.db.read() as conn:
        point = conn.execute(
            """SELECT cp.y_value FROM chart_points cp JOIN chart_series cs ON cs.id=cp.series_id
               JOIN charts c ON c.id=cs.chart_id WHERE c.element_id=? ORDER BY cp.point_order LIMIT 1""",
            (row["element_id"],),
        ).fetchone()
        revisions = conn.execute("SELECT count(*) FROM review_revisions WHERE review_task_id=?", (review_id,)).fetchone()[0]
    assert point["y_value"] == 99
    assert revisions == 1
    assert service.analytics_summary("ws_demo")["reviews"]["resolved"] == 1


def test_jwt_mode_is_fail_closed_and_rbac_is_enforced(tmp_path: Path) -> None:
    secret = "test-secret-that-is-longer-than-thirty-two-bytes"
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        run_inline_worker=False,
        auth_mode="jwt",
        jwt_secret=secret,
    )
    app = create_app(settings)
    with app.state.service.db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE workspace_members SET role='viewer' WHERE workspace_id='ws_demo' AND user_id='user_demo'"
        )
    token = create_hs256_token(
        settings,
        user_id="user_demo",
        workspace_id="ws_demo",
        roles=["viewer"],
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/documents").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/documents", headers=headers).status_code == 200
        denied = client.post("/api/v1/jobs/job_unknown/retry", headers=headers)
        assert denied.status_code == 403


def test_production_rejects_demo_auth(tmp_path: Path) -> None:
    settings = Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", app_env="production")
    with pytest.raises(ValueError, match="forbidden"):
        create_app(settings)


def test_password_login_uses_httponly_cookie_and_logout(tmp_path: Path) -> None:
    password = "correct horse battery staple"
    encoded = create_password_hash(password, salt=b"0123456789abcdef")
    assert verify_password(password, encoded)
    assert not verify_password("incorrect password", encoded)
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        run_inline_worker=False,
        auth_mode="password",
        jwt_secret="test-secret-that-is-longer-than-thirty-two-bytes",
        password_username="interviewer",
        password_hash=encoded,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/session").status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "interviewer", "password": password},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert client.get("/api/v1/auth/session").status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/auth/session").status_code == 401


def test_password_login_is_rate_limited(tmp_path: Path) -> None:
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        run_inline_worker=False,
        auth_mode="password",
        jwt_secret="test-secret-that-is-longer-than-thirty-two-bytes",
        password_username="interviewer",
        password_hash=create_password_hash("correct horse battery staple", salt=b"0123456789abcdef"),
        login_max_attempts=2,
        login_window_seconds=60,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        payload = {"username": "interviewer", "password": "definitely the wrong password"}
        assert client.post("/api/v1/auth/login", json=payload).status_code == 401
        assert client.post("/api/v1/auth/login", json=payload).status_code == 401
        limited = client.post("/api/v1/auth/login", json=payload)
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
