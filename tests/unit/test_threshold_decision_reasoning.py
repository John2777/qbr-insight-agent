from __future__ import annotations

from packages.qbr_core.analysis.table_reasoning import TableReasoner


def table_source(content: str) -> dict[str, object]:
    return {
        "id": "threshold_table",
        "chunk_type": "table",
        "content": content,
        "slide_title": "Decision evidence",
    }


def test_table_reasoner_uses_relevant_threshold_table_for_implicit_risk_evaluation() -> None:
    source = table_source(
        "指标 | 当前 | 阈值\n"
        "单项占比 | 43% | <45%\n"
        "前两项占比 | 68% | <70%\n"
        "组合变化 | 12% | >10%"
    )

    result = TableReasoner().answer("在其中一项快速增长后，组合是否已经形成明显的集中风险？", [source])

    assert result is not None
    assert result.operation == "threshold"
    assert "全部满足阈值" in result.answer
    assert all(value in result.answer for value in ("43%", "<45%", "68%", "<70%", "12%", ">10%"))


def test_table_reasoner_reports_breach_for_implicit_english_limit_evaluation() -> None:
    source = table_source(
        "Metric | Current | Threshold\n"
        "Largest share | 52% | <50%\n"
        "Portfolio change | 9% | >8%"
    )

    result = TableReasoner().answer("Has the recent change created a material concentration risk?", [source])

    assert result is not None
    assert result.operation == "threshold"
    assert "并非全部达标" in result.answer
    assert "52%" in result.answer and "<50%" in result.answer


def test_regression_regional_concentration_question_uses_all_stated_controls() -> None:
    source = table_source(
        "指标 | 当前 | 阈值\n"
        "最大市场占比 | 41% | <45%\n"
        "Top-2占比 | 62% | <70%\n"
        "组合增长 | +15% | >10%"
    )
    question = "香港VONB增长28%后，集团是否已经形成明显的区域集中风险？请结合当前值和阈值判断。"

    result = TableReasoner().answer(question, [source])

    assert result is not None
    assert result.operation == "threshold"
    assert "全部满足阈值" in result.answer
    assert all(value in result.answer for value in ("41%", "<45%", "62%", "<70%", "+15%", ">10%"))
    assert "没有风险" not in result.answer
