from __future__ import annotations

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.analysis.calculations import ChartCalculator, VerifiedCalculation
from packages.qbr_core.analysis.table_reasoning import TableReasoner


def _table_source(content: str) -> dict[str, object]:
    return {"id": "table", "chunk_type": "table", "content": content, "slide_title": "Core metrics"}


def _chart_point(
    *,
    series: str,
    category: str,
    value: float,
    order: int,
    slide_id: str,
    chart_type: str = "barChart",
) -> dict[str, object]:
    return {
        "id": f"{slide_id}_{series}_{order}",
        "series_id": series,
        "series_name": series,
        "category": category,
        "y_value": value,
        "point_order": order,
        "chart_id": f"chart_{slide_id}",
        "chart_type": chart_type,
        "chart_title": "Structured performance chart",
        "slide_title": "Structured performance chart",
        "slide_summary": "Structured chart test",
        "axis_id": "primary",
        "unit": "General",
        "document_version_id": "dv",
        "document_id": "doc",
        "slide_id": slide_id,
        "slide_no": 5,
        "element_id": f"element_{slide_id}",
        "bbox_json": "{}",
        "chart_confidence": 1.0,
        "series_confidence": 1.0,
        "confidence": 1.0,
        "source_kind": "embedded_workbook",
        "document_title": "Generic deck",
    }


def test_table_reasoner_does_not_collapse_a_multi_metric_question_to_the_first_row() -> None:
    source = _table_source(
        "Metric | FY2024 | FY2025 | YoY\n"
        "Revenue | 100 | 120 | +20%\n"
        "Profit | 20 | 24 | +20%"
    )

    assert TableReasoner().answer("Revenue和Profit分别同比增长多少？", [source]) is None


def test_chart_calculator_attributes_group_change_across_generic_segments() -> None:
    values = {
        "FY2024": (("North", 100.0), ("South", 80.0), ("West", 40.0)),
        "FY2025": (("North", 150.0), ("South", 105.0), ("West", 35.0)),
    }
    rows = [
        _chart_point(series=period, category=segment, value=value, order=index, slide_id="segment_growth")
        for period, points in values.items()
        for index, (segment, value) in enumerate(points)
    ]

    result = ChartCalculator().analyze("Which regional segments mainly contributed to year-over-year growth?", rows)

    assert result is not None
    assert result.scope["operation"] == "group_change_attribution"
    assert "primary contributors=North, South" in result.text
    assert "North=+50" in result.text and "South=+25" in result.text and "West=-5" in result.text
    assert result.facts[0]["primary_contributors"] == ["North", "South"]


def test_chart_calculator_anchors_yoy_to_the_document_reporting_year() -> None:
    rows = [
        {
            **_chart_point(
                series="Value Margin",
                category=period,
                value=value,
                order=index,
                chart_type="lineChart",
                slide_id="quality_trend",
            ),
            "document_title": "FY2025 performance review",
        }
        for index, (period, value) in enumerate((("24/12", 53.4), ("25/12", 56.1), ("26/03", 56.0)))
    ]

    result = ChartCalculator().analyze("Value Margin同比增长多少？", rows)

    assert result is not None
    assert result.scope["operation"] == "period_change"
    assert "24/12=53.4" in result.text and "25/12=56.1" in result.text
    assert "absolute change=2.7" in result.text and "relative change=5.1%" in result.text


def test_multi_facet_merge_preserves_cross_slide_evidence() -> None:
    calculation = VerifiedCalculation(
        text="verified",
        evidence=({"slide_id": "target", "element_id": "chart", "quote": "target point"},),
        scope={"kind": "chart_calculation", "slide_id": "target"},
    )
    pack = [
        {"slide_id": "target", "element_id": "table", "quote": "supporting context"},
        {"slide_id": "other", "element_id": "other", "quote": "second requested facet"},
    ]

    merged = DeterministicAnswerEngine._merge_evidence(calculation, pack, preserve_cross_scope=True)

    assert [item["slide_id"] for item in merged] == ["target", "target", "other"]
