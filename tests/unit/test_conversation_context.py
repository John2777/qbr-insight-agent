from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from packages.qbr_core import QBRService
from packages.qbr_core.config import Settings
from packages.qbr_core.conversation_context import estimate_tokens
from packages.qbr_core.db import Database


def _service(tmp_path: Path, *, max_turns: int = 4, token_budget: int = 2400) -> QBRService:
    return QBRService(
        Settings(
            tmp_path,
            tmp_path / "app.sqlite3",
            tmp_path / "objects",
            run_inline_worker=False,
            conversation_context_max_turns=max_turns,
            conversation_context_token_budget=token_budget,
        )
    )


def _complete_assistant(service: QBRService, message_id: str, content: str) -> None:
    with service.db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE messages SET status='completed',content=? WHERE id=?",
            (content, message_id),
        )


def _snapshot(service: QBRService, run_id: str) -> tuple[list[dict[str, str]], dict[str, object]]:
    with service.db.read() as conn:
        row = conn.execute(
            "SELECT history_json,diagnostics_json FROM run_context_snapshots WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row is not None
    return json.loads(row["history_json"]), json.loads(row["diagnostics_json"])


def test_run_context_is_frozen_at_enqueue_and_excludes_current_and_future_turns(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation = service.create_conversation("ws_demo", "user_demo")

    first = service.ask(conversation["id"], "第一问", "ws_demo", "user_demo")
    _complete_assistant(service, first["assistant_message_id"], "第一答")
    second = service.ask(conversation["id"], "第二问", "ws_demo", "user_demo")
    second_before, second_diagnostics = _snapshot(service, second["run_id"])

    _complete_assistant(service, second["assistant_message_id"], "第二答")
    third = service.ask(conversation["id"], "第三问", "ws_demo", "user_demo")
    second_after, _ = _snapshot(service, second["run_id"])
    third_history, third_diagnostics = _snapshot(service, third["run_id"])

    assert [(item["role"], item["content"]) for item in second_before] == [
        ("user", "第一问"),
        ("assistant", "第一答"),
    ]
    assert second_after == second_before
    assert [(item["role"], item["content"]) for item in third_history] == [
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
        ("assistant", "第二答"),
    ]
    assert second_diagnostics["cutoff_sequence"] == 3
    assert third_diagnostics["cutoff_sequence"] == 5
    assert all(item["content"] != "第三问" for item in third_history)


def test_same_timestamp_questions_use_explicit_message_binding_and_sequence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation = service.create_conversation("ws_demo", "user_demo")

    with patch("packages.qbr_core.qa_service.utc_now", return_value="2026-08-10T12:00:00.000Z"):
        first = service.ask(conversation["id"], "same-time first", "ws_demo", "user_demo")
        second = service.ask(conversation["id"], "same-time second", "ws_demo", "user_demo")

    with service.db.read() as conn:
        rows = conn.execute(
            "SELECT id,sequence_no FROM messages WHERE conversation_id=? ORDER BY sequence_no",
            (conversation["id"],),
        ).fetchall()
        run = conn.execute("SELECT user_message_id,context_cutoff_sequence FROM runs WHERE id=?", (second["run_id"],)).fetchone()

    assert [row["sequence_no"] for row in rows] == [1, 2, 3, 4]
    assert run is not None
    assert run["user_message_id"] == second["user_message_id"]
    assert run["user_message_id"] != first["user_message_id"]
    assert run["context_cutoff_sequence"] == 3

    assert service.process_next_run("same-time-test") == first["run_id"]
    assert service.process_next_run("same-time-test") == second["run_id"]
    result = service.get_conversation(conversation["id"], "ws_demo", "user_demo")
    answer = next(item for item in result["messages"] if item["id"] == second["assistant_message_id"])
    assert answer["metadata"]["conversation_context"]["original_question"] == "same-time second"


def test_context_budget_truncates_the_newest_complete_turn(tmp_path: Path) -> None:
    service = _service(tmp_path, token_budget=20)
    conversation = service.create_conversation("ws_demo", "user_demo")
    first = service.ask(conversation["id"], "用" * 80, "ws_demo", "user_demo")
    _complete_assistant(service, first["assistant_message_id"], "答" * 80)

    second = service.ask(conversation["id"], "继续", "ws_demo", "user_demo")
    history, diagnostics = _snapshot(service, second["run_id"])

    assert [item["role"] for item in history] == ["user", "assistant"]
    assert sum(estimate_tokens(item["content"]) for item in history) <= 20
    assert diagnostics["estimated_tokens"] <= 20
    assert diagnostics["truncated"] is True


def test_database_migrates_message_sequence_and_run_question_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE conversations (
          id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
          title TEXT NOT NULL, scope_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
          id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
          status TEXT NOT NULL, run_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE runs (
          id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
          assistant_message_id TEXT NOT NULL, status TEXT NOT NULL, warning_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL, completed_at TEXT
        );
        INSERT INTO conversations VALUES ('conv_old','ws_demo','user_demo','Old','{}','2026-01-01','2026-01-01');
        INSERT INTO messages VALUES ('msg_user','conv_old','user','old question','completed',NULL,'{}','2026-01-01');
        INSERT INTO messages VALUES ('msg_assistant','conv_old','assistant','','running','run_old','{}','2026-01-01');
        INSERT INTO runs VALUES ('run_old','ws_demo','conv_old','msg_assistant','pending','[]','2026-01-01',NULL);
        """
    )
    conn.close()

    database = Database(database_path)
    database.initialize()

    with database.read() as migrated:
        messages = migrated.execute(
            "SELECT id,sequence_no FROM messages WHERE conversation_id='conv_old' ORDER BY sequence_no"
        ).fetchall()
        run = migrated.execute(
            "SELECT user_message_id,context_cutoff_sequence FROM runs WHERE id='run_old'"
        ).fetchone()
        index_names = {row[1] for row in migrated.execute("PRAGMA index_list(messages)").fetchall()}
        summary_table = migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_summaries'"
        ).fetchone()
        snapshot_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(run_context_snapshots)").fetchall()
        }

    assert [(row["id"], row["sequence_no"]) for row in messages] == [("msg_user", 1), ("msg_assistant", 2)]
    assert run is not None
    assert run["user_message_id"] == "msg_user"
    assert run["context_cutoff_sequence"] == 1
    assert "idx_messages_conversation_sequence" in index_names
    assert summary_table is not None
    assert {"summary_id", "summary_json", "summary_through_sequence"}.issubset(snapshot_columns)
