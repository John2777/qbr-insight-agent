from __future__ import annotations

import json
from pathlib import Path

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.answering import DeterministicAnswerEngine
from packages.qbr_core.terminology import QBR_TERMS, find_term, glossary_by_term

ROOT = Path(__file__).parents[2]


def test_definition_intent_prefers_the_longest_professional_term() -> None:
    assert find_term("VONB是什么含义，解释一下").term == "VONB"
    assert find_term("VONB Margin 和 VONB 有什么区别？").term == "VONB Margin"
    assert find_term("2025年VONB是多少？") is None
    assert find_term("下一季度四项优先事项及Owner是什么？") is None


def test_definition_answer_uses_one_compact_document_source_without_numeric_noise() -> None:
    engine = object.__new__(DeterministicAnswerEngine)
    common = {
        "document_version_id": "dv_1",
        "slide_id": "slide_1",
        "element_id": "element_1",
        "bbox_json": "{}",
        "document_title": "Growth QBR",
        "slide_no": 2,
    }
    chunks = [
        {**common, "id": "definition", "chunk_type": "text", "content": "VONB 新业务价值"},
        {
            **common,
            "id": "table",
            "slide_id": "slide_4",
            "slide_no": 4,
            "chunk_type": "table",
            "content": "市场 | VONB US$m\n香港 | 2,256\n中国内地 | 1,180",
        },
    ]

    result = engine._term_definition_answer("VONB是什么含义，解释一下", chunks)

    assert result is not None
    answer, evidence, warnings = result
    assert all(text in answer for text in ("Value of New Business", "新业务价值", "未来价值"))
    assert all(text not in answer for text in ("香港", "2,256", "中国内地"))
    assert [item["chunk_id"] for item in evidence] == ["definition"]
    assert warnings == []


def test_term_definition_run_persists_presentation_metadata(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects", run_inline_worker=False))
    conversation = service.create_conversation("ws_demo", "user_demo")
    queued = service.ask(conversation["id"], "VONB是什么意思？", "ws_demo", "user_demo")

    assert service.process_next_run("terminology-test") == queued["run_id"]
    run = service.get_run(queued["run_id"], "ws_demo")
    history = service.get_conversation(conversation["id"], "ws_demo", "user_demo")

    assert "Value of New Business" in run["message"]["content"]
    assert run["citations"] == []
    assert run["message"]["metadata"]["answer_mode"] == "term_definition"
    assert run["message"]["metadata"]["show_visuals"] is False
    assert run["message"]["metadata"]["knowledge_source"] == "curated_glossary"
    assert run["message"]["metadata"]["pipeline_version"] == "planned-evidence-v2"
    assert run["message"]["metadata"]["query_plan"]["intent"] == "term_definition"
    assert history["messages"][-1]["metadata"]["show_visuals"] is False
    assert run["model"]["status"] == "skipped_curated_glossary"


def test_qbr_term_benchmark_covers_every_curated_term() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_terms/cases.json").read_text(encoding="utf-8"))
    benchmark_terms = {str(case["term"]).casefold() for case in dataset["cases"]}

    assert dataset["dataset"] == "qbr-terms"
    assert len(dataset["cases"]) >= 50
    assert benchmark_terms == set(glossary_by_term())
    assert len(QBR_TERMS) == len(benchmark_terms)


def test_term_intent_does_not_capture_existing_numeric_and_action_benchmark_questions() -> None:
    dataset = json.loads((ROOT / "benchmarks/qbr_50/cases.json").read_text(encoding="utf-8"))

    assert all(find_term(case["question"]) is None for case in dataset["cases"])
