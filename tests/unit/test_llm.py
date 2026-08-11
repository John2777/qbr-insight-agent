from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pytest import LogCaptureFixture

from packages.qbr_core import QBRService
from packages.qbr_core.foundation.config import Settings
from packages.qbr_core.providers.llm import EvidenceQAAgent, build_chat_model


class FakeModel:
    def __init__(self, content: str | Exception, *, finish_reason: str | None = None, output_tokens: int = 20) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.messages: object | None = None

    def invoke(self, messages: object) -> AIMessage:
        self.messages = messages
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


def task_frame(question: str) -> dict[str, object]:
    return {
        "canonical_question": question,
        "task_summary": f"Directly resolve: {question}",
        "answer_brief": "Use the evidence and answer directly.",
        "operations": ["locate the requested value"],
        "evidence_requirements": ["the requested metric and period"],
    }


def answer(agent: EvidenceQAAgent, question: str, grounding: str, items: list[dict[str, object]] | None = None):
    return agent.answer(
        question=question,
        grounding_context=grounding,
        evidence=items if items is not None else evidence(),
        history=[],
        task_frame=task_frame(question),
    )


def test_llm_answer_accepts_grounded_numbers_and_citations(tmp_path: Path) -> None:
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel("Q2 Revenue 为 20。[1]"))
    result = answer(agent, "Q2 Revenue 是多少？", "Revenue: Q2=20 [1]")
    assert result.answer == "Q2 Revenue 为 20。[1]"
    assert result.warnings == []
    assert result.model["thinking"] == "disabled"


def test_llm_answer_rejects_invented_number(tmp_path: Path) -> None:
    fallback = "Revenue: Q2=20 [1]"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel("Q2 Revenue 为 25。[1]"))
    result = answer(agent, "Q2 Revenue 是多少？", fallback)
    assert result.answer == fallback
    assert result.warnings == ["LLM_NUMERIC_VALIDATION_FAILED"]
    assert result.model["status"] == "fallback"
    assert result.model["answer_source"] == "safe_fallback"
    assert result.diagnostics["disposition"] == "fallback"


def test_llm_accepts_auditable_derived_numeric_claim_instead_of_dumping_evidence(tmp_path: Path) -> None:
    grounding = "编号证据：\n[1] 股东资本比率当前为221%，绿色阈值为210%"
    safe_fallback = "基于当前可核验证据，可以确认：\n\n- 业务事实：股东资本比率当前为221%。[1]"
    model = FakeModel("资本比率目前为221%，仍高于文档绿色阈值。[1]\n安全缓冲为11个百分点。[1]")
    result = EvidenceQAAgent(settings_at(tmp_path), model=model).answer(
        question="当前文档有哪些潜在问题？",
        grounding_context=grounding,
        safe_fallback=safe_fallback,
        evidence=[{**evidence()[0], "quote": "股东资本比率当前为221%，绿色阈值为210%"}],
        history=[],
        task_frame=task_frame("当前文档有哪些潜在问题？"),
    )

    assert result.answer == "资本比率目前为221%，仍高于文档绿色阈值。[1]\n安全缓冲为11个百分点。[1]"
    assert result.model["status"] == "completed"
    assert result.model["answer_source"] == "model"
    assert result.warnings == []
    assert result.diagnostics["disposition"] == "accepted"


def test_llm_provider_failure_is_explicit_and_uses_safe_grounding_fallback(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    fallback = "Revenue: Q2=20 [1]"
    agent = EvidenceQAAgent(settings_at(tmp_path), model=FakeModel(RuntimeError("provider unavailable")))
    result = agent.answer(
        question="Q2 Revenue 是多少？",
        grounding_context=fallback,
        evidence=evidence(),
        history=[],
        task_frame=task_frame("Q2 Revenue 是多少？"),
        run_id="run_provider_failure",
    )
    assert result.answer == fallback
    assert result.warnings == ["LLM_PROVIDER_ERROR"]
    assert result.model["status"] == "fallback"
    assert "provider unavailable" not in str(result.model)
    assert '"event":"provider_call_failed"' in caplog.text


def test_llm_truncation_uses_complete_grounding_fallback(tmp_path: Path) -> None:
    fallback = "Digital STP: 26/02=73.8; 26/03=72.1 [1]"
    model = FakeModel("Digital STP 从26/02的73.8", finish_reason="length", output_tokens=1200)
    result = answer(EvidenceQAAgent(settings_at(tmp_path), model=model), "Digital STP 有何变化？", fallback)
    assert result.answer == fallback
    assert result.warnings == ["LLM_OUTPUT_TRUNCATED"]
    assert result.model["status"] == "truncated"


def test_llm_token_limit_without_finish_reason_uses_fallback(tmp_path: Path) -> None:
    fallback = "Revenue: Q2=20 [1]"
    model = FakeModel("Q2 Revenue 为 20", output_tokens=1200)
    result = answer(EvidenceQAAgent(settings_at(tmp_path), model=model), "Q2 Revenue 是多少？", fallback)
    assert result.answer == fallback
    assert result.warnings == ["LLM_OUTPUT_TRUNCATED"]


def test_generation_prompt_uses_task_frame_without_fixed_summary_template(tmp_path: Path) -> None:
    model = FakeModel("增长延续，同时集中度偏高。[1]")
    items = [{**evidence()[0], "quote": "Growth continued while concentration remained elevated."}]
    agent = EvidenceQAAgent(settings_at(tmp_path), model=model)
    result = answer(agent, "请概括优势和潜在问题。", "Growth continued; concentration elevated [1]", items)
    prompt = "\n".join(str(getattr(item, "content", "")) for item in model.messages or [])
    assert result.answer == "增长延续，同时集中度偏高。[1]"
    assert "语义任务框架" in prompt
    assert "固定使用“总体判断" not in prompt
    assert "negative_signal_summary" not in prompt


def test_generation_prompt_marks_structured_summary_as_non_evidence(tmp_path: Path) -> None:
    model = FakeModel("ACME 的收入需要依据文档证据判断。[1]")
    agent = EvidenceQAAgent(settings_at(tmp_path), model=model)

    result = agent.answer(
        question="它怎么样？",
        grounding_context="ACME revenue improved [1]",
        evidence=[{**evidence()[0], "quote": "ACME revenue improved"}],
        history=[],
        conversation_summary={"entities": ["ACME"], "conversation_goals": ["分析收入"]},
        task_frame=task_frame("它怎么样？"),
    )

    prompt = "\n".join(str(getattr(item, "content", "")) for item in model.messages or [])
    assert result.answer == "ACME 的收入需要依据文档证据判断。[1]"
    assert '"entities":["ACME"]' in prompt
    assert "不能作为业务事实证据" in prompt


def test_deepseek_answer_client_explicitly_disables_thinking(tmp_path: Path) -> None:
    with patch("packages.qbr_core.providers.llm.ChatOpenAI") as constructor:
        build_chat_model(settings_at(tmp_path), "deepseek-v4-flash", thinking_enabled=False)
    assert constructor.call_args.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_planner_client_can_use_thinking_independently(tmp_path: Path) -> None:
    with patch("packages.qbr_core.providers.llm.ChatOpenAI") as constructor:
        build_chat_model(settings_at(tmp_path), "deepseek-v4-flash", thinking_enabled=True)
    assert constructor.call_args.kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


def test_qwen_model_identity_disables_thinking_with_generic_provider(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    settings = Settings(
        settings.data_dir,
        settings.database_path,
        settings.object_dir,
        llm_enabled=True,
        llm_provider="openai-compatible",
        llm_base_url="https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        llm_api_key="test-key",
        llm_model="qwen3.7-plus",
    )
    with patch("packages.qbr_core.providers.llm.ChatOpenAI") as constructor:
        build_chat_model(settings, "qwen3.6-flash", thinking_enabled=False)
    assert constructor.call_args.kwargs["extra_body"] == {"enable_thinking": False}


def test_service_disables_thinking_for_structured_planner_output(tmp_path: Path) -> None:
    with patch("packages.qbr_core.application.service.build_chat_model") as constructor:
        service = QBRService(settings_at(tmp_path))

    assert service.query_planner.model is constructor.return_value
    assert constructor.call_args.kwargs["thinking_enabled"] is False
