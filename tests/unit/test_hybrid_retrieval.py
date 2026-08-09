from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.db import Database, utc_now
from packages.qbr_core.retrieval import EvidenceRetriever
from packages.qbr_core.vector import FaissVectorStore, HashingEmbeddingProvider


class RecordingVectorStore:
    backend_name = "test-vector"

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.indexed: list[tuple[str, str]] = []
        self.rebuilt: list[str] = []

    def search(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        del question, workspace_id, document_ids
        return [dict(row) for row in self.rows[:limit]]

    def index_document_version(self, workspace_id: str, document_version_id: str) -> None:
        self.indexed.append((workspace_id, document_version_id))

    def rebuild_workspace(self, workspace_id: str) -> None:
        self.rebuilt.append(workspace_id)


def test_rrf_promotes_a_chunk_found_by_both_retrievers(tmp_path: Path) -> None:
    retriever = EvidenceRetriever(Database(tmp_path / "unused.sqlite3"), rrf_k=60)
    lexical = [
        {"id": "lexical-only", "slide_no": 1, "retrieval_score": 8.0},
        {"id": "both", "slide_no": 2, "retrieval_score": 7.0},
    ]
    vector = [
        {"id": "both", "slide_no": 2, "vector_score": 0.91},
        {"id": "vector-only", "slide_no": 3, "vector_score": 0.88},
    ]

    fused = retriever._reciprocal_rank_fusion(lexical, vector, top_k=3)

    assert [row["id"] for row in fused] == ["both", "lexical-only", "vector-only"]
    assert fused[0]["retrieval_sources"] == ["lexical", "vector"]
    assert fused[0]["vector_score"] == pytest.approx(0.91)


def test_vector_lifecycle_calls_are_delegated(tmp_path: Path) -> None:
    store = RecordingVectorStore()
    retriever = EvidenceRetriever(Database(tmp_path / "unused.sqlite3"), mode="hybrid", vector_store=store)

    retriever.index_document_version("ws_1", "dv_1")
    retriever.rebuild_workspace("ws_1")

    assert store.indexed == [("ws_1", "dv_1")]
    assert store.rebuilt == ["ws_1"]
    assert retriever.vector_available is True
    assert retriever.vector_backend == "test-vector"


def test_settings_distinguish_semantic_and_offline_vector_configuration(tmp_path: Path) -> None:
    base = dict(data_dir=tmp_path, database_path=tmp_path / "db.sqlite3", object_dir=tmp_path / "objects")
    assert Settings(**base, retrieval_strategy="hybrid").vector_configured is False
    assert Settings(**base, retrieval_strategy="hybrid", embedding_provider="hashing").vector_configured is True
    assert Settings(
        **base,
        retrieval_strategy="hybrid",
        embedding_model="text-embedding-model",
        embedding_api_key="secret",
    ).vector_configured is True


def test_dual_axis_resolution_and_multi_series_answer(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    chart = {
        "axes": [
            {"axis_type": "valAx", "axis_id": "value-right", "position": "r", "title": "Margin", "number_format": "0%"},
            {"axis_type": "catAx", "axis_id": "category", "position": "b"},
        ]
    }
    resolved = service._series_axis_metadata(chart, {"axis_ids": ["value-right", "category"]})
    assert resolved["axis_id"] == "value-right"
    assert resolved["role"] == "secondary"

    common = {
        "point_order": 0,
        "category": "Q2",
        "chart_title": "Revenue and Margin",
        "document_title": "QBR",
        "chart_confidence": 1.0,
        "series_confidence": 1.0,
        "confidence": 1.0,
        "document_version_id": "dv_1",
        "slide_id": "slide_1",
        "element_id": "element_1",
        "bbox_json": "{}",
        "source_kind": "embedded_workbook",
    }
    rows = [
        {
            **common,
            "series_id": "revenue",
            "series_name": "Revenue",
            "y_value": 120.0,
            "display_value": "$120m",
            "unit": "$m",
            "visual_json": '{"axis":{"role":"primary","position":"l","title":"Revenue"}}',
        },
        {
            **common,
            "series_id": "margin",
            "series_name": "Margin",
            "y_value": 0.24,
            "display_value": "24%",
            "unit": "0%",
            "visual_json": '{"axis":{"role":"secondary","position":"r","title":"Margin"}}',
        },
    ]

    answer, evidence, warnings = service.qa_service.answer_engine._chart_answer(
        "Q2 Revenue 和 Margin 分别是多少？",
        service.qa_service.answer_engine._rank_chart_points("Q2 Revenue 和 Margin 分别是多少？", rows),
    )

    assert "$120m" in answer and "24%" in answer
    assert "左侧主轴" in answer and "右侧次轴" in answer
    assert len(evidence) == 2
    assert warnings == []


def _seed_chunk(
    db: Database,
    *,
    document_id: str,
    version_id: str,
    parser_run_id: str,
    slide_id: str,
    chunk_id: str,
    content: str,
    active_run: bool,
) -> None:
    now = utc_now()
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO documents VALUES (?,? ,?,'ready','{}',NULL,'user_demo',?,?)",
            (document_id, "ws_demo", document_id, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO document_versions VALUES (?,?,1,?,'test',1,?,NULL,?)",
            (version_id, document_id, version_id, f"/{version_id}.pptx", now),
        )
        conn.execute(
            "INSERT INTO parser_runs VALUES (?,?,?,?,?,'ready','{}',?,?)",
            (parser_run_id, version_id, "test", "1", "1", now, now),
        )
        conn.execute(
            "INSERT INTO slides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slide_id, parser_run_id, version_id, 1, content, content, None, 1, 1, None, 1.0),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,NULL,'text',?,'{}',?,1)",
            (chunk_id, "ws_demo", version_id, slide_id, content, chunk_id),
        )
        conn.execute("INSERT INTO chunk_fts(chunk_id,workspace_id,content) VALUES (?,?,?)", (chunk_id, "ws_demo", content))
        if active_run:
            conn.execute("UPDATE document_versions SET active_parser_run_id=? WHERE id=?", (parser_run_id, version_id))


@pytest.mark.skipif(importlib.util.find_spec("faiss") is None, reason="optional FAISS dependency is not installed")
def test_faiss_filters_document_scope_and_inactive_parser_runs(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.sqlite3")
    db.initialize()
    _seed_chunk(
        db,
        document_id="doc_a",
        version_id="dv_a",
        parser_run_id="run_a",
        slide_id="slide_a",
        chunk_id="chunk_a",
        content="revenue growth and operating margin",
        active_run=True,
    )
    _seed_chunk(
        db,
        document_id="doc_b",
        version_id="dv_b",
        parser_run_id="run_b",
        slide_id="slide_b",
        chunk_id="chunk_b",
        content="customer retention and renewal",
        active_run=True,
    )
    _seed_chunk(
        db,
        document_id="doc_a",
        version_id="dv_a",
        parser_run_id="run_stale",
        slide_id="slide_stale",
        chunk_id="chunk_stale",
        content="stale parser run secret metric",
        active_run=False,
    )
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        retrieval_strategy="hybrid",
        embedding_provider="hashing",
        embedding_model="hashing-v1",
        embedding_dimensions=128,
        vector_index_dir=tmp_path / "indexes",
        vector_min_similarity=-1.0,
    )
    store = FaissVectorStore(db, settings, HashingEmbeddingProvider(128))
    store.index_workspace("ws_demo")

    scoped = store.search("revenue growth", "ws_demo", ["doc_b"], limit=5)
    unscoped = store.search("stale parser run secret metric", "ws_demo", [], limit=10)

    assert [row["id"] for row in scoped] == ["chunk_b"]
    assert "chunk_stale" not in {row["id"] for row in unscoped}
    with db.read() as conn:
        indexed_ids = {row[0] for row in conn.execute("SELECT chunk_id FROM chunk_embeddings")}
    assert indexed_ids == {"chunk_a", "chunk_b"}
