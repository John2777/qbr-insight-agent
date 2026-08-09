from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.qbr_core import QBRService, Settings

WORKSPACE_ID = "ws_demo"
USER_ID = "user_demo"
DIMENSION_WEIGHTS = {
    "content_coverage": 0.35,
    "numeric_accuracy": 0.30,
    "citation_recall": 0.12,
    "citation_precision": 0.08,
    "retrieval_hit": 0.10,
    "groundedness": 0.05,
    "relevance": 0.08,
    "answer_mode": 0.05,
    "citation_economy": 0.02,
}
ABSTENTION_TERMS = ("没有足够证据", "证据不足", "未提供", "无法回答", "不能回答", "不可回答")


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in text if character not in " \t\r\n,，。；;：:()（）[]【】`*_")


def group_coverage(answer: str, groups: list[list[str]]) -> float | None:
    if not groups:
        return None
    folded = normalize(answer)
    hits = sum(any(normalize(alias) in folded for alias in group) for group in groups)
    return hits / len(groups)


def forbidden_group_absence(answer: str, groups: list[list[str]]) -> float | None:
    if not groups:
        return None
    folded = normalize(answer)
    violations = sum(any(normalize(alias) in folded for alias in group) for group in groups)
    return 1.0 - violations / len(groups)


def ref_matches(actual: dict[str, Any], expected: dict[str, Any], document_key_by_id: dict[str, str]) -> bool:
    return document_key_by_id.get(str(actual.get("document_id"))) == expected["deck"] and int(actual.get("slide_no") or 0) == int(
        expected["slide"]
    )


def evidence_recall(
    actual: list[dict[str, Any]],
    groups: list[list[dict[str, Any]]],
    document_key_by_id: dict[str, str],
) -> float | None:
    if not groups:
        return None
    hits = 0
    for group in groups:
        if any(ref_matches(item, expected, document_key_by_id) for item in actual for expected in group):
            hits += 1
    return hits / len(groups)


def citation_precision(
    citations: list[dict[str, Any]],
    allowed: list[dict[str, Any]],
    document_key_by_id: dict[str, str],
    expected_abstention: bool,
    policy: str = "required",
) -> float:
    if expected_abstention:
        return 1.0 if not citations else 0.0
    if policy == "forbidden":
        return 1.0 if not citations else 0.0
    if policy == "optional" and not citations:
        return 1.0
    if not citations:
        return 0.0
    return sum(any(ref_matches(citation, expected, document_key_by_id) for expected in allowed) for citation in citations) / len(citations)


def weighted_score(dimensions: dict[str, float | None]) -> float:
    applicable = [(DIMENSION_WEIGHTS[name], score) for name, score in dimensions.items() if score is not None]
    denominator = sum(weight for weight, _ in applicable)
    return 100 * sum(weight * float(score) for weight, score in applicable) / denominator if denominator else 0.0


def create_service(
    runtime: Path,
    *,
    retrieval_strategy: str = "fts",
    embedding_provider: str = "hashing",
    embedding_model: str = "hashing-v1",
    embedding_dimensions: int = 384,
) -> QBRService:
    return QBRService(
        Settings(
            data_dir=runtime,
            database_path=runtime / "app.sqlite3",
            object_dir=runtime / "objects",
            run_inline_worker=False,
            llm_enabled=False,
            retrieval_strategy=retrieval_strategy,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            vector_index_dir=runtime / "vector_indexes",
        )
    )


def ingest_decks(service: QBRService, project_root: Path, dataset: dict[str, Any]) -> dict[str, str]:
    document_ids: dict[str, str] = {}
    upload_dir = service.settings.data_dir / "benchmark_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    for key, relative_path in dataset["source_decks"].items():
        path = project_root / relative_path
        if not path.exists():
            raise FileNotFoundError(path)
        upload_path = upload_dir / path.name
        shutil.copy2(path, upload_path)
        result = service.import_document(
            upload_path,
            filename=path.name,
            title=path.stem,
            metadata={"benchmark": dataset["dataset"], "deck_key": key},
            deduplication="reject",
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
        )
        processed = service.process_next_job("qbr-benchmark-ingest")
        if processed != result["job"]["id"]:
            raise RuntimeError(f"Unexpected ingestion job order for {key}")
        job = service.get_job(result["job"]["id"], WORKSPACE_ID)
        if job["status"] not in {"ready", "partial"}:
            raise RuntimeError(f"Ingestion failed for {key}: {job}")
        document_ids[key] = result["document"]["id"]
    return document_ids


def evaluate_case(
    service: QBRService,
    case: dict[str, Any],
    document_ids: dict[str, str],
) -> dict[str, Any]:
    scope_ids = [document_ids[key] for key in case["scope"]]
    reverse_ids = {value: key for key, value in document_ids.items()}
    retrieval = service.retriever.search(case["question"], WORKSPACE_ID, scope_ids, top_k=5)
    retrieval_items = [
        {
            "rank": index,
            "deck": reverse_ids.get(str(item.get("document_id"))),
            "slide": item.get("slide_no"),
            "chunk_type": item.get("chunk_type"),
            "score": item.get("retrieval_score"),
            "content": str(item.get("content", ""))[:500],
        }
        for index, item in enumerate(retrieval.items, 1)
    ]
    retrieval_refs = [{"document_id": item.get("document_id"), "slide_no": item.get("slide_no")} for item in retrieval.items]

    conversation = service.create_conversation(
        WORKSPACE_ID,
        USER_ID,
        document_ids=scope_ids,
        title=f"Benchmark {case['id']}",
    )
    started = time.perf_counter()
    queued = service.ask(conversation["id"], case["question"], WORKSPACE_ID, USER_ID)
    processed_run = service.process_next_run("qbr-benchmark-answer")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if processed_run != queued["run_id"]:
        raise RuntimeError(f"Unexpected answer run order for {case['id']}")
    run = service.get_run(queued["run_id"], WORKSPACE_ID)
    answer = str(run.get("message", {}).get("content", ""))
    citations = run.get("citations", [])
    expected_abstention = bool(case.get("expected_abstention"))
    abstained = any(term in answer for term in ABSTENTION_TERMS) or "INSUFFICIENT_EVIDENCE" in run.get("warnings", [])

    citation_recall_score = evidence_recall(citations, case.get("evidence_groups", []), reverse_ids)
    citation_policy = str(case.get("citation_policy") or "required")
    citation_precision_score = citation_precision(
        citations,
        case.get("allowed_evidence", []),
        reverse_ids,
        expected_abstention,
        citation_policy,
    )
    message = dict(run.get("message") or {})
    message_metadata = dict(message.get("metadata") or {})
    expected_answer_mode = case.get("expected_answer_mode")
    answer_mode_score = float(message_metadata.get("answer_mode") == expected_answer_mode) if expected_answer_mode else None
    max_citations = case.get("max_citations")
    citation_economy = float(len(citations) <= int(max_citations)) if max_citations is not None else None
    curated_grounding = (
        message_metadata.get("knowledge_source") in {"curated_glossary", "curated_glossary+document"}
        and expected_answer_mode == "term_definition"
    )
    groundedness = (
        1.0
        if expected_abstention and abstained and not citations
        else 0.0
        if expected_abstention
        else 1.0
        if curated_grounding
        else ((citation_recall_score or 0.0) + citation_precision_score) / 2
    )
    dimensions: dict[str, float | None] = {
        "content_coverage": group_coverage(answer, case.get("fact_groups", [])),
        "numeric_accuracy": group_coverage(answer, case.get("numeric_groups", [])),
        "citation_recall": citation_recall_score,
        "citation_precision": citation_precision_score,
        "retrieval_hit": evidence_recall(retrieval_refs, case.get("evidence_groups", []), reverse_ids),
        "groundedness": groundedness,
        "relevance": forbidden_group_absence(answer, case.get("forbidden_groups", [])),
        "answer_mode": answer_mode_score,
        "citation_economy": citation_economy,
    }
    public_citations = [
        {
            "label": citation.get("label"),
            "deck": reverse_ids.get(str(citation.get("document_id"))),
            "document_title": citation.get("document_title"),
            "slide": citation.get("slide_no"),
            "element_type": citation.get("element_type"),
            "source_kind": citation.get("source_kind"),
            "confidence": citation.get("confidence"),
            "quote": citation.get("quote"),
        }
        for citation in citations
    ]
    return {
        "id": case["id"],
        "category": case["category"],
        "scope": case["scope"],
        "question": case["question"],
        "standard_answer": case["standard_answer"],
        "system_answer": answer,
        "warnings": run.get("warnings", []),
        "model": run.get("model", {}),
        "message_metadata": message_metadata,
        "latency_ms": elapsed_ms,
        "retrieval_strategy": retrieval.strategy,
        "retrieval_query": retrieval.query,
        "retrieval_diagnostics": retrieval.diagnostics,
        "retrieval": retrieval_items,
        "citations": public_citations,
        "expected_abstention": expected_abstention,
        "abstained": abstained,
        "dimensions": {name: None if score is None else round(score * 100, 2) for name, score in dimensions.items()},
        "score": round(weighted_score(dimensions), 2),
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    dimension_values: dict[str, list[float]] = defaultdict(list)
    category_values: dict[str, list[float]] = defaultdict(list)
    for result in results:
        category_values[result["category"]].append(result["score"])
        for name, score in result["dimensions"].items():
            if score is not None:
                dimension_values[name].append(score)
    scores = [result["score"] for result in results]
    dimension_scores = {name: round(statistics.fmean(values), 2) for name, values in sorted(dimension_values.items())}
    category_scores = {name: round(statistics.fmean(values), 2) for name, values in sorted(category_values.items())}
    retrieval_to_citation_gap = (
        round(dimension_scores["retrieval_hit"] - dimension_scores["citation_recall"], 2)
        if "retrieval_hit" in dimension_scores and "citation_recall" in dimension_scores
        else None
    )
    retrieval_to_content_gap = (
        round(dimension_scores["retrieval_hit"] - dimension_scores["content_coverage"], 2)
        if "retrieval_hit" in dimension_scores and "content_coverage" in dimension_scores
        else None
    )
    return {
        "case_count": len(results),
        "overall_score": round(statistics.fmean(scores), 2) if scores else 0.0,
        "median_score": round(statistics.median(scores), 2) if scores else 0.0,
        "pass_at_80": round(sum(score >= 80 for score in scores) / len(scores) * 100, 2) if scores else 0.0,
        "perfect_cases": sum(score == 100 for score in scores),
        "dimension_scores": dimension_scores,
        "category_scores": category_scores,
        "diagnostics": {
            "retrieval_to_citation_gap": retrieval_to_citation_gap,
            "retrieval_to_content_gap": retrieval_to_content_gap,
            "strongest_category": max(category_scores, key=category_scores.get) if category_scores else None,
            "weakest_category": min(category_scores, key=category_scores.get) if category_scores else None,
            "weakest_dimension": min(dimension_scores, key=dimension_scores.get) if dimension_scores else None,
        },
        "latency_ms": {
            "mean": round(statistics.fmean(result["latency_ms"] for result in results), 2) if results else 0.0,
            "p95": round(sorted(result["latency_ms"] for result in results)[max(0, int(len(results) * 0.95) - 1)], 2) if results else 0.0,
        },
    }


def markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        f"# {payload['dataset'].upper()} Benchmark Report",
        "",
        f"- Dataset: `{payload['dataset']}@{payload['version']}`",
        f"- Generated: `{payload['generated_at']}`",
        f"- Answer mode: `{payload['answer_mode']}`",
        f"- Retrieval strategy: `{payload['retrieval_strategy']}`",
        f"- Overall score: **{summary['overall_score']:.2f}/100**",
        f"- Median score: **{summary['median_score']:.2f}/100**",
        f"- Cases ≥80: **{summary['pass_at_80']:.2f}%**",
        f"- Perfect cases: **{summary['perfect_cases']}/{summary['case_count']}**",
        f"- Mean / p95 latency: **{summary['latency_ms']['mean']:.2f} / {summary['latency_ms']['p95']:.2f} ms**",
        "",
        "## Dimension scores",
        "",
        "| Dimension | Score |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {score:.2f} |" for name, score in summary["dimension_scores"].items())
    lines.extend(["", "## Category scores", "", "| Category | Score |", "|---|---:|"])
    lines.extend(f"| {name} | {score:.2f} |" for name, score in summary["category_scores"].items())
    diagnostics = summary["diagnostics"]
    citation_gap = diagnostics["retrieval_to_citation_gap"]
    content_gap = diagnostics["retrieval_to_content_gap"]
    citation_gap_text = "n/a" if citation_gap is None else f"{citation_gap:.2f} points"
    content_gap_text = "n/a" if content_gap is None else f"{content_gap:.2f} points"
    lines.extend(
        [
            "",
            "## Automated interpretation",
            "",
            f"- Strongest category: **{diagnostics['strongest_category']}**; weakest category: **{diagnostics['weakest_category']}**.",
            f"- Weakest quality dimension: **{diagnostics['weakest_dimension']}**.",
            f"- Retrieval-to-citation gap: **{citation_gap_text}**.",
            f"- Retrieval-to-content gap: **{content_gap_text}**.",
            "- A large positive gap means relevant evidence is often retrieved but is not selected, cited, "
            "or transformed into the requested answer.",
        ]
    )
    lines.extend(["", "## Case results", "", "| ID | Category | Score | Key weakness |", "|---|---|---:|---|"])
    for result in payload["results"]:
        weak = [name for name, score in result["dimensions"].items() if score is not None and score < 100]
        lines.append(f"| {result['id']} | {result['category']} | {result['score']:.2f} | {', '.join(weak) or '—'} |")
    lines.extend(["", "## Lowest-scoring cases", ""])
    for result in sorted(payload["results"], key=lambda item: (item["score"], item["id"]))[:10]:
        lines.extend(
            [
                f"### {result['id']} · {result['score']:.2f}/100",
                "",
                f"**Question:** {result['question']}",
                "",
                f"**Standard:** {result['standard_answer']}",
                "",
                f"**System:** {result['system_answer']}",
                "",
                f"**Dimensions:** `{json.dumps(result['dimensions'], ensure_ascii=False)}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the QBR end-to-end benchmark")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/qbr_50/cases.json"))
    parser.add_argument("--output-json", type=Path, default=Path("benchmark_results/qbr_50_latest.json"))
    parser.add_argument("--output-md", type=Path, default=Path("benchmark_results/qbr_50_latest.md"))
    parser.add_argument("--min-score", type=float, default=0.0, help="Exit non-zero when the overall score is below this value")
    parser.add_argument(
        "--retrieval-strategy",
        choices=("fts", "vector", "hybrid"),
        default="fts",
        help="Retrieval ablation mode; vector/hybrid require the optional vector dependencies",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("hashing", "openai-compatible"),
        default="hashing",
        help="Use hashing for offline plumbing tests or an OpenAI-compatible semantic embedding endpoint",
    )
    parser.add_argument("--embedding-model", default="hashing-v1")
    parser.add_argument("--embedding-dimensions", type=int, default=384)
    arguments = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    dataset = json.loads((project_root / arguments.cases).read_text(encoding="utf-8"))
    defaults = dict(dataset.get("case_defaults") or {})
    dataset["cases"] = [{**defaults, **case} for case in dataset["cases"]]
    with tempfile.TemporaryDirectory(prefix="qbr-30-") as directory:
        service = create_service(
            Path(directory),
            retrieval_strategy=arguments.retrieval_strategy,
            embedding_provider=arguments.embedding_provider,
            embedding_model=arguments.embedding_model,
            embedding_dimensions=arguments.embedding_dimensions,
        )
        document_ids = ingest_decks(service, project_root, dataset)
        results = [evaluate_case(service, case, document_ids) for case in dataset["cases"]]

    payload = {
        "dataset": dataset["dataset"],
        "version": dataset["version"],
        "generated_at": datetime.now(UTC).isoformat(),
        "answer_mode": "deterministic_local_no_llm",
        "retrieval_strategy": arguments.retrieval_strategy,
        "weights": DIMENSION_WEIGHTS,
        "summary": aggregate(results),
        "results": results,
    }
    output_json = project_root / arguments.output_json
    output_md = project_root / arguments.output_md
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if payload["summary"]["overall_score"] < arguments.min_score:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
