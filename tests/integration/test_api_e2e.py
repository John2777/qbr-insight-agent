from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.qbr_core import Settings


def test_upload_ingest_query_citation_and_delete(tmp_path: Path, synthetic_pptx: Path) -> None:
    settings = Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False)
    app = create_app(settings)
    with TestClient(app) as client:
        with synthetic_pptx.open("rb") as source:
            upload = client.post(
                "/api/v1/documents",
                files={"file": ("FY25-QBR.pptx", source, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
                data={"deduplication": "new_version", "metadata": '{"quarter":"2025-Q4"}'},
            )
        assert upload.status_code == 202, upload.text
        resources = upload.json()
        assert app.state.service.process_next_job("test-worker") == resources["job"]["id"]

        job = client.get(f"/api/v1/jobs/{resources['job']['id']}")
        assert job.status_code == 200
        assert job.json()["status"] in {"ready", "partial"}
        events = client.get(f"/api/v1/jobs/{resources['job']['id']}/events")
        assert "event: completed" in events.text

        slides = client.get(f"/api/v1/document-versions/{resources['version']['id']}/slides").json()["items"]
        assert len(slides) == 2
        slide = client.get(f"/api/v1/slides/{slides[0]['id']}").json()
        assert slide["charts"][0]["series"][0]["points"][1]["y_value"] == 20
        preview = client.get(slide["preview_url"])
        thumbnail = client.get(slide["thumbnail_url"])
        assert preview.status_code == 200
        assert thumbnail.status_code == 200
        assert "immutable" in preview.headers["cache-control"]
        assert "immutable" in thumbnail.headers["cache-control"]

        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Revenue check", "document_ids": [resources["document"]["id"]]},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "Q2 Revenue 是多少？"},
        )
        assert sent.status_code == 202
        history = client.get("/api/v1/conversations?limit=10")
        assert history.status_code == 200
        assert history.json()["items"][0]["id"] == conversation["id"]
        assert history.json()["items"][0]["last_question"] == "Q2 Revenue 是多少？"
        assert history.json()["items"][0]["message_count"] == 2
        assert app.state.service.process_next_run("test-worker") == sent.json()["run_id"]
        run = client.get(f"/api/v1/runs/{sent.json()['run_id']}").json()
        assert "20" in run["message"]["content"]
        assert run["citations"][0]["slide_no"] == 1
        assert run["citations"][0]["source_kind"] == "embedded_workbook"
        assert run["citations"][0]["bbox"] == {"x": 100 / 12192000, "y": 200 / 6858000, "w": 800 / 12192000, "h": 500 / 6858000}

        assert client.post(
            f"/api/v1/messages/{sent.json()['assistant_message_id']}/feedback",
            json={"rating": 1, "category": "accurate"},
        ).status_code == 201
        conversation_deleted = client.delete(f"/api/v1/conversations/{conversation['id']}")
        assert conversation_deleted.status_code == 204
        assert client.get(f"/api/v1/conversations/{conversation['id']}").status_code == 404
        assert client.get("/api/v1/conversations?limit=10").json()["items"] == []
        with app.state.service.db.read() as conn:
            related_tables = ("conversations", "messages", "runs", "run_events", "citations", "feedback")
            assert {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in related_tables} == {
                table: 0 for table in related_tables
            }
            assert conn.execute(
                "SELECT count(*) FROM audit_events WHERE action='conversation.delete' AND target_id=?",
                (conversation["id"],),
            ).fetchone()[0] == 1

        deleted = client.delete(f"/api/v1/documents/{resources['document']['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/documents/{resources['document']['id']}").status_code == 404


def test_cross_workspace_ids_are_not_enumerable(tmp_path: Path, synthetic_pptx: Path) -> None:
    app = create_app(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/documents/doc_unknown",
            headers={"X-Workspace-ID": "ws_other", "X-User-ID": "user_demo"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_multi_part_coverage_is_exposed_end_to_end_and_not_hidden_by_calculation(
    tmp_path: Path,
    synthetic_pptx: Path,
) -> None:
    app = create_app(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    with TestClient(app) as client:
        with synthetic_pptx.open("rb") as source:
            uploaded = client.post(
                "/api/v1/documents",
                files={
                    "file": (
                        "coverage-qbr.pptx",
                        source,
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            ).json()
        assert app.state.service.process_next_job("coverage-worker") == uploaded["job"]["id"]

        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Coverage check", "document_ids": [uploaded["document"]["id"]]},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "How much did Revenue and Margin grow? Which product lines drive growth?"},
        ).json()
        assert app.state.service.process_next_run("coverage-worker") == sent["run_id"]

        run = client.get(f"/api/v1/runs/{sent['run_id']}").json()
        coverage = run["message"]["metadata"]["evidence_pack"]["coverage"]
        assert "PARTIAL_EVIDENCE_COVERAGE" in run["warnings"]
        assert coverage["total"] == 3
        assert coverage["supported"] == 2
        assert coverage["gap_labels"] == ["Which product lines drive growth"]
        assert coverage["facets"][-1]["status"] == "unsupported"
        assert "The current sources do not yet support: Which product lines drive growth" in run["message"]["content"]

        recent = client.get("/api/v1/analytics/summary").json()["recent_runs"][0]
        assert recent["id"] == sent["run_id"]
        assert recent["evidence_coverage"] == coverage
        assert recent["warning_details"][0]["label"] == "Some details lack source support"


def test_document_purge_removes_derived_data_and_is_idempotent(tmp_path: Path, synthetic_pptx: Path) -> None:
    settings = Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False)
    app = create_app(settings)
    with TestClient(app) as client:
        with synthetic_pptx.open("rb") as source:
            uploaded = client.post(
                "/api/v1/documents",
                files={
                    "file": (
                        "purge-me.pptx",
                        source,
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            ).json()
        assert app.state.service.process_next_job("purge-test-worker") == uploaded["job"]["id"]

        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Delete this conversation", "document_ids": [uploaded["document"]["id"]]},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "Q2 Revenue 是多少？"},
        ).json()
        assert app.state.service.process_next_run("purge-test-worker") == sent["run_id"]
        assert client.post(
            f"/api/v1/messages/{sent['assistant_message_id']}/feedback",
            json={"rating": 1, "category": "accurate", "comment": "temporary"},
        ).status_code == 201

        version_dir = settings.object_dir / "ws_demo" / uploaded["version"]["id"]
        assert version_dir.is_dir()
        first = client.delete(f"/api/v1/documents/{uploaded['document']['id']}/purge")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "completed"
        assert first.json()["already_purged"] is False
        assert not version_dir.exists()

        second = client.delete(f"/api/v1/documents/{uploaded['document']['id']}/purge")
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "completed"
        assert second.json()["already_purged"] is True
        assert client.get(f"/api/v1/documents/{uploaded['document']['id']}").status_code == 404
        assert client.get(f"/api/v1/conversations/{conversation['id']}").status_code == 404

        with app.state.service.db.read() as conn:
            emptied_tables = (
                "documents",
                "document_versions",
                "parser_runs",
                "ingestion_jobs",
                "job_events",
                "slides",
                "elements",
                "charts",
                "chart_series",
                "chart_points",
                "chunks",
                "chunk_fts",
                "chunk_embeddings",
                "review_tasks",
                "review_revisions",
                "conversations",
                "messages",
                "runs",
                "run_events",
                "citations",
                "feedback",
            )
            assert {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in emptied_tables} == {
                table: 0 for table in emptied_tables
            }
            purge = conn.execute(
                "SELECT status,version_ids_json FROM document_purges WHERE workspace_id='ws_demo' AND document_id=?",
                (uploaded["document"]["id"],),
            ).fetchone()
            assert purge["status"] == "completed"
            assert purge["version_ids_json"] == "[]"
            assert conn.execute(
                "SELECT count(*) FROM audit_events WHERE action='document.purge' AND target_id=?",
                (uploaded["document"]["id"],),
            ).fetchone()[0] == 1
