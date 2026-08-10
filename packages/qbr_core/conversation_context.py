from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .conversation_summary import SUMMARY_VERSION

CONTEXT_VERSION = "recent-complete-turns-v1"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    """Estimate prompt tokens without coupling context storage to one tokenizer."""

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk
    return cjk + math.ceil(non_cjk / 4)


def _truncate_to_token_budget(text: str, budget: int) -> tuple[str, bool]:
    if budget <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= budget:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    suffix = "…"
    clipped = text[:low].rstrip()
    while clipped and estimate_tokens(clipped + suffix) > budget:
        clipped = clipped[:-1].rstrip()
    return clipped + suffix if clipped else suffix, True


@dataclass(frozen=True, slots=True)
class ConversationContext:
    summary: dict[str, Any]
    history: tuple[dict[str, str], ...]
    diagnostics: dict[str, Any]

    def history_list(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.history]


class ConversationContextAssembler:
    """Build and persist the immutable dialogue context used by one answer run."""

    def __init__(self, *, max_turns: int, token_budget: int, summary_token_budget: int = 800) -> None:
        if max_turns < 1 or token_budget < 1 or summary_token_budget < 1:
            raise ValueError("Conversation context limits must be positive")
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.summary_token_budget = min(summary_token_budget, token_budget)

    def load_or_create(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        conversation_id: str,
        current_user_message_id: str,
        cutoff_sequence: int,
        created_at: str,
    ) -> ConversationContext:
        existing = conn.execute(
            """SELECT summary_json,history_json,diagnostics_json
               FROM run_context_snapshots WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if existing:
            return self._deserialize(
                existing["summary_json"],
                existing["history_json"],
                existing["diagnostics_json"],
            )

        context = self.build(
            conn,
            conversation_id=conversation_id,
            current_user_message_id=current_user_message_id,
            cutoff_sequence=cutoff_sequence,
        )
        conn.execute(
            """INSERT INTO run_context_snapshots(
                 run_id,context_version,current_user_message_id,cutoff_sequence,
                 summary_id,summary_json,summary_through_sequence,
                 history_json,diagnostics_json,context_hash,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                CONTEXT_VERSION,
                current_user_message_id,
                cutoff_sequence,
                context.diagnostics.get("summary_id"),
                json.dumps(context.summary, ensure_ascii=False, separators=(",", ":")),
                context.diagnostics.get("summary_through_sequence", 0),
                json.dumps(context.history, ensure_ascii=False, separators=(",", ":")),
                json.dumps(context.diagnostics, ensure_ascii=False, separators=(",", ":")),
                context.diagnostics["context_hash"],
                created_at,
            ),
        )
        return context

    def build(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        current_user_message_id: str,
        cutoff_sequence: int,
    ) -> ConversationContext:
        summary_row = conn.execute(
            """SELECT id,through_sequence,summary_json,status
               FROM conversation_summaries
               WHERE conversation_id=? AND summary_version=? AND through_sequence<?
               ORDER BY through_sequence DESC,created_at DESC LIMIT 1""",
            (conversation_id, SUMMARY_VERSION, cutoff_sequence),
        ).fetchone()
        summary = self._fit_summary_to_budget(
            json.loads(summary_row["summary_json"] or "{}") if summary_row else {},
            self.summary_token_budget,
        )
        summary_tokens = estimate_tokens(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        summary_through_sequence = int(summary_row["through_sequence"]) if summary_row else 0
        rows = conn.execute(
            """SELECT
                 u.id user_id,u.content user_content,u.sequence_no user_sequence,
                 a.id assistant_id,a.content assistant_content,a.sequence_no assistant_sequence,
                 count(*) OVER () total_turns
               FROM messages u JOIN messages a
                 ON a.conversation_id=u.conversation_id
                AND a.sequence_no=u.sequence_no+1
                AND a.role='assistant' AND a.status='completed'
               WHERE u.conversation_id=? AND u.role='user' AND u.status='completed'
                 AND u.sequence_no>? AND u.sequence_no<?
               ORDER BY u.sequence_no DESC
               LIMIT ?""",
            (conversation_id, summary_through_sequence, cutoff_sequence, self.max_turns + 1),
        ).fetchall()

        selected_newest_first: list[tuple[dict[str, str], dict[str, str]]] = []
        remaining = max(0, self.token_budget - summary_tokens)
        truncated = False

        for row in rows[: self.max_turns]:
            if remaining <= 0:
                break
            user_content = str(row["user_content"])
            assistant_content = str(row["assistant_content"])
            turn_tokens = estimate_tokens(user_content) + estimate_tokens(assistant_content)
            if turn_tokens <= remaining:
                user_text, assistant_text = user_content, assistant_content
            elif selected_newest_first:
                break
            else:
                user_budget = min(remaining, max(1, remaining // 3))
                assistant_budget = max(0, remaining - user_budget)
                user_text, user_truncated = _truncate_to_token_budget(user_content, user_budget)
                assistant_text, assistant_truncated = _truncate_to_token_budget(assistant_content, assistant_budget)
                truncated = user_truncated or assistant_truncated

            selected_newest_first.append(
                (
                    {"id": str(row["user_id"]), "role": "user", "content": user_text},
                    {"id": str(row["assistant_id"]), "role": "assistant", "content": assistant_text},
                )
            )
            remaining -= estimate_tokens(user_text) + estimate_tokens(assistant_text)
            if remaining <= 0:
                break

        history = tuple(message for turn in reversed(selected_newest_first) for message in turn)
        total_turns = int(rows[0]["total_turns"]) if rows else 0
        history_estimated_tokens = sum(estimate_tokens(item["content"]) for item in history)
        hash_payload = {
            "version": CONTEXT_VERSION,
            "conversation_id": conversation_id,
            "current_user_message_id": current_user_message_id,
            "cutoff_sequence": cutoff_sequence,
            "summary": summary,
            "history": history,
        }
        context_hash = hashlib.sha256(
            json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        diagnostics: dict[str, Any] = {
            "version": CONTEXT_VERSION,
            "cutoff_sequence": cutoff_sequence,
            "selected_turns": len(selected_newest_first),
            "selected_messages": len(history),
            "selected_message_ids": [item["id"] for item in history],
            "estimated_tokens": summary_tokens + history_estimated_tokens,
            "history_estimated_tokens": history_estimated_tokens,
            "summary_estimated_tokens": summary_tokens,
            "token_budget": self.token_budget,
            "summary_token_budget": self.summary_token_budget,
            "max_turns": self.max_turns,
            "available_turns": total_turns,
            "dropped_turns": max(0, total_turns - len(selected_newest_first)),
            "truncated": truncated,
            "summary_id": str(summary_row["id"]) if summary_row else None,
            "summary_version": SUMMARY_VERSION if summary_row else None,
            "summary_status": str(summary_row["status"]) if summary_row else None,
            "summary_through_sequence": summary_through_sequence,
            "context_hash": context_hash,
        }
        return ConversationContext(summary=summary, history=history, diagnostics=diagnostics)

    @staticmethod
    def _deserialize(
        summary_json: str,
        history_json: str,
        diagnostics_json: str,
    ) -> ConversationContext:
        raw_summary = json.loads(summary_json or "{}")
        raw_history = json.loads(history_json or "[]")
        raw_diagnostics = json.loads(diagnostics_json or "{}")
        history = tuple(
            {
                "id": str(item.get("id", "")),
                "role": str(item.get("role", "user")),
                "content": str(item.get("content", "")),
            }
            for item in raw_history
            if isinstance(item, dict)
        )
        summary = dict(raw_summary) if isinstance(raw_summary, dict) else {}
        return ConversationContext(summary=summary, history=history, diagnostics=dict(raw_diagnostics))

    @staticmethod
    def _fit_summary_to_budget(summary: Any, budget: int) -> dict[str, Any]:
        if not isinstance(summary, dict):
            return {}
        fitted = json.loads(json.dumps(summary, ensure_ascii=False))

        def size() -> int:
            return estimate_tokens(
                json.dumps(fitted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )

        removable_fields = (
            "source_message_ids",
            "entities",
            "time_ranges",
            "terminology",
            "user_preferences",
            "pending_questions",
            "conversation_goals",
            "resolved_references",
            "document_scope",
        )
        while size() > budget:
            removed = False
            for field in removable_fields:
                values = fitted.get(field)
                if isinstance(values, list) and values:
                    values.pop(0)
                    removed = True
                    break
            if not removed:
                return {}
        return fitted


__all__ = ["CONTEXT_VERSION", "ConversationContext", "ConversationContextAssembler", "estimate_tokens"]
