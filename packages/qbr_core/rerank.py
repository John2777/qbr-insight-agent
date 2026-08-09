from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings


@dataclass(frozen=True, slots=True)
class RerankScore:
    index: int
    relevance_score: float


class Reranker(Protocol):
    model: str

    def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> list[RerankScore]: ...


class QwenReranker:
    """Client for Model Studio's OpenAI-compatible rerank endpoint.

    The endpoint is intentionally configured separately from ``compatible-mode``
    chat/embedding URLs because Model Studio exposes rerank under
    ``compatible-api/v1/reranks``.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.rerank_configured:
            raise ValueError("Rerank settings are incomplete")
        self.model = settings.rerank_model
        self.endpoint = self._endpoint(settings.rerank_base_url)
        self._api_key = settings.rerank_api_key
        self._timeout = settings.llm_timeout_seconds
        self._ssl_context = ssl.create_default_context()

    @staticmethod
    def _endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        return base if base.endswith("/reranks") else f"{base}/reranks"

    def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> list[RerankScore]:
        if not documents or top_n <= 0:
            return []
        payload = json.dumps(
            {
                "model": self.model,
                "query": query,
                "documents": list(documents),
                "top_n": min(top_n, len(documents)),
                "return_documents": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=self._ssl_context) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not include provider bodies: they can contain request data and
            # identifiers. Retrieval catches this error and degrades to RRF.
            raise RuntimeError(f"Rerank provider returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Rerank provider failed: {type(exc).__name__}") from exc
        raw_results: Any = body.get("results") if isinstance(body, dict) else None
        if raw_results is None and isinstance(body, dict) and isinstance(body.get("output"), dict):
            raw_results = body["output"].get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError("Rerank provider returned an invalid response")
        scores: list[RerankScore] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if isinstance(index, int) and 0 <= index < len(documents) and isinstance(score, int | float):
                scores.append(RerankScore(index=index, relevance_score=float(score)))
        if not scores:
            raise RuntimeError("Rerank provider returned no usable scores")
        return scores


def create_reranker(settings: Settings) -> Reranker | None:
    return QwenReranker(settings) if settings.rerank_configured else None
