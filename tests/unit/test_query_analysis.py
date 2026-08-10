from __future__ import annotations

from dataclasses import replace

from packages.qbr_core.evidence import EvidencePackBuilder, infer_facet
from packages.qbr_core.query_planning import QueryPlannerAgent, deterministic_plan


def _row(row_id: str, content: str, *, slide_no: int = 1, chunk_type: str = "text") -> dict[str, object]:
    return {
        "id": row_id,
        "document_version_id": "dv",
        "document_id": "doc",
        "slide_id": f"slide_{slide_no}",
        "slide_no": slide_no,
        "chunk_type": chunk_type,
        "content": content,
        "retrieval_score": 2.0,
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
