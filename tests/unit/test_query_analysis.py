from __future__ import annotations

from dataclasses import replace

from packages.qbr_core.analysis.answering import DeterministicAnswerEngine
from packages.qbr_core.planning import QueryPlannerAgent, deterministic_plan
from packages.qbr_core.retrieval.coverage import build_evidence_contract, evaluate_evidence_coverage
from packages.qbr_core.retrieval.evidence import EvidencePackBuilder, infer_facet


def _row(
    row_id: str,
    content: str,
    *,
    slide_no: int = 1,
    chunk_type: str = "text",
    document_id: str = "doc",
    document_title: str = "QBR",
    retrieval_score: float = 2.0,
) -> dict[str, object]:
    return {
        "id": row_id,
        "document_version_id": "dv",
        "document_id": document_id,
        "document_title": document_title,
        "slide_id": f"slide_{slide_no}",
        "slide_no": slide_no,
        "chunk_type": chunk_type,
        "content": content,
        "retrieval_score": retrieval_score,
    }


def test_requirement_coverage_is_derived_from_semantics_not_intent_category() -> None:
    baseline = deterministic_plan("Compare revenue and margin")
    plan = replace(baseline, evidence_requirements=("revenue evidence", "margin evidence"))
    assert infer_facet("Revenue increased to 120", plan) == "revenue evidence"
    assert infer_facet("Margin reached 24%", plan) == "margin evidence"


def test_evidence_pack_reports_free_form_missing_requirements() -> None:
    plan = deterministic_plan("Compare revenue and margin")
    plan = replace(
        plan,
        evidence_requirements=("revenue evidence", "margin evidence"),
        execution_profile="deep",
    )
    pack = EvidencePackBuilder().build(plan, [_row("revenue", "Revenue increased to 120")])
    assert "revenue evidence" in pack.covered_facets
    assert "margin evidence" in pack.missing_facets


def test_typed_coverage_does_not_treat_fallback_wording_as_a_missing_requirement() -> None:
    plan = deterministic_plan("概括当前文档")

    pack = EvidencePackBuilder().build(plan, [_row("summary", "Revenue increased and margin remained resilient.")])

    assert pack.missing_facets == ("Use evidence that directly resolves the user's wording",)
    assert pack.coverage.has_gaps is False
    assert pack.coverage.supported_count == 1


def test_chart_analysis_bundle_satisfies_one_chart_level_contract() -> None:
    question = "这张24个月双轴控制图说明了什么？请指出增长结构、质量变化和潜在背离。"
    tool_evidence = [
        {
            "quote": "Chart analytical evidence bundle: aggregate output and recent divergence",
            "content_role": "chart",
            "extraction": "native_chart_analysis_bundle",
            "confidence": 1.0,
            "evidence_atom_id": "chart_bundle",
        }
    ]
    contract = build_evidence_contract(question, tool_evidence=tool_evidence)
    coverage = evaluate_evidence_coverage(
        contract,
        tool_evidence,
    )

    assert len(contract) == 1 and contract[0].kind == "chart_analysis"
    assert coverage.has_gaps is False
    assert coverage.supported_count == 1


def test_chart_calculation_bundle_satisfies_one_typed_contract_without_hiding_business_gaps() -> None:
    calculation = [
        {
            "quote": "Current: Alpha=15; Baseline: Alpha=10; relative change=50%",
            "content_role": "chart",
            "extraction": "native_chart_calculation",
            "confidence": 1.0,
            "evidence_atom_id": "calculation",
        }
    ]
    simple = build_evidence_contract("Alpha Current 比 Baseline 增加多少？", tool_evidence=calculation)
    compound = build_evidence_contract(
        "Alpha Current 比 Baseline 增加多少？为什么增长？",
        tool_evidence=calculation,
    )

    simple_coverage = evaluate_evidence_coverage(simple, calculation)

    assert len(simple) == 1 and simple[0].kind == "chart_calculation"
    assert simple_coverage.supported_count == 1 and not simple_coverage.has_gaps
    assert any(facet.kind == "driver_attribution" for facet in compound)


def test_deterministic_evidence_updates_document_coverage_even_without_retrieved_atoms() -> None:
    plan = deterministic_plan("请分析这张图", ["doc"])
    empty = EvidencePackBuilder().build(plan, [])

    updated = EvidencePackBuilder.with_additional_evidence(
        plan,
        empty,
        [
            {
                "document_id": "doc",
                "quote": "Chart analytical evidence bundle",
                "content_role": "chart",
                "extraction": "native_chart_analysis_bundle",
                "confidence": 1.0,
            }
        ],
    )

    assert updated.answerable
    assert updated.diagnostics["covered_document_ids"] == ["doc"]
    assert updated.diagnostics["missing_document_ids"] == []
    assert updated.diagnostics["deterministic_evidence_count"] == 1


def test_multi_part_growth_question_has_stable_typed_facets_and_specific_gap() -> None:
    question = "VONB、OPAT和新业务价值率分别同比增长多少？增长主要来自哪些业务板块？"
    plan = deterministic_plan(question)
    candidates = [
        _row(
            "financials",
            "核心财务指标 | 2024A | 2025A | 同比/变化\n"
            "VONB | 4,712 | 5,516 | +15% CER\n"
            "OPAT | 6,605 | 7,136 | +12%/股",
            chunk_type="table",
        ),
        _row(
            "margin",
            "VONB Margin | 24/12=53.4; 25/12=56.1; 26/03=56.0",
            slide_no=2,
            chunk_type="chart",
        ),
        _row(
            "market",
            "香港2025年VONB为2,256，增长28%；其他市场只有本期估算值。",
            slide_no=3,
        ),
    ]

    pack = EvidencePackBuilder().build(plan, candidates)
    by_id = {item.facet.facet_id: item.status for item in pack.coverage.facets}

    assert set(by_id) == {
        "metric_comparison:vonb",
        "metric_comparison:opat",
        "metric_comparison:vonb_margin",
        "driver_attribution:business_segment",
    }
    assert by_id["metric_comparison:vonb"] == "supported"
    assert by_id["metric_comparison:opat"] == "supported"
    assert by_id["metric_comparison:vonb_margin"] == "supported"
    assert by_id["driver_attribution:business_segment"] == "partial"
    assert pack.coverage.supported_count == 3
    assert pack.coverage.gap_labels == ("增长主要来自哪些业务板块",)


def test_typed_facets_are_stable_across_equivalent_planner_wording_and_question_paraphrase() -> None:
    question = "VONB、OPAT和新业务价值率分别同比增长多少？增长主要来自哪些业务板块？"
    first = build_evidence_contract(question)
    second = build_evidence_contract("VONB、OPAT与新业务价值率同比变化多少？哪些业务板块贡献增长？")

    assert {facet.facet_id for facet in first} == {facet.facet_id for facet in second}

    evidence = [
        _row("growth", "VONB | 2024=4,712 | 2025=5,516 | +15% CER", chunk_type="table"),
        _row("driver", "增长主要来自香港市场，香港VONB同比增长28%。", slide_no=2),
    ]
    first_plan = replace(deterministic_plan(question), evidence_requirements=("metric growth and segment drivers",))
    second_plan = replace(deterministic_plan(question), evidence_requirements=("同比数值", "业务板块增长来源"))
    first_statuses = {item.facet.facet_id: item.status for item in EvidencePackBuilder().build(first_plan, evidence).coverage.facets}
    second_statuses = {item.facet.facet_id: item.status for item in EvidencePackBuilder().build(second_plan, evidence).coverage.facets}
    assert first_statuses == second_statuses


def test_attribution_requires_direct_driver_evidence_not_just_current_segment_size() -> None:
    question = "VONB同比增长多少？增长主要来自哪些业务板块？"
    plan = deterministic_plan(question)
    pack = EvidencePackBuilder().build(
        plan,
        [
            _row("growth", "VONB | 2024=4,712 | 2025=5,516 | +15% CER", chunk_type="table"),
            _row("size", "市场 | VONB\n香港 | 2,256\n中国内地 | 1,180", slide_no=2, chunk_type="table"),
        ],
    )

    driver = next(item for item in pack.coverage.facets if item.facet.kind == "driver_attribution")
    assert driver.status == "partial"

    supported = EvidencePackBuilder().build(
        plan,
        [
            _row("growth", "VONB | 2024=4,712 | 2025=5,516 | +15% CER", chunk_type="table"),
            _row("driver", "增长主要来自香港市场，香港VONB同比增长28%。", slide_no=2),
        ],
    )
    driver = next(item for item in supported.coverage.facets if item.facet.kind == "driver_attribution")
    assert driver.status == "supported"


def test_source_gap_message_is_user_friendly_in_chinese_and_english() -> None:
    builder = EvidencePackBuilder()
    chinese_plan = deterministic_plan("VONB同比增长多少？增长主要来自哪些业务板块？")
    chinese_pack = builder.build(
        chinese_plan,
        [_row("growth_zh", "VONB | 2024=4,712 | 2025=5,516 | +15% CER", chunk_type="table")],
    )
    chinese_answer = DeterministicAnswerEngine._render_safe_fallback(
        chinese_plan,
        chinese_pack.evidence,
        None,
        chinese_pack,
    )

    english_plan = deterministic_plan("How much did VONB grow? Which business segments drove growth?")
    english_pack = builder.build(
        english_plan,
        [_row("growth_en", "VONB | 2024=4,712 | 2025=5,516 | +15% CER", chunk_type="table")],
    )
    english_answer = DeterministicAnswerEngine._render_safe_fallback(
        english_plan,
        english_pack.evidence,
        None,
        english_pack,
    )

    assert "当前资料暂未支持以下内容" in chinese_answer
    assert "证据覆盖" not in chinese_answer
    assert "The current sources do not yet support" in english_answer
    assert "Evidence coverage" not in english_answer


def test_task_frame_can_request_provenance_without_special_source_intent() -> None:
    plan = deterministic_plan("哪些结论来自公开披露？")
    pack = EvidencePackBuilder().build(
        plan,
        [_row("source", "Official disclosure source: FY25 results", chunk_type="notes")],
    )
    assert pack.atoms
    assert pack.atoms[0].content_role == "provenance"


def test_model_free_planner_is_explicitly_degraded() -> None:
    plan = QueryPlannerAgent().plan("请概括这份文档")
    assert plan.planner == "linguistic_fallback"
    assert plan.planner_confidence < 0.5
    assert not hasattr(plan, "intent")


def test_multi_document_pack_preserves_each_scoped_document_before_global_ranking() -> None:
    baseline = deterministic_plan("综合三份报告提出管理行动", ["doc_1", "doc_2", "doc_3"])
    plan = replace(baseline, execution_profile="deep")
    candidates = [
        _row("d1-high", "渠道效率明显提升，需要继续优化。", document_id="doc_1", retrieval_score=10),
        _row("d1-next", "客户增长保持稳定。", document_id="doc_1", slide_no=2, retrieval_score=9),
        _row("d2", "资本缓冲高于管理阈值。", document_id="doc_2", retrieval_score=3),
        _row("d3", "市场风险资本占用最高。", document_id="doc_3", retrieval_score=2),
    ]

    pack = EvidencePackBuilder().build(plan, candidates, max_atoms=3)

    assert {atom.source["document_id"] for atom in pack.atoms} == {"doc_1", "doc_2", "doc_3"}
    assert pack.diagnostics["missing_document_ids"] == []


def test_named_document_requirement_cannot_be_covered_by_another_deck() -> None:
    baseline = deterministic_plan("综合QBR02和QBR03", ["doc_2", "doc_3"])
    plan = replace(
        baseline,
        evidence_requirements=("QBR02中的渠道数据", "QBR03中的资本数据"),
    )

    assert (
        infer_facet("资本比率为221%", plan, document_title="AIA_QBR_03_Capital_Resilience")
        == "QBR03中的资本数据"
    )
