from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.analysis.calculations import ChartCalculator, VerifiedCalculation
from packages.qbr_core.analysis.charts.analyzer import ChartAnalyzer
from packages.qbr_core.analysis.table_reasoning import TableReasoner
from packages.qbr_core.foundation.database import Database
from packages.qbr_core.planning import deterministic_plan
from packages.qbr_core.retrieval.engine import EvidenceRetriever, query_terms


def table_source(content: str) -> dict[str, object]:
    return {
        "id": "chunk_1",
        "chunk_type": "table",
        "content": content,
        "slide_title": "CORE FINANCIALS",
    }


def test_table_reasoner_handles_bilingual_metric_and_time_difference() -> None:
    source = table_source(
        "指标 | 2023A | 2024A | 2025A | 同比/变化 | 单位\n"
        "Operating ROEV / 营运ROEV | 12.9% | 14.9% | 15.8% | +0.9ppt | %\n"
        "Operating ROE / 营运ROE | 13.5% | 14.8% | 15.5% | +0.7ppt | %\n"
        "Shareholder capital ratio | 269% | 236% | 221% | -15ppt | %"
    )
    reasoner = TableReasoner()

    roev = reasoner.answer("Operating ROEV从2024A到2025A提高了多少个百分点？", [source])
    capital = reasoner.answer("股东资本比率从2023A到2025A累计下降了多少个百分点？", [source])

    assert roev and "14.9%" in roev.answer and "15.8%" in roev.answer and "提高0.9个百分点" in roev.answer
    assert capital and "269%" in capital.answer and "221%" in capital.answer and "下降48个百分点" in capital.answer


def test_table_reasoner_prefers_compound_lookup_over_recomputing_share() -> None:
    source = table_source(
        "资本用途 | 年度额 US$m | 占比 | 预期ROE | 流动性 | 风险限额利用\n"
        "股份回购 | 1,743 | 24% | 15.5% | 高 | 82%\n"
        "合计 | 7,292 | 100% | - | - | -"
    )

    result = TableReasoner().answer(
        "股份回购的年度额、占比、预期ROE、流动性和风险限额利用分别是什么？", [source]
    )

    assert result
    assert all(value in result.answer for value in ("1,743", "24%", "15.5%", "流动性为高", "82%"))


def test_table_reasoner_filters_and_aggregates_rows() -> None:
    source = table_source(
        "资本用途 | 年度额 US$m | 占比 | 流动性 | 期限\n"
        "新业务投资 | 2,314 | 32% | 低 | 长\n"
        "股息 | 2,480 | 34% | 高 | 短\n"
        "股份回购 | 1,743 | 24% | 高 | 短\n"
        "战略选择权 | 180 | 2% | 中 | 长\n"
        "缓冲/其他 | 155 | 2% | 高 | 短\n"
        "合计 | 7,292 | 100% | — | —"
    )
    reasoner = TableReasoner()

    long_term = reasoner.answer("期限为长的用途有哪些？各自年度额是多少？", [source])
    liquid = reasoner.answer("流动性为高的用途年度额合计多少，占总额多少？", [source])

    assert long_term and all(value in long_term.answer for value in ("新业务投资", "2,314", "战略选择权", "180"))
    assert liquid and all(value in liquid.answer for value in ("股息", "股份回购", "缓冲/其他", "4,378", "60%"))


def test_legacy_table_chunk_is_repaired_from_structured_cells() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE elements(id TEXT PRIMARY KEY, structured_json TEXT NOT NULL);
        CREATE TABLE chunks(
          id TEXT PRIMARY KEY, element_id TEXT, workspace_id TEXT, chunk_type TEXT,
          content TEXT, content_hash TEXT
        );
        CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED, workspace_id UNINDEXED, content);
        """
    )
    structured = {"rows": [["市场", "VONB"], ["香港", "2,256"]]}
    conn.execute("INSERT INTO elements VALUES (?,?)", ("element_1", json.dumps(structured)))
    conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)", ("chunk_1", "element_1", "ws", "table", "市场\nVONB", "old"))
    conn.execute("INSERT INTO chunk_fts VALUES (?,?,?)", ("chunk_1", "ws", "市场\nVONB"))

    Database._repair_table_chunks(conn)

    assert conn.execute("SELECT content FROM chunks").fetchone()[0] == "市场 | VONB\n香港 | 2,256"
    assert conn.execute("SELECT content FROM chunk_fts").fetchone()[0] == "市场 | VONB\n香港 | 2,256"


def test_exact_value_constraints_are_preserved_for_semantic_planning() -> None:
    plan = deterministic_plan("2026年第二季度日本市场的VONB精确值是多少？")
    assert "2026年" in plan.hard_constraints
    assert "第二季度" in plan.hard_constraints
    assert plan.retrieval_queries[0].text == "2026年第二季度日本市场的VONB精确值是多少？"


def test_chart_comparison_and_split_dual_axis_inference() -> None:
    common = {
        "category": "26/03",
        "y_value": 0.0,
        "point_order": 1,
        "chart_title": None,
        "chart_type": "lineChart",
        "slide_title": "双轴控制图",
        "slide_summary": "右轴 · 质量/效率",
        "visual_json": '{"axis":{"role":"primary"}}',
        "axes_json": "[]",
        "axis_id": "axis",
        "unit": "0",
        "document_version_id": "dv",
        "slide_id": "slide",
        "element_id": "chart",
        "bbox_json": "{}",
        "chart_confidence": 1.0,
        "series_confidence": 1.0,
        "confidence": 1.0,
        "source_kind": "embedded_workbook",
        "document_title": "Growth",
    }
    rows = [
        {**common, "id": "p1", "series_id": "hk", "series_name": "香港", "y_value": 179.0},
        {**common, "id": "p2", "series_id": "cn", "series_name": "中国内地", "y_value": 184.0},
    ]

    result = ChartCalculator().analyze("26/03中国内地和香港哪个更高？高多少？", rows)

    assert result is not None
    assert "higher series=中国内地" in result.text and "difference=5" in result.text
    assert len(result.evidence) == 2


def _generic_chart_point(
    *,
    series_id: str,
    series_name: str,
    category: str,
    value: float,
    order: int,
    chart_type: str = "barChart",
    slide_id: str = "generic_slide",
) -> dict[str, object]:
    return {
        "id": f"{series_id}_{order}",
        "series_id": series_id,
        "series_name": series_name,
        "category": category,
        "y_value": value,
        "point_order": order,
        "chart_id": f"chart_{slide_id}",
        "chart_type": chart_type,
        "chart_title": "Generic structured chart",
        "slide_title": "Generic structured chart",
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


def test_chart_calculator_compares_two_series_at_one_category_without_domain_rules() -> None:
    rows = [
        _generic_chart_point(series_id=series, series_name=series, category=category, value=value, order=index)
        for series, values in (("Baseline", (10.0, 40.0)), ("Current", (15.0, 50.0)))
        for index, (category, value) in enumerate(zip(("Alpha", "Beta"), values, strict=True))
    ]

    result = ChartCalculator().analyze("Alpha Current 比 Baseline 增加多少，增长率是多少？", rows)

    assert result is not None
    assert result.scope["operation"] == "cross_dimension_change"
    assert "Baseline=10" in result.text and "Current=15" in result.text
    assert "absolute change=5" in result.text and "relative change=50.0%" in result.text
    assert result.facts[-1]["category"] == "Alpha"


def test_chart_calculator_treats_single_series_share_points_as_segments() -> None:
    rows = [
        _generic_chart_point(
            series_id="mix",
            series_name="Mix",
            category=category,
            value=value,
            order=index,
            chart_type="doughnutChart",
            slide_id="share_slide",
        )
        for index, (category, value) in enumerate((("Alpha", 43.0), ("Beta", 24.0), ("Gamma", 14.0), ("Delta", 19.0)))
    ]

    result = ChartCalculator().analyze("这个占比结构里 Alpha 和非 Alpha 部分各是多少？", rows)

    assert result is not None
    assert result.scope["operation"] == "share_composition"
    assert "Alpha=43" in result.text and "complement=57" in result.text
    assert result.facts[-1]["operation"] == "share_complement"
    assert len(result.evidence) == 1


def test_chart_calculator_derives_adjacent_steps_for_any_ordered_category_path() -> None:
    rows = [
        _generic_chart_point(
            series_id="bridge",
            series_name="Bridge",
            category=category,
            value=value,
            order=index,
            slide_id="path_slide",
        )
        for index, (category, value) in enumerate((("Start", 72.0), ("Build", 91.0), ("Use A", 84.0), ("Use B", 76.0), ("End", 68.0)))
    ]

    result = ChartCalculator().analyze("请分析这个路径，最大的单步下降是什么？", rows)

    assert result is not None
    assert result.scope["operation"] == "sequential_path"
    assert "Use A→Use B (-8)" in result.text
    assert result.facts[0]["largest_decline"] == {"from": "Use A", "to": "Use B", "delta": -8.0}


def test_chart_analyzer_accepts_single_series_multi_category_structures() -> None:
    share_rows = [
        _generic_chart_point(
            series_id="mix",
            series_name="Mix",
            category=category,
            value=value,
            order=index,
            chart_type="pieChart",
            slide_id="share_analysis",
        )
        for index, (category, value) in enumerate((("North", 60.0), ("South", 40.0)))
    ]
    question = "请分析这张饼图的构成"

    result = ChartAnalyzer().analyze(question, share_rows, plan=deterministic_plan(question))

    assert result is not None
    assert result.scope["selection"]["analytical_entity_count"] == 2
    assert any(fact.get("operation") == "share_composition" for fact in result.facts)
    assert "North=60" in result.fallback_text and "South=40" in result.fallback_text


def test_calculation_evidence_narrows_retrieval_to_the_selected_slide() -> None:
    calculation = VerifiedCalculation(
        text="verified",
        evidence=({"slide_id": "target", "element_id": "chart", "quote": "target point"},),
        scope={"kind": "chart_calculation", "slide_id": "target"},
    )
    pack = [
        {"slide_id": "target", "element_id": "table", "quote": "supporting context"},
        {"slide_id": "other", "element_id": "other", "quote": "irrelevant result"},
    ]

    merged = DeterministicAnswerEngine._merge_evidence(calculation, pack)

    assert [item["slide_id"] for item in merged] == ["target", "target"]
    assert all(item["quote"] != "irrelevant result" for item in merged)


def _dense_chart_rows() -> list[dict[str, object]]:
    periods = [f"25/{month:02d}" for month in range(1, 13)] + [f"26/{month:02d}" for month in range(1, 7)]
    bar_series = {
        "香港": [160 + index * 4 for index in range(18)],
        "中国内地": [150 + index * 6 for index in range(18)],
        "泰国": [90 + index * 3 for index in range(18)],
        "新加坡": [60 + index * 4 for index in range(18)],
        "其他市场": [80 + index * 5 for index in range(18)],
    }
    line_series = {
        "VONB Margin": [52 + index * 0.3 for index in range(18)],
        "13M Persistency": [86 + index * 0.2 for index in range(18)],
        "Digital STP": [50 + index * 1.2 for index in range(18)],
        "Agent Productivity": [60 + index * 0.8 for index in range(15)] + [75, 72, 69],
        "Protection Mix": [40 + index * 0.5 for index in range(15)] + [49, 46, 43],
    }
    rows: list[dict[str, object]] = []
    for family, values_by_name in (("barChart", bar_series), ("lineChart", line_series)):
        element_id = "bars" if family == "barChart" else "lines"
        chart_id = "chart_bars" if family == "barChart" else "chart_lines"
        for series_index, (name, values) in enumerate(values_by_name.items()):
            for point_index, (period, value) in enumerate(zip(periods, values, strict=True)):
                rows.append(
                    {
                        "id": f"{chart_id}_{series_index}_{point_index}",
                        "series_id": f"{chart_id}_{series_index}",
                        "series_name": name,
                        "category": period,
                        "y_value": float(value),
                        "point_order": point_index,
                        "chart_id": chart_id,
                        "chart_type": family,
                        "chart_title": "24-MONTH CONTROL TOWER",
                        "slide_title": "24-MONTH CONTROL TOWER",
                        "slide_summary": "Five markets and five quality indicators",
                        "axis_id": "left" if family == "barChart" else "right",
                        "unit": "US$m" if family == "barChart" else "% / Index",
                        "document_version_id": "dv",
                        "document_id": "doc",
                        "slide_id": "slide_4",
                        "slide_no": 4,
                        "element_id": element_id,
                        "bbox_json": "{}",
                        "chart_confidence": 1.0,
                        "series_confidence": 1.0,
                        "confidence": 1.0,
                        "source_kind": "embedded_workbook",
                        "document_title": "Deployment Test",
                    }
                )
    rows.append(
        {
            **rows[0],
            "id": "other_1",
            "series_id": "opat",
            "series_name": "OPAT",
            "slide_id": "slide_6",
            "slide_no": 6,
            "element_id": "other_chart",
            "chart_id": "other_chart",
            "chart_title": "Financial baseline",
        }
    )
    return rows


def test_broad_chart_analysis_builds_one_slide_local_evidence_bundle() -> None:
    question = "请解读五大市场产出与五项质量指标构成的高密度双轴监控图，分析并推理"
    result = ChartAnalyzer().analyze(
        question,
        _dense_chart_rows(),
        plan=deterministic_plan(question),
        preferred_element_ids={"bars", "lines"},
    )

    assert result is not None
    assert result.kind == "chart_analysis"
    assert len(result.evidence) == 1
    assert result.scope["slide_id"] == "slide_4"
    assert result.scope["family_series"]["bar"] == ["香港", "中国内地", "泰国", "新加坡", "其他市场"]
    assert result.scope["family_series"]["line"] == [
        "VONB Margin",
        "13M Persistency",
        "Digital STP",
        "Agent Productivity",
        "Protection Mix",
    ]
    assert "OPAT" in result.scope["excluded_document_series_names"]
    assert "Aggregate bar/column output" in result.text
    assert "Observed recent divergence" in result.text
    assert "Agent Productivity" in result.text and "Protection Mix" in result.text
    assert result.evidence[0]["chart_scope"] == result.scope


def test_broad_chart_analysis_does_not_activate_for_non_chart_business_question() -> None:
    question = "公司的整体优势是什么？"
    assert ChartAnalyzer().analyze(question, _dense_chart_rows(), plan=deterministic_plan(question)) is None


def test_chart_analysis_does_not_sum_heterogeneous_metrics_across_market_categories() -> None:
    rows = []
    for series_index, name in enumerate(("活跃代理k", "人均件数", "NPS")):
        for point_index, (market, value) in enumerate((("香港", 40), ("中国内地", 60), ("泰国", 50))):
            rows.append(
                {
                    **_dense_chart_rows()[0],
                    "id": f"heterogeneous_{series_index}_{point_index}",
                    "series_id": f"heterogeneous_{series_index}",
                    "series_name": name,
                    "category": market,
                    "y_value": value + series_index,
                    "point_order": point_index,
                    "slide_id": "market_matrix",
                    "slide_no": 9,
                    "element_id": "market_chart",
                    "chart_id": "market_chart",
                }
            )

    question = "请分析这张市场指标图说明了什么"
    result = ChartAnalyzer().analyze(
        question,
        rows,
        plan=deterministic_plan(question),
        preferred_element_ids={"market_chart"},
    )

    assert result is not None
    assert "Aggregate bar/column output" not in result.text
    assert not any(fact.get("operation") == "aggregate_series" for fact in result.facts)


def test_chart_analysis_clusters_overlaid_charts_but_excludes_unrelated_same_slide_chart() -> None:
    rows = _dense_chart_rows()
    for row in rows:
        if row["slide_id"] == "slide_4":
            row["bbox_json"] = '{"x": 10, "y": 20, "w": 800, "h": 400}'
    unrelated = []
    for point_index, period in enumerate(("25/01", "25/02", "25/03")):
        unrelated.append(
            {
                **rows[0],
                "id": f"unrelated_{point_index}",
                "series_id": "unrelated_revenue",
                "series_name": "Revenue",
                "category": period,
                "y_value": 100 + point_index,
                "point_order": point_index,
                "slide_id": "slide_4",
                "slide_no": 4,
                "element_id": "unrelated_chart",
                "chart_id": "unrelated_chart",
                "bbox_json": '{"x": 900, "y": 20, "w": 200, "h": 200}',
            }
        )

    question = "请解读五大市场和五项质量指标的双轴图"
    result = ChartAnalyzer().analyze(
        question,
        [*rows, *unrelated],
        plan=deterministic_plan(question),
        preferred_element_ids={"bars", "lines"},
    )

    assert result is not None
    assert "Revenue" not in result.scope["selected_series_names"]
    assert "Revenue" in result.scope["excluded_document_series_names"]


def _structural_scope_rows(
    *,
    slide_id: str,
    slide_no: int,
    family: str,
    series_count: int,
    point_count: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for series_index in range(series_count):
        for point_index in range(point_count):
            rows.append(
                {
                    **_dense_chart_rows()[0],
                    "id": f"{slide_id}_{series_index}_{point_index}",
                    "series_id": f"{slide_id}_{series_index}",
                    "series_name": f"Metric {series_index + 1}",
                    "category": f"25/{point_index + 1:02d}",
                    "y_value": 100 + series_index + point_index,
                    "point_order": point_index,
                    "slide_id": slide_id,
                    "slide_no": slide_no,
                    "element_id": f"{slide_id}_chart",
                    "chart_id": f"{slide_id}_chart",
                    "chart_type": family,
                    "slide_title": f"Structural chart {slide_no}",
                }
            )
    return rows


def test_chart_selection_uses_generic_cardinality_and_granularity_signature() -> None:
    rows = [
        *_structural_scope_rows(slide_id="six_by_twelve", slide_no=2, family="barChart", series_count=6, point_count=12),
        *_structural_scope_rows(slide_id="eight_by_twenty_four", slide_no=8, family="lineChart", series_count=8, point_count=24),
    ]
    question = "请分析8条折线、24个月的数据走势"

    result = ChartAnalyzer().analyze(question, rows, plan=deterministic_plan(question))

    assert result is not None
    assert result.scope["slide_id"] == "eight_by_twenty_four"
    assert result.scope["request_signature"]["series_counts"]
    assert result.scope["request_signature"]["point_counts"] == [24]
    assert result.scope["selection"]["series_count_matches"] >= 1
    assert result.scope["selection"]["point_count_matches"] == 8


def test_llm_visual_structure_overrides_ambiguous_surface_numbers() -> None:
    rows = [
        *_structural_scope_rows(slide_id="five_by_eight", slide_no=5, family="barChart", series_count=5, point_count=8),
        *_structural_scope_rows(slide_id="eight_by_five", slide_no=8, family="barChart", series_count=8, point_count=5),
    ]
    question = "比较5组指标在8种情景中的表现"
    plan = replace(
        deterministic_plan(question),
        needs_visuals=True,
        visual_structure={"series_group_counts": [5], "point_counts": [8], "chart_families": ["bar"]},
        planner="llm_semantic",
    )

    result = ChartAnalyzer().analyze(question, rows, plan=plan)

    assert result is not None
    assert result.scope["slide_id"] == "five_by_eight"


def test_chinese_query_expansion_and_slide_diversification() -> None:
    terms = query_terms("资本情景路径中哪一步的阶段值降幅最大？")
    assert "阶段值" in terms and "路径" in terms
    rows = [
        {"id": "a", "document_id": "d", "slide_id": "s1"},
        {"id": "b", "document_id": "d", "slide_id": "s1"},
        {"id": "c", "document_id": "d", "slide_id": "s2"},
    ]

    selected = EvidenceRetriever._select_diverse(rows, 2, 1)

    assert [row["id"] for row in selected] == ["a", "c"]
