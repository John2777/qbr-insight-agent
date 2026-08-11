from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.qbr_core import Settings
from packages.qbr_core.documents.vision import VisualKnowledge


class FakeSlideVisionEnricher:
    model = "fake-vision"

    def enrich(self, image_path: Path) -> VisualKnowledge:
        assert image_path.exists()
        return VisualKnowledge(
            summary="Regional demand is concentrated in Southeast Asia and the priority market is highlighted.",
            ocr_text="Priority market",
            observations=("Southeast Asia receives the strongest visual emphasis.",),
            confidence=0.88,
            model=self.model,
        )


def test_visual_ingestion_to_retrieval_answer_is_auditable(tmp_path: Path, synthetic_pptx: Path) -> None:
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        run_inline_worker=False,
        vision_enrich_all_slides=True,
    )
    app = create_app(settings)
    app.state.service.vision_enricher = FakeSlideVisionEnricher()

    with TestClient(app) as client:
        with synthetic_pptx.open("rb") as source:
            uploaded = client.post(
                "/api/v1/documents",
                files={
                    "file": (
                        "visual-qbr.pptx",
                        source,
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            ).json()
        assert app.state.service.process_next_job("vision-e2e-worker") == uploaded["job"]["id"]

        with app.state.service.db.read() as conn:
            chunks = conn.execute(
                "SELECT chunk_type,content,metadata_json FROM chunks WHERE chunk_type LIKE '%visual%' ORDER BY chunk_type"
            ).fetchall()
        assert {row["chunk_type"] for row in chunks} == {
            "slide_visual_summary",
            "visual_observation",
            "visual_ocr",
        }
        assert all('"source_kind":"visual_model"' in row["metadata_json"] for row in chunks)

        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Visual retrieval", "document_ids": [uploaded["document"]["id"]]},
        ).json()
        queued = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "Where is regional demand concentrated?"},
        ).json()
        assert app.state.service.process_next_run("vision-e2e-worker") == queued["run_id"]
        run = client.get(f"/api/v1/runs/{queued['run_id']}").json()

    assert "Southeast Asia" in run["message"]["content"]
    assert any(citation["source_kind"] == "visual_model" for citation in run["citations"])
