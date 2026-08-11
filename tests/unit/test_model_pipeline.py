from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.documents.vision import VisualKnowledge
from packages.qbr_core.foundation.database import Database, utc_now
from packages.qbr_core.planning import deterministic_plan
from packages.qbr_core.retrieval.engine import EvidenceRetriever
from packages.qbr_core.retrieval.evidence import EvidencePackBuilder
from packages.qbr_core.retrieval.reranking import QwenReranker, RerankScore
from packages.qbr_core.retrieval.vector_store import FaissVectorStore, HashingEmbeddingProvider


class ReverseReranker:
    model = "test-reranker"

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankScore]:
        del query
        return [RerankScore(index=index, relevance_score=float(index)) for index in range(len(documents) - 1, -1, -1)][:top_n]


class FailingReranker:
    model = "failing-reranker"

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankScore]:
        del query, documents, top_n
        raise TimeoutError("offline")


class FakeHTTPResponse:
    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"results":[{"index":1,"relevance_score":0.91},{"index":0,"relevance_score":0.24}]}'


def _seed_active_chunk(db: Database, content: str = "semantic retrieval content") -> None:
    now = utc_now()
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO documents VALUES ('doc_model','ws_demo','Model QBR','ready','{}',NULL,'user_demo',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO document_versions VALUES ('dv_model','doc_model',1,'hash','test',1,'/tmp/model.pptx','pr_model',?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO parser_runs VALUES ('pr_model','dv_model','test','1','1','ready','{}',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO slides VALUES ('slide_model','pr_model','dv_model',1,'Model','Model',NULL,1,1,NULL,1)"
        )
        conn.execute(
            "INSERT INTO chunks VALUES ('chunk_model','ws_demo','dv_model','slide_model',NULL,'text',?,'{}','hash',1)",
            (content,),
        )
        conn.execute("INSERT INTO chunk_fts VALUES ('chunk_model','ws_demo',?)", (content,))


def test_bailian_defaults_and_capability_gates(tmp_path: Path) -> None:
    base = dict(data_dir=tmp_path, database_path=tmp_path / "db.sqlite3", object_dir=tmp_path / "objects")
    settings = Settings(
        **base,
        rerank_enabled=True,
        rerank_base_url="https://example.test/compatible-api/v1",
        rerank_api_key="secret",
        vision_enabled=True,
        vision_base_url="https://example.test/compatible-mode/v1",
        vision_api_key="secret",
    )

    assert settings.embedding_batch_size == 10
    assert settings.rerank_configured is True
    assert settings.vision_configured is True


def test_database_migrates_embedding_identity_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE chunk_embeddings(
             id INTEGER PRIMARY KEY AUTOINCREMENT,chunk_id TEXT NOT NULL UNIQUE,
             workspace_id TEXT NOT NULL,model TEXT NOT NULL,dimensions INTEGER NOT NULL,
             vector BLOB NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO chunk_embeddings(chunk_id,workspace_id,model,dimensions,vector,content_hash,created_at) "
        "VALUES ('chunk_old','ws_demo','old-model',2,?,'hash','now')",
        (b"12345678",),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.initialize()

    with db.read() as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(chunk_embeddings)")}
        row = migrated.execute("SELECT chunk_id,embedding_identity FROM chunk_embeddings").fetchone()
    assert "embedding_identity" in columns
    assert dict(row) == {"chunk_id": "chunk_old", "embedding_identity": ""}


def test_semantic_rerank_reorders_and_falls_back_without_losing_candidates(tmp_path: Path) -> None:
    rows = [
        {"id": "first", "content": "first", "slide_no": 1},
        {"id": "second", "content": "second", "slide_no": 2},
        {"id": "third", "content": "third", "slide_no": 3},
    ]
    retriever = EvidenceRetriever(Database(tmp_path / "db.sqlite3"), reranker=ReverseReranker())

    reranked, diagnostics = retriever._semantic_rerank("question", rows, 3)

    assert [row["id"] for row in reranked] == ["third", "second", "first"]
    assert diagnostics["rerank_status"] == "completed"
    assert reranked[0]["rerank_score"] == 2.0

    fallback = EvidenceRetriever(Database(tmp_path / "db.sqlite3"), reranker=FailingReranker())
    unchanged, diagnostics = fallback._semantic_rerank("question", rows, 3)
    assert unchanged == rows
    assert diagnostics == {
        "rerank_model": "failing-reranker",
        "rerank_status": "fallback",
        "rerank_error": "TimeoutError",
    }


def test_qwen_rerank_client_uses_compatible_api_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Any, **kwargs: object) -> FakeHTTPResponse:
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = kwargs["timeout"]
        return FakeHTTPResponse()

    monkeypatch.setattr("packages.qbr_core.retrieval.reranking.urllib.request.urlopen", fake_urlopen)
    settings = Settings(
        tmp_path,
        tmp_path / "db.sqlite3",
        tmp_path / "objects",
        rerank_enabled=True,
        rerank_base_url="https://workspace.example/compatible-api/v1",
        rerank_api_key="secret",
    )

    scores = QwenReranker(settings).rerank("risk", ["stable", "risk increased"], top_n=2)

    assert captured["url"] == "https://workspace.example/compatible-api/v1/reranks"
    assert captured["payload"] == {
        "model": "qwen3-rerank",
        "query": "risk",
        "documents": ["stable", "risk increased"],
        "top_n": 2,
        "return_documents": False,
    }
    assert scores == [RerankScore(1, 0.91), RerankScore(0, 0.24)]


@pytest.mark.skipif(importlib.util.find_spec("faiss") is None, reason="optional FAISS dependency is not installed")
def test_embedding_identity_and_dimensions_force_safe_reindex(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    _seed_active_chunk(db)
    base = dict(
        data_dir=tmp_path,
        database_path=tmp_path / "db.sqlite3",
        object_dir=tmp_path / "objects",
        retrieval_strategy="hybrid",
        embedding_provider="hashing",
        vector_index_dir=tmp_path / "indexes",
        vector_min_similarity=-1.0,
    )
    store_128 = FaissVectorStore(db, Settings(**base, embedding_dimensions=128), HashingEmbeddingProvider(128))
    store_128.index_workspace("ws_demo")
    store_64 = FaissVectorStore(db, Settings(**base, embedding_dimensions=64), HashingEmbeddingProvider(64))
    store_64.index_workspace("ws_demo")

    with db.read() as conn:
        row = conn.execute("SELECT dimensions,embedding_identity FROM chunk_embeddings").fetchone()
    assert row["dimensions"] == 64
    assert row["embedding_identity"] == "hashing-v1:64"
    assert store_64.search("semantic", "ws_demo", [], limit=1)[0]["id"] == "chunk_model"


def test_role_specific_models_are_wired_without_changing_default_contract(tmp_path: Path) -> None:
    settings = Settings(
        tmp_path,
        tmp_path / "db.sqlite3",
        tmp_path / "objects",
        llm_enabled=True,
        llm_base_url="https://example.test/compatible-mode/v1",
        llm_api_key="secret",
        llm_model="qwen3.7-plus",
        planner_model="qwen3.6-flash",
        deep_llm_model="qwen3.7-max",
    )

    service = QBRService(settings)

    assert service.qa_agent is not None and service.qa_agent.model_name == "qwen3.7-plus"
    assert service.deep_qa_agent is not None and service.deep_qa_agent.model_name == "qwen3.7-max"
    assert service.query_planner.model is not service.qa_agent.model
    assert service.qa_service.deep_qa_agent is service.deep_qa_agent


def test_visual_knowledge_emits_typed_supplemental_chunks() -> None:
    knowledge = VisualKnowledge(
        summary="Regional demand is concentrated in Southeast Asia.",
        ocr_text="Priority market",
        observations=("The highlighted region has the strongest emphasis.",),
        confidence=0.82,
        model="qwen3.7-plus",
    )

    assert knowledge.chunks() == (
        ("slide_visual_summary", "Regional demand is concentrated in Southeast Asia."),
        ("visual_ocr", "Priority market"),
        ("visual_observation", "- The highlighted region has the strongest emphasis."),
    )


def test_visual_numeric_text_cannot_replace_authoritative_chart_or_table_data() -> None:
    plan = deterministic_plan("Revenue 的准确数值是多少？")
    pack = EvidencePackBuilder().build(
        plan,
        [
            {
                "id": "visual-number",
                "document_version_id": "dv",
                "slide_id": "slide",
                "slide_no": 1,
                "chunk_type": "visual_ocr",
                "content": "Revenue 123.4 million",
                "source_kind": "visual_model",
                "retrieval_score": 10,
            }
        ],
    )

    assert pack.atoms == ()
    assert pack.diagnostics["rejected_quality"] == {"visual_numeric": 1}
