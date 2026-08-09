from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI

from .config import Settings
from .db import Database, utc_now

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    model: str

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorSearchBackend(Protocol):
    backend_name: str

    def search(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def index_document_version(self, workspace_id: str, document_version_id: str) -> None: ...

    def rebuild_workspace(self, workspace_id: str) -> None: ...


class OpenAICompatibleEmbeddingProvider:
    """Synchronous embeddings client for OpenAI-compatible endpoints."""

    def __init__(self, settings: Settings) -> None:
        self.model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._client = OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        arguments: dict[str, Any] = {"model": self.model, "input": list(texts)}
        if self._dimensions:
            arguments["dimensions"] = self._dimensions
        response = self._client.embeddings.create(**arguments)
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(map(float, item.embedding)) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding provider returned an unexpected vector count")
        if vectors:
            actual_dimensions = len(vectors[0])
            if any(len(vector) != actual_dimensions for vector in vectors):
                raise RuntimeError("Embedding provider returned inconsistent dimensions")
            if self._dimensions not in {0, actual_dimensions}:
                raise RuntimeError("Embedding provider ignored the configured dimensions")
            self._dimensions = actual_dimensions
        return vectors


class HashingEmbeddingProvider:
    """Deterministic offline feature hashing for tests and ablation plumbing.

    This provider validates the vector lifecycle without network calls. It is
    intentionally not advertised as a semantic model for production use.
    """

    model = "hashing-v1"

    def __init__(self, dimensions: int = 384) -> None:
        self._dimensions = dimensions or 384

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @staticmethod
    def _features(text: str) -> list[str]:
        folded = text.casefold()
        features = re.findall(r"[a-z0-9%_-]{2,}", folded)
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
            features.extend(phrase[index : index + 2] for index in range(len(phrase) - 1))
        compact = re.sub(r"\s+", "", folded)
        features.extend(compact[index : index + 3] for index in range(max(0, len(compact) - 2)))
        return features or [folded]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                raw = int.from_bytes(digest, "little")
                vector[raw % self._dimensions] += 1.0 if raw & 1 else -1.0
            vectors.append(vector)
        return vectors


class FaissVectorStore:
    """FAISS cache over authoritative embedding rows stored in SQLite."""

    backend_name = "faiss"

    def __init__(self, db: Database, settings: Settings, provider: EmbeddingProvider) -> None:
        try:
            import faiss  # type: ignore[import-not-found]
            import numpy as np  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("FAISS vector dependencies are not installed; install the 'vector' extra") from exc
        self.db = db
        self.settings = settings
        self.provider = provider
        self.index_dir = settings.vector_index_dir or settings.data_dir / "vector_indexes"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.min_similarity = settings.vector_min_similarity
        self.candidate_k = settings.vector_candidate_k
        self.batch_size = settings.embedding_batch_size
        self._faiss = faiss
        self._np = np
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}

    def _stem(self, workspace_id: str) -> str:
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:24]
        return f"workspace_{digest}"

    def _index_path(self, workspace_id: str) -> Path:
        return self.index_dir / f"{self._stem(workspace_id)}.index"

    def _manifest_path(self, workspace_id: str) -> Path:
        return self.index_dir / f"{self._stem(workspace_id)}.json"

    def _active_chunks(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT ch.id,ch.content,ch.content_hash,ch.workspace_id
                FROM chunks ch JOIN slides s ON s.id=ch.slide_id
                JOIN document_versions dv ON dv.id=ch.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE ch.workspace_id=? AND ch.active=1 AND d.deleted_at IS NULL
                  AND s.parser_run_id=dv.active_parser_run_id
                ORDER BY ch.id
                """,
                (workspace_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _missing_chunks(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT ch.id,ch.content,ch.content_hash,ch.workspace_id
                FROM chunks ch JOIN slides s ON s.id=ch.slide_id
                JOIN document_versions dv ON dv.id=ch.document_version_id
                JOIN documents d ON d.id=dv.document_id
                LEFT JOIN chunk_embeddings ce ON ce.chunk_id=ch.id
                WHERE ch.workspace_id=? AND ch.active=1 AND d.deleted_at IS NULL
                  AND s.parser_run_id=dv.active_parser_run_id
                  AND (ce.id IS NULL OR ce.model<>? OR ce.content_hash<>ch.content_hash)
                ORDER BY ch.id
                """,
                (workspace_id, self.provider.model),
            ).fetchall()
        return [dict(row) for row in rows]

    def index_document_version(self, workspace_id: str, document_version_id: str) -> None:
        # Reconcile the whole workspace so a newly active parser run also evicts
        # vectors from the superseded run. SQLite remains the source of truth.
        del document_version_id
        self.index_workspace(workspace_id)

    def index_workspace(self, workspace_id: str) -> None:
        with self._lock:
            missing = self._missing_chunks(workspace_id)
            for start in range(0, len(missing), self.batch_size):
                batch = missing[start : start + self.batch_size]
                vectors = self.provider.embed([item["content"] for item in batch])
                if len(vectors) != len(batch):
                    raise RuntimeError("Embedding provider returned an unexpected vector count")
                with self.db.transaction(immediate=True) as conn:
                    for item, vector in zip(batch, vectors, strict=True):
                        dimensions = len(vector)
                        if dimensions < 1 or any(not math.isfinite(value) for value in vector):
                            raise RuntimeError("Embedding provider returned an invalid vector")
                        blob = array("f", vector).tobytes()
                        conn.execute(
                            """
                            INSERT INTO chunk_embeddings(
                              chunk_id,workspace_id,model,dimensions,vector,content_hash,created_at
                            ) VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(chunk_id) DO UPDATE SET
                              workspace_id=excluded.workspace_id,model=excluded.model,
                              dimensions=excluded.dimensions,vector=excluded.vector,
                              content_hash=excluded.content_hash,created_at=excluded.created_at
                            """,
                            (
                                item["id"], workspace_id, self.provider.model, dimensions,
                                blob, item["content_hash"], utc_now(),
                            ),
                        )
            if missing or not self._manifest_is_current(workspace_id):
                self.rebuild_workspace(workspace_id)

    def _embedding_rows(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT ce.id,ce.chunk_id,ce.dimensions,ce.vector,ce.content_hash
                FROM chunk_embeddings ce JOIN chunks ch ON ch.id=ce.chunk_id
                JOIN slides s ON s.id=ch.slide_id
                JOIN document_versions dv ON dv.id=ch.document_version_id
                JOIN documents d ON d.id=dv.document_id
                WHERE ce.workspace_id=? AND ce.model=? AND ch.active=1
                  AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
                ORDER BY ce.id
                """,
                (workspace_id, self.provider.model),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _signature(rows: Sequence[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for row in rows:
            digest.update(f"{row['id']}:{row['content_hash']}:{row['dimensions']}\n".encode())
        return digest.hexdigest()

    def _manifest_is_current(self, workspace_id: str) -> bool:
        path = self._index_path(workspace_id)
        manifest_path = self._manifest_path(workspace_id)
        if not path.exists() or not manifest_path.exists():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        rows = self._embedding_rows(workspace_id)
        return (
            manifest.get("model") == self.provider.model
            and manifest.get("count") == len(rows)
            and manifest.get("signature") == self._signature(rows)
        )

    def rebuild_workspace(self, workspace_id: str) -> None:
        with self._lock:
            rows = self._embedding_rows(workspace_id)
            index_path = self._index_path(workspace_id)
            manifest_path = self._manifest_path(workspace_id)
            if not rows:
                index_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                self._cache.pop(workspace_id, None)
                return
            dimensions = {int(row["dimensions"]) for row in rows}
            if len(dimensions) != 1:
                raise RuntimeError("Active embeddings have inconsistent dimensions")
            dimension = dimensions.pop()
            matrix = self._np.vstack(
                [self._np.frombuffer(row["vector"], dtype=self._np.float32, count=dimension) for row in rows]
            )
            matrix = self._normalize(matrix)
            ids = self._np.asarray([int(row["id"]) for row in rows], dtype=self._np.int64)
            index = self._faiss.IndexIDMap2(self._faiss.IndexFlatIP(dimension))
            index.add_with_ids(matrix, ids)
            tmp_index = index_path.with_suffix(".index.tmp")
            self._faiss.write_index(index, str(tmp_index))
            os.replace(tmp_index, index_path)
            manifest = {
                "workspace_id": workspace_id,
                "model": self.provider.model,
                "dimensions": dimension,
                "count": len(rows),
                "signature": self._signature(rows),
            }
            tmp_manifest = manifest_path.with_suffix(".json.tmp")
            tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_manifest, manifest_path)
            self._cache[workspace_id] = index

    def _normalize(self, matrix: Any) -> Any:
        contiguous = self._np.ascontiguousarray(matrix, dtype=self._np.float32)
        norms = self._np.linalg.norm(contiguous, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return self._np.ascontiguousarray(contiguous / norms, dtype=self._np.float32)

    def _load_index(self, workspace_id: str) -> Any | None:
        cached = self._cache.get(workspace_id)
        if cached is not None:
            return cached
        path = self._index_path(workspace_id)
        if not path.exists():
            return None
        index = self._faiss.read_index(str(path))
        self._cache[workspace_id] = index
        return index

    def search(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            self.index_workspace(workspace_id)
            index = self._load_index(workspace_id)
            if index is None or index.ntotal == 0:
                return []
            vectors = self.provider.embed([question])
            if not vectors:
                return []
            query = self._normalize(self._np.asarray(vectors[0], dtype=self._np.float32).reshape(1, -1))
            if query.shape[1] != index.d:
                raise RuntimeError("Query embedding dimension does not match the FAISS index")
            candidate_count = index.ntotal if document_ids else min(index.ntotal, max(limit, self.candidate_k))
            distances, identifiers = index.search(query, candidate_count)
            scored_ids = [
                (int(identifier), float(score))
                for score, identifier in zip(distances[0], identifiers[0], strict=True)
                if int(identifier) >= 0 and float(score) >= self.min_similarity
            ]
            if not scored_ids:
                return []
            ids = [identifier for identifier, _ in scored_ids]
            placeholders = ",".join("?" for _ in ids)
            document_scope = ""
            scope_args: list[Any] = []
            if document_ids:
                document_placeholders = ",".join("?" for _ in document_ids)
                document_scope = f" AND d.id IN ({document_placeholders})"
                scope_args.extend(document_ids)
            with self.db.read() as conn:
                rows = conn.execute(
                    f"""
                    SELECT ch.*,s.slide_no,e.bbox_json,d.title document_title,d.id document_id,
                      ce.id embedding_id
                    FROM chunk_embeddings ce JOIN chunks ch ON ch.id=ce.chunk_id
                    JOIN slides s ON s.id=ch.slide_id
                    JOIN document_versions dv ON dv.id=ch.document_version_id
                    JOIN documents d ON d.id=dv.document_id
                    LEFT JOIN elements e ON e.id=ch.element_id
                    WHERE ce.id IN ({placeholders}) AND ce.workspace_id=? AND ce.model=?
                      AND ch.workspace_id=? AND ch.active=1 AND d.deleted_at IS NULL
                      AND s.parser_run_id=dv.active_parser_run_id {document_scope}
                    """,
                    (*ids, workspace_id, self.provider.model, workspace_id, *scope_args),
                ).fetchall()
            by_id = {int(row["embedding_id"]): dict(row) for row in rows}
            results: list[dict[str, Any]] = []
            for identifier, score in scored_ids:
                row = by_id.get(identifier)
                if row is None:
                    continue
                row["vector_score"] = score
                results.append(row)
                if len(results) >= limit:
                    break
            return results


def create_vector_store(db: Database, settings: Settings) -> VectorSearchBackend | None:
    if not settings.vector_configured:
        return None
    provider: EmbeddingProvider
    if settings.embedding_provider == "hashing":
        provider = HashingEmbeddingProvider(settings.embedding_dimensions)
    else:
        provider = OpenAICompatibleEmbeddingProvider(settings)
    try:
        return FaissVectorStore(db, settings, provider)
    except RuntimeError as exc:
        logger.warning("Vector retrieval disabled: %s", exc)
        return None
