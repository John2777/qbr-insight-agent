from __future__ import annotations

import json
from pathlib import Path

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.query_planning import deterministic_plan
from packages.qbr_core.terminology import QBR_TERMS, find_term, glossary_by_term

ROOT = Path(__file__).parents[2]


def test_language_rule_prefers_the_longest_professional_term() -> None:
    assert find_term("VONB是什么含义，解释一下").term == "VONB"
    assert find_term("VONB Margin 和 VONB 有什么区别？").term == "VONB Margin"
    assert find_term("2025年VONB是多少？") is None
    assert find_term("下一季度四项优先事项及Owner是什么？") is None


def test_terminology_rule_adds_retrieval_vocabulary_without_answering() -> None:
    plan = deterministic_plan("VONB是什么含义，解释一下")
    corpus = " ".join(item.text for item in plan.retrieval_queries)
    assert "VONB" in corpus and "新业务价值" in corpus
    assert "香港" not in corpus and "2,256" not in corpus
    assert not hasattr(plan, "intent")


def test_term_question_without_documents_uses_evidence_boundary_not_curated_answer_template(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    conversation = service.create_conversation("ws_demo", "user_demo")
    queued = service.ask(conversation["id"], "VONB是什么意思？", "ws_demo", "user_demo")

    assert service.process_next_run("terminology-test") == queued["run_id"]
    run = service.get_run(queued["run_id"], "ws_demo")
    history = service.get_conversation(conversation["id"], "ws_demo", "user_demo")

    assert "没有检索到" in run["message"]["content"]
    assert run["citations"] == []
    assert run["message"]["metadata"]["knowledge_source"] == "document_evidence"
    assert run["message"]["metadata"]["pipeline_version"] == "semantic-task-frame-v1"
    assert "intent" not in run["message"]["metadata"]["query_plan"]
    assert history["messages"][-1]["metadata"]["pipeline_version"] == "semantic-task-frame-v1"
    assert run["model"]["status"] == "disabled"


def test_qbr_term_benchmark_covers_every_curated_term() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_terms/cases.json").read_text(encoding="utf-8"))
    benchmark_terms = {str(case["term"]).casefold() for case in dataset["cases"]}

    assert dataset["dataset"] == "qbr-terms"
    assert len(dataset["cases"]) >= 50
    assert benchmark_terms == set(glossary_by_term())
    assert len(QBR_TERMS) == len(benchmark_terms)


def test_terminology_hint_does_not_capture_existing_numeric_and_action_benchmark_questions() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_50/cases.json").read_text(encoding="utf-8"))

    assert all(find_term(case["question"]) is None for case in dataset["cases"])
