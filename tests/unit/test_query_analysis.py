from __future__ import annotations

from packages.qbr_core.evaluation_analysis import EvaluativeSignalAnalyzer
from packages.qbr_core.evidence import EvidencePack
from packages.qbr_core.negative_analysis import NegativeSignalAnalyzer
from packages.qbr_core.query_planning import deterministic_plan


def test_negative_analyzer_derives_trends_and_distinguishes_green_gates() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "financial-table",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s6",
            "slide_no": 6,
            "element_id": "e6",
            "chunk_type": "table",
            "content": (
                "核心财务指标 | 2023A | 2024A | 2025A | 同比/变化 | 单位\nShareholder capital ratio | 269% | 236% | 221% | -15ppt | %"
            ),
        },
        {
            "id": "green-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s7",
            "slide_no": 7,
            "element_id": "e7",
            "chunk_type": "table",
            "content": ("风险闸门 | 绿 | 黄 | 红 | 当前\nVONB增速 | >12% | 5-12% | <5% | 15%\n资本比率 | >210% | 190-210% | <190% | 221%"),
        },
        {
            "id": "management-action",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s7",
            "slide_no": 7,
            "element_id": "e8",
            "chunk_type": "text",
            "content": "强化高净值、保障与跨境服务，控制产品集中度。",
        },
    ]
    chart_rows = []
    for series_id, series_name, previous, current in (
        ("digital-stp", "Digital STP", 73.8, 72.1),
        ("persistency", "Persistency", 90.8, 90.1),
        ("other-markets", "其他市场", 134.0, 129.0),
    ):
        for point_order, category, value in ((1, "26/02", previous), (2, "26/03", current)):
            chart_rows.append(
                {
                    "series_id": series_id,
                    "series_name": series_name,
                    "point_order": point_order,
                    "category": category,
                    "y_value": value,
                    "display_value": str(value),
                    "document_version_id": "dv",
                    "document_id": "doc",
                    "slide_id": "s3",
                    "slide_no": 3,
                    "element_id": "shared-chart-element",
                    "chart_title": "Monthly KPI trends",
                }
            )

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=chart_rows)
    answer, evidence, warnings = assessment.render(plan)

    assert assessment.explicit_red_flags is False
    assert "No red/amber threshold breach" in answer
    assert "236%" in answer and "221%" in answer and "15 percentage points" in answer
    assert "Digital STP" in answer and "Persistency" in answer
    assert "其他市场" not in answer
    assert "控制产品集中度" in answer
    assert "enough relevant business evidence" not in answer
    assert {item["analysis_method"] for item in evidence} == {
        "structured_table_trend",
        "structured_chart_trend",
        "management_action_inference",
    }
    assert warnings == []


def test_execution_risk_explanation_synthesizes_definition_signals_and_gate_boundary() -> None:
    plan = deterministic_plan("所谓的执行风险具体是指什么？")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "capital-trend",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s2",
            "slide_no": 2,
            "element_id": "e2",
            "chunk_type": "table",
            "content": "Metric | 2024A | 2025A | Change\nShareholder capital ratio | 236% | 221% | -15ppt",
        },
        {
            "id": "risk-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s5",
            "slide_no": 5,
            "element_id": "e5",
            "chunk_type": "table",
            "content": "风险闸门 | 绿 | 黄 | 红 | 当前\n资本比率 | >210% | 190-210% | <190% | 221%",
        },
        {
            "id": "management-action",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s6",
            "slide_no": 6,
            "element_id": "e6",
            "chunk_type": "text",
            "content": "强化高净值、保障与跨境服务，控制产品集中度。",
        },
    ]
    chart_rows = [
        {
            "series_id": "persistency",
            "series_name": "Persistency 13M",
            "point_order": point_order,
            "category": period,
            "y_value": value,
            "display_value": f"{value}%",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s3",
            "slide_no": 3,
            "element_id": "execution-chart",
            "chart_title": "Monthly execution indicators",
        }
        for point_order, period, value in ((1, "26/02", 90.8), (2, "26/03", 89.4))
    ]

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=chart_rows)
    answer, evidence, warnings = assessment.render_explanation(plan)

    assert "## 直接解释" in answer
    assert "既定经营目标和优先事项在落地过程中偏离计划" in answer
    assert "## 在这份 PPT 中的具体表现" in answer
    assert "Persistency 13M" in answer
    assert "控制产品集中度" in answer
    assert "## 如何判断是否升级为实际问题" in answer
    assert "绿色也不等于未来没有风险" in answer
    assert "24/10=55.5" not in answer
    assert any(item.get("element_id") == "execution-chart" for item in evidence)
    assert warnings == []


def test_negative_analyzer_reports_no_red_flags_instead_of_insufficient_evidence() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "green-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "table",
            "content": "Risk gate | Green | Amber | Red | Current\nCapital ratio | >210% | 190-210% | <190% | 221%",
        }
    ]

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert answer.startswith("No explicit negative result or breached threshold")
    assert "misleading to manufacture bad news" in answer
    assert len(evidence) == 1
    assert warnings == []


def test_negative_analyzer_distinguishes_no_adverse_signal_from_no_business_content() -> None:
    plan = deterministic_plan("what is the bad news in this ppt")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "positive-business-content",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "text",
            "content": "Revenue growth remained above target and the capital ratio stayed within the green range.",
        }
    ]

    assessment = NegativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert answer.startswith("No explicit negative statement")
    assert "does not support a specific bad-news claim" in answer
    assert "enough relevant business evidence" not in answer
    assert evidence == []
    assert warnings == []


def test_evaluative_analyzer_derives_strengths_and_balances_counterevidence() -> None:
    plan = deterministic_plan("公司的优势在哪些点上")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "financial-table",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s2",
            "slide_no": 2,
            "element_id": "e2",
            "chunk_type": "table",
            "content": (
                "核心财务指标 | 2024A | 2025A | 同比/变化 | 单位\n"
                "VONB / 新业务价值 | 4,712 | 5,516 | +15% CER | US$m\n"
                "Net FSG / 净自由盈余产生 | 4,020 | 4,451 | +14%/股 | US$m\n"
                "Shareholder capital ratio | 236% | 221% | -15ppt | %"
            ),
        },
        {
            "id": "green-gates",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s3",
            "slide_no": 3,
            "element_id": "e3",
            "chunk_type": "table",
            "content": "指标 | 当前 | 阈值\n最大市场占比 | 41% | <45%\n组合增长 | +15% | >10%",
        },
    ]
    categorical_chart = [
        {
            "series_id": "fy2024",
            "series_name": "FY2024",
            "point_order": 1,
            "category": "Singapore",
            "y_value": 380,
            "document_version_id": "dv",
            "slide_id": "s4",
            "slide_no": 4,
            "element_id": "e4",
        },
        {
            "series_id": "fy2024",
            "series_name": "FY2024",
            "point_order": 2,
            "category": "Other markets",
            "y_value": 1072,
            "document_version_id": "dv",
            "slide_id": "s4",
            "slide_no": 4,
            "element_id": "e4",
        },
    ]

    assessment = EvaluativeSignalAnalyzer().analyze(
        plan,
        explicit_pack=empty_pack,
        chunks=chunks,
        chart_rows=categorical_chart,
    )
    answer, evidence, warnings = assessment.render(plan)

    assert "公司的优势主要体现在" in answer
    assert "VONB" in answer and "Net FSG" in answer
    assert "最大市场占比" in answer
    assert "Shareholder capital ratio" in answer and "需要平衡看待" in answer
    assert "Singapore" not in answer and "Other markets" not in answer
    assert {item["analysis_method"] for item in evidence} == {
        "structured_table_improvement",
        "structured_threshold_strength",
        "structured_table_trend",
    }
    assert warnings == []


def test_evaluative_analyzer_does_not_claim_strength_without_comparison_basis() -> None:
    plan = deterministic_plan("What are the company's strengths?")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "descriptive",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "text",
            "content": "The company operates through agency and partnership channels across several markets.",
        }
    ]

    assessment = EvaluativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert answer.startswith("The document contains business content")
    assert "does not justify a company-strength claim" in answer
    assert evidence == []
    assert warnings == ["NO_COMPARATIVE_STRENGTH_EVIDENCE"]


def test_evaluative_analyzer_turns_management_actions_into_evidenced_opportunities() -> None:
    plan = deterministic_plan("下一阶段有哪些增长机会？")
    empty_pack = EvidencePack((), (), plan.required_facets, False)
    chunks = [
        {
            "id": "action",
            "document_version_id": "dv",
            "document_id": "doc",
            "slide_id": "s1",
            "slide_no": 1,
            "element_id": "e1",
            "chunk_type": "text",
            "content": "降低新业务资本强度，提升净FSG转化。",
        }
    ]

    assessment = EvaluativeSignalAnalyzer().analyze(plan, explicit_pack=empty_pack, chunks=chunks, chart_rows=[])
    answer, evidence, warnings = assessment.render(plan)

    assert "可执行的改进或增长机会" in answer
    assert "降低新业务资本强度" in answer
    assert evidence[0]["analysis_method"] == "management_action_opportunity"
    assert warnings == []
