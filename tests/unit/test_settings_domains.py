"""Tests for focused settings domain views."""

from pathlib import Path

from packages.qbr_core.foundation.config import Settings


def test_settings_exposes_focused_immutable_domain_objects(tmp_path: Path) -> None:
    settings = Settings(
        tmp_path,
        tmp_path / "app.sqlite3",
        tmp_path / "objects",
        retrieval_strategy="hybrid",
        embedding_provider="hashing",
        llm_enabled=True,
        llm_api_key="secret",
        llm_model="answer-model",
        parser_skill_name="parser",
    )

    assert settings.storage.database_path == settings.database_path
    assert settings.conversation.context_max_turns == settings.conversation_context_max_turns
    assert settings.models.model == "answer-model"
    assert settings.retrieval.vector_configured is True
    assert settings.workers.max_attempts == settings.job_max_attempts
    assert settings.skills.parser_name == "parser"
