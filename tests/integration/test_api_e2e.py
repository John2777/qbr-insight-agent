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
        assert client.get(slide["preview_url"]).status_code == 200

        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Revenue check", "document_ids": [resources["document"]["id"]]},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "Q2 Revenue 是多少？"},
        )
        assert sent.status_code == 202
        assert app.state.service.process_next_run("test-worker") == sent.json()["run_id"]
        run = client.get(f"/api/v1/runs/{sent.json()['run_id']}").json()
        assert "20" in run["message"]["content"]
        assert run["citations"][0]["slide_no"] == 1
        assert run["citations"][0]["source_kind"] == "embedded_workbook"
        assert run["citations"][0]["bbox"] == {"x": 100 / 12192000, "y": 200 / 6858000, "w": 800 / 12192000, "h": 500 / 6858000}

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
