from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage
from pytest import LogCaptureFixture

from packages.qbr_core.config import Settings
from packages.qbr_core.llm import EvidenceQAAgent


class FakeModel:
    def __init__(self, content: str | Exception) -> None:
        self.content = content

    def invoke(self, _messages: object) -> AIMessage:
        if isinstance(self.content, Exception):
            raise self.content
        return AIMessage(
            content=self.content,
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            response_metadata={"model_name": "deepseek-v4-flash"},
        )


def settings_at(root: Path) -> Settings:
    return Settings(
        root,
        root / "app.sqlite3",
        root / "objects",
        llm_enabled=True,
        llm_provider="deepseek",
        llm_base_url="https://api.deepseek.com",
        llm_api_key="test-key",
        llm_model="deepseek-v4-flash",
    )


def evidence() -> list[dict[str, object]]:
    return [
        {
            "document_title": "FY25 QBR",
            "slide_no": 1,
            "source_kind": "embedded_workbook",
            "confidence": 1.0,
            "quote": "Revenue: Q2=20",
        }
    ]


def test_llm_answer_accepts_grounded_numbers_and_citations(tmp_path: Path) -> None:
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel("Q2 Revenue 为 20。[1]"))
    result = agent.answer(
        question="Q2 Revenue 是多少？",
        deterministic_answer="Revenue 在 Q2 的值为 20。[1]",
        evidence=evidence(),
        history=[],
    )
    assert result.answer == "Q2 Revenue 为 20。[1]"
    assert result.warnings == []
    assert result.model["total_tokens"] == 120


def test_llm_answer_rejects_invented_number(tmp_path: Path) -> None:
    fallback = "Revenue 在 Q2 的值为 20。[1]"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel("Q2 Revenue 为 25。[1]"))
    result = agent.answer(question="Q2 Revenue 是多少？", deterministic_answer=fallback, evidence=evidence(), history=[])
    assert result.answer == fallback
    assert result.warnings == ["LLM_NUMERIC_VALIDATION_FAILED"]


def test_llm_provider_failure_is_explicit_and_uses_safe_fallback(tmp_path: Path, caplog: LogCaptureFixture) -> None:
    fallback = "Revenue 在 Q2 的值为 20。[1]"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(RuntimeError("provider unavailable")))
    result = agent.answer(
        question="Q2 Revenue 是多少？",
        deterministic_answer=fallback,
        evidence=evidence(),
        history=[],
        run_id="run_provider_failure",
    )
    assert result.answer == fallback
    assert result.warnings == ["LLM_PROVIDER_ERROR"]
    assert result.model["status"] == "fallback"
    assert "provider unavailable" not in str(result.model)
    assert '"event":"provider_call_failed"' in caplog.text
    assert '"run_id":"run_provider_failure"' in caplog.text
