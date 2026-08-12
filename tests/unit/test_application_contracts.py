"""Tests for strongly typed answer-run boundary contracts."""

from __future__ import annotations

import pytest

from packages.qbr_core.application.contracts import Citation, Evidence, RunMetadata, RunResult


def test_run_result_validates_evidence_and_deduplicates_warnings() -> None:
    metadata = RunMetadata(
        show_visuals=False,
        query_plan={},
        answer_routing={},
        retrieval={},
        evidence_pack={},
        verification={},
        conversation_context={},
    )
    result = RunResult.create(
        answer="Grounded answer",
        evidence=[
            {
                "document_version_id": "dv_1",
                "slide_id": "slide_1",
                "quote": "Revenue grew",
                "confidence": 1.4,
                "source_kind": "native_ooxml",
                "document_title": "QBR",
            }
        ],
        warnings=["PARTIAL_EVIDENCE_COVERAGE", "PARTIAL_EVIDENCE_COVERAGE"],
        model={"status": "disabled"},
        metadata=metadata,
    )

    assert result.evidence[0].confidence == 1.0
    assert result.evidence[0].to_dict()["document_title"] == "QBR"
    assert result.warnings == ("PARTIAL_EVIDENCE_COVERAGE",)


def test_evidence_rejects_missing_persistence_identity() -> None:
    with pytest.raises(ValueError, match="document_version_id"):
        Evidence.from_mapping({"slide_id": "slide_1", "quote": "value"})


def test_citation_round_trips_public_attributes() -> None:
    citation = Citation.from_mapping(
        {
            "id": "cit_1",
            "claim_no": 2,
            "slide_id": "slide_1",
            "quote": "A fact",
            "source_kind": "table",
            "label": "[2]",
            "preview_url": "/api/v1/slides/slide_1/preview",
            "document_title": "QBR",
        }
    )

    assert citation.to_dict()["document_title"] == "QBR"
    assert citation.to_dict()["label"] == "[2]"
