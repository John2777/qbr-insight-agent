"""Tests for deterministic evidence-preserving answer formatting."""

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine


def test_chart_quote_compaction_prefers_the_line_with_verifiable_points() -> None:
    quote = "Revenue & margin\nRevenue | primary | $m | Q1=10; Q2=20; Q3=30"

    compact = DeterministicAnswerEngine._compact_chart_quote(quote)

    assert "Q2=20" in compact
    assert compact.startswith("Revenue | primary")
