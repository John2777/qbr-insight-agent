from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from packages.qbr_core.foundation.paths import bundled_skill_root


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _path_list_env(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    value = os.getenv(name)
    if not value:
        return tuple(path.resolve() for path in default)
    return tuple(Path(item).expanduser().resolve() for item in value.split(os.pathsep) if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Hold validated runtime configuration for the QBR application."""
    data_dir: Path
    database_path: Path
    object_dir: Path
    max_upload_mib: int = 10
    max_slides: int = 200
    run_inline_worker: bool = True
    worker_poll_seconds: float = 1.0
    conversation_context_max_turns: int = 4
    conversation_context_token_budget: int = 2400
    conversation_summary_enabled: bool = True
    conversation_summary_token_budget: int = 800
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://localhost:5173")
    llm_enabled: bool = False
    llm_provider: str = "openai-compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    planner_model: str = ""
    deep_llm_model: str = ""
    answer_polishing_enabled: bool = True
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 1200
    llm_temperature: float = 0.1
    retrieval_strategy: str = "fts"
    embedding_provider: str = "openai-compatible"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_batch_size: int = 10
    vector_index_dir: Path | None = None
    vector_min_similarity: float = 0.30
    vector_candidate_k: int = 24
    lexical_candidate_k: int = 24
    retrieval_rrf_k: int = 60
    retrieval_lexical_weight: float = 1.25
    retrieval_vector_weight: float = 1.0
    rerank_enabled: bool = False
    rerank_base_url: str = ""
    rerank_api_key: str = field(default="", repr=False)
    rerank_model: str = "qwen3-rerank"
    rerank_candidate_k: int = 30
    rerank_top_n: int = 12
    vision_enabled: bool = False
    vision_base_url: str = ""
    vision_api_key: str = field(default="", repr=False)
    vision_model: str = "qwen3.7-plus"
    vision_max_tokens: int = 1200
    vision_max_slides: int = 50
    vision_enrich_all_slides: bool = False
    app_env: str = "local"
    auth_mode: str = "demo"
    jwt_secret: str = field(default="", repr=False)
    jwt_issuer: str = "qbr-insight-agent"
    jwt_audience: str = "qbr-web"
    password_username: str = ""
    password_hash: str = field(default="", repr=False)
    password_user_id: str = "user_demo"
    password_workspace_id: str = "ws_demo"
    session_ttl_seconds: int = 28_800
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    job_lease_seconds: int = 120
    job_heartbeat_seconds: int = 30
    job_max_attempts: int = 3
    log_level: str = "INFO"
    skill_paths: tuple[Path, ...] = ()
    parser_skill_name: str = ""
    table_reasoning_skill_name: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        """Build validated settings from environment variables."""
        load_dotenv(override=False)
        data_dir = Path(os.getenv("QBR_DATA_DIR", "data")).resolve()
        database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "app.sqlite3"))).resolve()
        object_dir = Path(os.getenv("OBJECT_STORE_PATH", str(data_dir / "objects"))).resolve()
        origins = tuple(
            item.strip()
            for item in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
            if item.strip()
        )
        llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        embedding_api_key = os.getenv("EMBEDDING_API_KEY", llm_api_key).strip()
        embedding_base_url = os.getenv(
            "EMBEDDING_BASE_URL", llm_base_url
        ).strip().rstrip("/")
        rerank_api_key = os.getenv("RERANK_API_KEY", llm_api_key).strip()
        vision_api_key = os.getenv("VISION_API_KEY", llm_api_key).strip()
        project_skill_root = bundled_skill_root()
        settings = cls(
            data_dir=data_dir,
            database_path=database_path,
            object_dir=object_dir,
            max_upload_mib=int(os.getenv("MAX_UPLOAD_MIB", "10")),
            max_slides=int(os.getenv("MAX_SLIDES", "200")),
            run_inline_worker=_bool_env("RUN_INLINE_WORKER", True),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1")),
            conversation_context_max_turns=int(os.getenv("CONVERSATION_CONTEXT_MAX_TURNS", "4")),
            conversation_context_token_budget=int(os.getenv("CONVERSATION_CONTEXT_TOKEN_BUDGET", "2400")),
            conversation_summary_enabled=_bool_env("CONVERSATION_SUMMARY_ENABLED", True),
            conversation_summary_token_budget=int(os.getenv("CONVERSATION_SUMMARY_TOKEN_BUDGET", "800")),
            cors_origins=origins,
            llm_enabled=_bool_env("LLM_ENABLED", bool(llm_api_key)),
            llm_provider=os.getenv("LLM_PROVIDER", "openai-compatible").strip(),
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            planner_model=os.getenv("PLANNER_MODEL", "").strip(),
            deep_llm_model=os.getenv("DEEP_LLM_MODEL", "").strip(),
            answer_polishing_enabled=_bool_env("ANSWER_POLISHING_ENABLED", True),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1200")),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
            retrieval_strategy=os.getenv("RETRIEVAL_STRATEGY", "fts").strip().lower(),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "openai-compatible").strip().lower(),
            embedding_base_url=embedding_base_url,
            embedding_api_key=embedding_api_key,
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "0")),
            embedding_batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
            vector_index_dir=Path(os.getenv("VECTOR_INDEX_DIR", str(data_dir / "vector_indexes"))).resolve(),
            vector_min_similarity=float(os.getenv("VECTOR_MIN_SIMILARITY", "0.30")),
            vector_candidate_k=int(os.getenv("VECTOR_CANDIDATE_K", "24")),
            lexical_candidate_k=int(os.getenv("LEXICAL_CANDIDATE_K", "24")),
            retrieval_rrf_k=int(os.getenv("RETRIEVAL_RRF_K", "60")),
            retrieval_lexical_weight=float(os.getenv("RETRIEVAL_LEXICAL_WEIGHT", "1.25")),
            retrieval_vector_weight=float(os.getenv("RETRIEVAL_VECTOR_WEIGHT", "1.0")),
            rerank_enabled=_bool_env("RERANK_ENABLED", False),
            rerank_base_url=os.getenv("RERANK_BASE_URL", "").strip().rstrip("/"),
            rerank_api_key=rerank_api_key,
            rerank_model=os.getenv("RERANK_MODEL", "qwen3-rerank").strip(),
            rerank_candidate_k=int(os.getenv("RERANK_CANDIDATE_K", "30")),
            rerank_top_n=int(os.getenv("RERANK_TOP_N", "12")),
            vision_enabled=_bool_env("VISION_ENABLED", False),
            vision_base_url=os.getenv("VISION_BASE_URL", llm_base_url).strip().rstrip("/"),
            vision_api_key=vision_api_key,
            vision_model=os.getenv("VISION_MODEL", "qwen3.7-plus").strip(),
            vision_max_tokens=int(os.getenv("VISION_MAX_TOKENS", "1200")),
            vision_max_slides=int(os.getenv("VISION_MAX_SLIDES", "50")),
            vision_enrich_all_slides=_bool_env("VISION_ENRICH_ALL_SLIDES", False),
            app_env=os.getenv("APP_ENV", "local").strip().lower(),
            auth_mode=os.getenv("AUTH_MODE", "demo").strip().lower(),
            jwt_secret=os.getenv("JWT_SECRET", "").strip(),
            jwt_issuer=os.getenv("JWT_ISSUER", "qbr-insight-agent").strip(),
            jwt_audience=os.getenv("JWT_AUDIENCE", "qbr-web").strip(),
            password_username=os.getenv("PASSWORD_USERNAME", "").strip(),
            password_hash=os.getenv("PASSWORD_HASH", "").strip(),
            password_user_id=os.getenv("PASSWORD_USER_ID", "user_demo").strip(),
            password_workspace_id=os.getenv("PASSWORD_WORKSPACE_ID", "ws_demo").strip(),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "28800")),
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")),
            login_window_seconds=int(os.getenv("LOGIN_WINDOW_SECONDS", "300")),
            job_lease_seconds=int(os.getenv("JOB_LEASE_SECONDS", "120")),
            job_heartbeat_seconds=int(os.getenv("JOB_HEARTBEAT_SECONDS", "30")),
            job_max_attempts=int(os.getenv("JOB_MAX_ATTEMPTS", "3")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            skill_paths=_path_list_env("SKILL_PATHS", (project_skill_root,)),
            parser_skill_name=os.getenv("PARSER_SKILL_NAME", "").strip(),
            table_reasoning_skill_name=os.getenv("TABLE_REASONING_SKILL_NAME", "").strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Validate configuration values and cross-field invariants."""
        if self.retrieval_strategy not in {"fts", "vector", "hybrid"}:
            raise ValueError("RETRIEVAL_STRATEGY must be fts, vector, or hybrid")
        if self.embedding_provider not in {"openai-compatible", "hashing"}:
            raise ValueError("EMBEDDING_PROVIDER must be openai-compatible or hashing")
        if self.embedding_dimensions < 0 or self.embedding_batch_size < 1:
            raise ValueError("Embedding dimensions and batch size are invalid")
        if not -1.0 <= self.vector_min_similarity <= 1.0:
            raise ValueError("VECTOR_MIN_SIMILARITY must be between -1 and 1")
        if self.vector_candidate_k < 1 or self.lexical_candidate_k < 1 or self.retrieval_rrf_k < 1:
            raise ValueError("Retrieval candidate sizes and RRF constant must be positive")
        if self.retrieval_lexical_weight <= 0 or self.retrieval_vector_weight <= 0:
            raise ValueError("Retrieval fusion weights must be positive")
        if self.rerank_candidate_k < 1 or self.rerank_top_n < 1:
            raise ValueError("Rerank candidate sizes must be positive")
        if self.rerank_top_n > self.rerank_candidate_k:
            raise ValueError("RERANK_TOP_N must not exceed RERANK_CANDIDATE_K")
        if self.conversation_context_max_turns < 1 or self.conversation_context_token_budget < 1:
            raise ValueError("Conversation context limits must be positive")
        if self.conversation_summary_token_budget < 1:
            raise ValueError("CONVERSATION_SUMMARY_TOKEN_BUDGET must be positive")
        if self.vision_max_tokens < 1 or self.vision_max_slides < 1:
            raise ValueError("Vision token and slide limits must be positive")
        if self.auth_mode not in {"demo", "jwt", "password"}:
            raise ValueError("AUTH_MODE must be demo, jwt, or password")
        if self.app_env in {"production", "prod"} and self.auth_mode == "demo":
            raise ValueError("AUTH_MODE=demo is forbidden when APP_ENV=production")
        if self.auth_mode in {"jwt", "password"} and len(self.jwt_secret.encode()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes when signed authentication is enabled")
        if self.auth_mode == "password":
            if not self.password_username or not self.password_hash.startswith("scrypt$"):
                raise ValueError("PASSWORD_USERNAME and a generated scrypt PASSWORD_HASH are required")
            if not self.password_user_id or not self.password_workspace_id:
                raise ValueError("Password identity mapping must not be empty")
        if self.session_ttl_seconds < 300 or self.session_ttl_seconds > 86_400:
            raise ValueError("SESSION_TTL_SECONDS must be between 300 and 86400")
        if self.login_max_attempts < 1 or self.login_window_seconds < 30:
            raise ValueError("Login rate-limit settings are invalid")
        if self.job_heartbeat_seconds < 1 or self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("JOB_HEARTBEAT_SECONDS must be positive and shorter than JOB_LEASE_SECONDS")
        if self.job_max_attempts < 1:
            raise ValueError("JOB_MAX_ATTEMPTS must be positive")

    @property
    def llm_configured(self) -> bool:
        """Return whether answer-model configuration is complete."""
        return self.llm_enabled and bool(self.llm_api_key and self.llm_model and self.llm_base_url)

    @property
    def vector_configured(self) -> bool:
        """Return whether vector retrieval configuration is complete."""
        if self.retrieval_strategy == "fts":
            return False
        if self.embedding_provider == "hashing":
            return True
        return bool(self.embedding_api_key and self.embedding_model and self.embedding_base_url)

    @property
    def rerank_configured(self) -> bool:
        """Return whether reranking configuration is complete."""
        return self.rerank_enabled and bool(self.rerank_api_key and self.rerank_model and self.rerank_base_url)

    @property
    def vision_configured(self) -> bool:
        """Return whether slide-vision configuration is complete."""
        return self.vision_enabled and bool(self.vision_api_key and self.vision_model and self.vision_base_url)

    def ensure_directories(self) -> None:
        """Create the configured runtime data directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_dir.mkdir(parents=True, exist_ok=True)
        if self.vector_index_dir is not None:
            self.vector_index_dir.mkdir(parents=True, exist_ok=True)
