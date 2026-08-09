from __future__ import annotations

import json
from pathlib import Path

from scripts.evaluate_qbr_benchmark import (
    citation_precision,
    evidence_recall,
    forbidden_group_absence,
    group_coverage,
    normalize,
)

ROOT = Path(__file__).resolve().parent.parent


def test_dataset_has_50_well_formed_cases() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_50/cases.json").read_text(encoding="utf-8"))
    cases = dataset["cases"]
    assert dataset["dataset"] == "qbr-50"
    assert len(cases) == 50
    assert len({case["id"] for case in cases}) == 50
    assert all(case["question"] and case["standard_answer"] for case in cases)
    assert all(case["scope"] and set(case["scope"]) <= set(dataset["source_decks"]) for case in cases)
    assert all(case["fact_groups"] for case in cases)
    assert sum(case["category"] == "abstention" for case in cases) >= 2


def test_normalized_alias_coverage_handles_full_width_and_commas() -> None:
    answer = "2025年 VONB 为 US$5,516m，增长１５%。"
    assert normalize("１５％") == normalize("15%")
    assert group_coverage(answer, [["5516"], ["15%"]]) == 1.0


def test_evidence_metrics_use_deck_and_slide() -> None:
    reverse_ids = {"doc_growth": "growth"}
    actual = [{"document_id": "doc_growth", "slide_no": 4}]
    groups = [[{"deck": "growth", "slide": 4}]]
    assert evidence_recall(actual, groups, reverse_ids) == 1.0
    assert citation_precision(actual, [{"deck": "growth", "slide": 4}], reverse_ids, False) == 1.0
    assert citation_precision(actual, [{"deck": "growth", "slide": 5}], reverse_ids, False) == 0.0


def test_abstention_requires_no_citation_for_full_precision() -> None:
    reverse_ids = {"doc_growth": "growth"}
    assert citation_precision([], [], reverse_ids, True) == 1.0
    assert citation_precision([{"document_id": "doc_growth", "slide_no": 2}], [], reverse_ids, True) == 0.0


def test_optional_glossary_citations_and_irrelevant_content_are_scored() -> None:
    assert citation_precision([], [], {}, False, "optional") == 1.0
    assert citation_precision([{"document_id": "doc_growth", "slide_no": 2}], [], {}, False, "forbidden") == 0.0
    assert forbidden_group_absence("VONB 是新业务价值。", [["香港"], ["2,256"]]) == 1.0
    assert forbidden_group_absence("香港 VONB 为 2,256。", [["香港"], ["2,256"]]) == 0.0
