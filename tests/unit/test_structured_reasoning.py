from __future__ import annotations

import json
import sqlite3

from packages.qbr_core.calculations import ChartCalculator
from packages.qbr_core.db import Database
from packages.qbr_core.query_planning import deterministic_plan
from packages.qbr_core.reasoning import TableReasoner
from packages.qbr_core.retrieval import EvidenceRetriever, query_terms


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
