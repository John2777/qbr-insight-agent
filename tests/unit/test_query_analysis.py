from __future__ import annotations

from dataclasses import replace

from packages.qbr_core.evidence import EvidencePackBuilder, infer_facet
from packages.qbr_core.query_planning import QueryPlannerAgent, deterministic_plan


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
