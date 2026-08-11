from __future__ import annotations

from typing import Any

from packages.qbr_core.analysis.table_reasoning import ReasoningResult, TableReasoner

_REASONER = TableReasoner()


def answer(question: str, sources: list[dict[str, Any]]) -> ReasoningResult | None:
    return _REASONER.answer(question, sources)
