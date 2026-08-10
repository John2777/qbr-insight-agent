from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage
from pytest import LogCaptureFixture

from packages.qbr_core.config import Settings
from packages.qbr_core.llm import EvidenceQAAgent


class FakeModel:
    def __init__(self, content: str | Exception, *, finish_reason: str | None = None, output_tokens: int = 20) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens

    def invoke(self, _messages: object) -> AIMessage:
        if isinstance(self.content, Exception):
            raise self.content
        response_metadata = {"model_name": "deepseek-v4-flash"}
        if self.finish_reason is not None:
            response_metadata["finish_reason"] = self.finish_reason
        return AIMessage(
            content=self.content,
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": self.output_tokens,
                "total_tokens": 100 + self.output_tokens,
            },
            response_metadata=response_metadata,
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


def test_llm_length_finish_uses_complete_deterministic_fallback(tmp_path: Path) -> None:
    fallback = "潜在问题包括：Digital STP 从26/02的73.8降至26/03的72.1，下降1.7个百分点。[1]"
    truncated = "潜在问题包括：Digital STP 从26/02的73.8"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(truncated, finish_reason="length", output_tokens=1200))

    result = agent.answer(
        question="当前文档里能找到哪些公司潜在的问题？",
        deterministic_answer=fallback,
        evidence=evidence(),
        history=[],
        answer_mode="negative_signal_summary",
        query_plan={"intent": "negative_signal_summary"},
    )

    assert result.answer == fallback
    assert result.warnings == ["LLM_OUTPUT_TRUNCATED"]
    assert result.model["status"] == "truncated"
    assert result.model["finish_reason"] == "length"


def test_llm_token_limit_without_finish_reason_uses_fallback(tmp_path: Path) -> None:
    fallback = "Revenue 在 Q2 的值为 20。[1]"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel("Q2 Revenue 为 20", output_tokens=1200))

    result = agent.answer(question="Q2 Revenue 是多少？", deterministic_answer=fallback, evidence=evidence(), history=[])

    assert result.answer == fallback
    assert result.warnings == ["LLM_OUTPUT_TRUNCATED"]
    assert result.model["status"] == "truncated"


def test_llm_rejects_positive_document_summary_for_negative_intent(tmp_path: Path) -> None:
    fallback = "文档中最明确的负面信号是：\n\n**阈值事项**\n\n- Risk concentration 超过限额。[1]"
    candidate = (
        "## 总体判断\n\n公司业绩整体上行。[1]\n\n"
        "## 关键趋势\n\n核心指标保持增长。[1]\n\n"
        "## 经营解读\n\n增长动量延续。[1]\n\n"
        "## 建议关注\n\n后续继续关注执行风险。[1]"
    )
    negative_evidence = [
        {
            "document_title": "FY25 QBR",
            "slide_no": 1,
            "source_kind": "native_ooxml",
            "confidence": 1.0,
            "quote": "Risk concentration exceeded the approved limit.",
        }
    ]
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(candidate))

    result = agent.answer(
        question="当前文档有哪些潜在问题？",
        deterministic_answer=fallback,
        evidence=negative_evidence,
        history=[],
        answer_mode="negative_signal_summary",
        query_plan={"intent": "negative_signal_summary"},
    )

    assert result.answer == fallback
    assert result.warnings == ["LLM_QUERY_ADHERENCE_FAILED"]


def test_llm_accepts_issue_focused_answer_for_negative_intent(tmp_path: Path) -> None:
    fallback = "文档中最明确的负面信号是：Risk concentration 超过限额。[1]"
    candidate = "潜在问题主要是风险集中度超过限额，需要管理层关注。[1]"
    negative_evidence = [
        {
            "document_title": "FY25 QBR",
            "slide_no": 1,
            "source_kind": "native_ooxml",
            "confidence": 1.0,
            "quote": "Risk concentration exceeded the approved limit.",
        }
    ]
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(candidate))

    result = agent.answer(
        question="当前文档有哪些潜在问题？",
        deterministic_answer=fallback,
        evidence=negative_evidence,
        history=[],
        answer_mode="negative_signal_summary",
        query_plan={"intent": "negative_signal_summary"},
    )

    assert result.answer == candidate
    assert result.warnings == []


def test_llm_allows_summary_structure_when_negative_analysis_is_one_part_of_composite_task(tmp_path: Path) -> None:
    fallback = "## 文档概览\n\n整体增长。[1]\n\n## 潜在问题与风险\n\n风险集中度偏高。[1]"
    candidate = "## 总体判断\n\n增长延续，但风险集中度偏高。[1]\n\n## 建议关注\n\n需要降低集中度。[1]"
    composite_evidence = [
        {
            "document_title": "FY25 QBR",
            "slide_no": 1,
            "source_kind": "native_ooxml",
            "confidence": 1.0,
            "quote": "Growth continued, while risk concentration remained elevated.",
        }
    ]
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(candidate))

    result = agent.answer(
        question="请总结优势和潜在问题。",
        deterministic_answer=fallback,
        evidence=composite_evidence,
        history=[],
        answer_mode="business_evaluation",
        query_plan={
            "intent": "business_evaluation",
            "secondary_intents": ["negative_signal_summary", "summary"],
        },
    )

    assert result.answer == candidate
    assert result.warnings == []
