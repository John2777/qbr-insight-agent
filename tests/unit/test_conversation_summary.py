from __future__ import annotations

import json
from pathlib import Path

from packages.qbr_core import QBRService
from packages.qbr_core.config import Settings


class FailingSummaryModel:
    model_name = "summary-test-model"

    def invoke(self, messages: object) -> object:
        raise RuntimeError("summary provider unavailable")


def _service(tmp_path: Path, *, max_turns: int = 2) -> QBRService:
    return QBRService(
        Settings(
            tmp_path,
            tmp_path / "app.sqlite3",
            tmp_path / "objects",
            run_inline_worker=False,
            conversation_context_max_turns=max_turns,
            conversation_context_token_budget=1200,
            conversation_summary_token_budget=400,
        )
    )


def _complete_turn(
    service: QBRService,
    assistant_message_id: str,
    answer: str,
    canonical_question: str,
) -> None:
    metadata = {"query_plan": {"canonical_question": canonical_question}}
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            """UPDATE messages
               SET status='completed',content=?,metadata_json=? WHERE id=?""",
            (answer, service.db.json(metadata), assistant_message_id),
        )


def _add_completed_turn(
    service: QBRService,
    conversation_id: str,
    question: str,
    answer: str,
    canonical_question: str,
) -> dict[str, object]:
    queued = service.ask(conversation_id, question, "ws_demo", "user_demo")
    _complete_turn(service, queued["assistant_message_id"], answer, canonical_question)
    return queued


def test_structured_summary_is_incremental_and_excludes_assistant_answer_text(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation = service.create_conversation("ws_demo", "user_demo")
    _add_completed_turn(service, conversation["id"], "看 ACME 2025 年收入", "秘密回答一", "分析 ACME 2025 年收入")
    _add_completed_turn(service, conversation["id"], "和 2024 年比呢", "秘密回答二", "比较 ACME 2025 年与 2024 年收入")
    _add_completed_turn(service, conversation["id"], "再看利润", "秘密回答三", "分析 ACME 2025 年利润")
    _add_completed_turn(service, conversation["id"], "总结一下", "秘密回答四", "总结 ACME 的收入与利润")

    first = service.qa_service.summary_service.refresh(conversation["id"])

    assert first is not None
    assert first["through_sequence"] == 4
    assert first["status"] == "deterministic"
    assert first["summary"]["conversation_goals"] == [
        "分析 ACME 2025 年收入",
        "比较 ACME 2025 年与 2024 年收入",
    ]
    assert first["summary"]["time_ranges"] == ["2025 年", "2024 年"]
    assert "秘密回答" not in json.dumps(first["summary"], ensure_ascii=False)

    _add_completed_turn(service, conversation["id"], "继续", "秘密回答五", "继续总结 ACME")
    second = service.qa_service.summary_service.refresh(conversation["id"])

    assert second is not None
    assert second["through_sequence"] == 6
    assert second["id"] != first["id"]
    assert second["summary"]["conversation_goals"][-1] == "分析 ACME 2025 年利润"
    assert set(first["summary"]["source_message_ids"]).issubset(second["summary"]["source_message_ids"])


def test_run_freezes_summary_and_recent_turns_for_stable_replay(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation = service.create_conversation("ws_demo", "user_demo")
    for index in range(1, 5):
        _add_completed_turn(
            service,
            conversation["id"],
            f"问题 {index}",
            f"回答 {index}",
            f"规范问题 {index}",
        )
    first_summary = service.qa_service.summary_service.refresh(conversation["id"])
    assert first_summary is not None

    fifth = service.ask(conversation["id"], "问题 5", "ws_demo", "user_demo")
    with service.db.read() as conn:
        frozen_before = conn.execute(
            """SELECT summary_id,summary_json,summary_through_sequence,history_json,context_hash
               FROM run_context_snapshots WHERE run_id=?""",
            (fifth["run_id"],),
        ).fetchone()
    assert frozen_before is not None
    frozen_history = json.loads(frozen_before["history_json"])
    assert frozen_before["summary_id"] == first_summary["id"]
    assert frozen_before["summary_through_sequence"] == 4
    assert [item["content"] for item in frozen_history] == ["问题 3", "回答 3", "问题 4", "回答 4"]

    _complete_turn(service, fifth["assistant_message_id"], "回答 5", "规范问题 5")
    newer_summary = service.qa_service.summary_service.refresh(conversation["id"])
    assert newer_summary is not None
    assert newer_summary["id"] != first_summary["id"]

    with service.db.read() as conn:
        frozen_after = conn.execute(
            """SELECT summary_id,summary_json,summary_through_sequence,history_json,context_hash
               FROM run_context_snapshots WHERE run_id=?""",
            (fifth["run_id"],),
        ).fetchone()
    assert frozen_after is not None
    assert dict(frozen_after) == dict(frozen_before)


def test_summary_model_failure_keeps_deterministic_state(tmp_path: Path) -> None:
    service = _service(tmp_path, max_turns=1)
    service.qa_service.summary_service.model = FailingSummaryModel()
    conversation = service.create_conversation("ws_demo", "user_demo")
    _add_completed_turn(service, conversation["id"], "看 ACME", "回答一", "分析 ACME")
    _add_completed_turn(service, conversation["id"], "继续", "回答二", "继续分析 ACME")

    summary = service.qa_service.summary_service.refresh(conversation["id"])

    assert summary is not None
    assert summary["status"] == "deterministic_fallback"
    assert summary["summary"]["conversation_goals"] == ["分析 ACME"]
    assert "summary provider unavailable" not in json.dumps(summary["model"])


def test_out_of_order_completion_does_not_advance_summary_past_pending_gap(tmp_path: Path) -> None:
    service = _service(tmp_path, max_turns=1)
    conversation = service.create_conversation("ws_demo", "user_demo")
    first = service.ask(conversation["id"], "问题一", "ws_demo", "user_demo")
    second = service.ask(conversation["id"], "问题二", "ws_demo", "user_demo")
    third = service.ask(conversation["id"], "问题三", "ws_demo", "user_demo")
    _complete_turn(service, first["assistant_message_id"], "回答一", "规范问题一")
    _complete_turn(service, third["assistant_message_id"], "回答三", "规范问题三")

    assert service.qa_service.summary_service.refresh(conversation["id"]) is None

    _complete_turn(service, second["assistant_message_id"], "回答二", "规范问题二")
    summary = service.qa_service.summary_service.refresh(conversation["id"])

    assert summary is not None
    assert summary["through_sequence"] == 4
    assert summary["summary"]["conversation_goals"] == ["规范问题一", "规范问题二"]
