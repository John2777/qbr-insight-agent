"""Focused immutable settings objects used by application domains."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """Configure local persistence and document ingestion limits."""

    data_dir: Path
    database_path: Path
    object_dir: Path
    max_upload_mib: int
    max_slides: int


@dataclass(frozen=True, slots=True)
class ConversationSettings:
    """Configure immutable conversation context and summary behavior."""

    context_max_turns: int
    context_token_budget: int
    summary_enabled: bool
    summary_token_budget: int


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Configure answer, planning, and polishing model behavior."""

    enabled: bool
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    planner_model: str = ""
    deep_model: str = ""
    answer_polishing_enabled: bool = True
    timeout_seconds: float = 30.0
    max_retries: int = 2
    max_tokens: int = 1200
    temperature: float = 0.1

    @property
    def configured(self) -> bool:
        """Return whether answer-model configuration is complete."""
        return self.enabled and bool(self.api_key and self.model and self.base_url)


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """Configure lexical, vector, fusion, and semantic reranking."""

    strategy: str
    embedding_provider: str
    embedding_base_url: str
    embedding_api_key: str = field(repr=False)
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_batch_size: int = 10
    vector_index_dir: Path | None = None
    vector_min_similarity: float = 0.30
    vector_candidate_k: int = 24
    lexical_candidate_k: int = 24
    rrf_k: int = 60
    lexical_weight: float = 1.25
    vector_weight: float = 1.0
    rerank_enabled: bool = False
    rerank_base_url: str = ""
    rerank_api_key: str = field(default="", repr=False)
    rerank_model: str = "qwen3-rerank"
    rerank_candidate_k: int = 30
    rerank_top_n: int = 12

    @property
    def vector_configured(self) -> bool:
        """Return whether the selected vector backend can be created."""
        if self.strategy == "fts":
            return False
        if self.embedding_provider == "hashing":
            return True
        return bool(self.embedding_api_key and self.embedding_model and self.embedding_base_url)

    @property
    def rerank_configured(self) -> bool:
        """Return whether semantic reranking configuration is complete."""
        return self.rerank_enabled and bool(self.rerank_api_key and self.rerank_model and self.rerank_base_url)


@dataclass(frozen=True, slots=True)
class VisionSettings:
    """Configure optional visual slide enrichment."""

    enabled: bool
    base_url: str
    api_key: str = field(repr=False)
    model: str = "qwen3.7-plus"
    max_tokens: int = 1200
    max_slides: int = 50
    enrich_all_slides: bool = False

    @property
    def configured(self) -> bool:
        """Return whether vision enrichment configuration is complete."""
        return self.enabled and bool(self.api_key and self.model and self.base_url)


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Configure environment identity, tokens, and password authentication."""

    app_env: str
    mode: str
    jwt_secret: str = field(repr=False)
    jwt_issuer: str = "qbr-insight-agent"
    jwt_audience: str = "qbr-web"
    password_username: str = ""
    password_hash: str = field(default="", repr=False)
    password_user_id: str = "user_demo"
    password_workspace_id: str = "ws_demo"
    session_ttl_seconds: int = 28_800
    login_max_attempts: int = 5
    login_window_seconds: int = 300


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Configure inline execution, polling, leases, and retry limits."""

    run_inline: bool
    poll_seconds: float
    lease_seconds: int
    heartbeat_seconds: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class SkillSettings:
    """Configure discoverable parser and reasoning skill selection."""

    paths: tuple[Path, ...]
    parser_name: str
    table_reasoning_name: str


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Configure HTTP origins and operational logging."""

    cors_origins: tuple[str, ...]
    log_level: str
