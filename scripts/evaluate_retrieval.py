from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from packages.qbr_core import QBRService, Settings


def evaluate(service: QBRService, cases: list[dict[str, Any]], workspace_id: str) -> dict[str, Any]:
    hits = 0
    details = []
    for case in cases:
        result = service.retriever.search(str(case["question"]), workspace_id, [], top_k=5)
        corpus = "\n".join(str(item.get("content", "")) for item in result.items).casefold()
        expected = [str(term) for term in case.get("expected_terms", [])]
        hit = all(term.casefold() in corpus for term in expected)
        hits += int(hit)
        details.append({"question": case["question"], "hit": hit, "strategy": result.strategy})
    total = len(cases)
    return {"cases": total, "hits": hits, "hit_at_5": round(hits / total, 4) if total else 0, "details": details}


def synthetic_service(root: Path, retrieval_strategy: str = "fts") -> QBRService:
    script = Path("skills/extract-ppt-chart-data/scripts/self_test.py").resolve()
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("qbr_eval_fixture", script)
    if not spec or not spec.loader:
        raise RuntimeError("Unable to load the synthetic PPTX fixture")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    presentation = root / "golden-retrieval.pptx"
    module.make_pptx(presentation)
    service = QBRService(
        Settings(
            root,
            root / "app.sqlite3",
            root / "objects",
            run_inline_worker=False,
            retrieval_strategy=retrieval_strategy,
            embedding_provider="hashing",
            embedding_model="hashing-v1",
            embedding_dimensions=384,
            vector_index_dir=root / "vector_indexes",
        )
    )
    uploaded = service.import_document(
        presentation,
        filename=presentation.name,
        title="Golden retrieval fixture",
        metadata={"dataset": "retrieval-v1"},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )
    service.process_next_job("eval-worker")
    if service.get_job(uploaded["job"]["id"], "ws_demo")["status"] not in {"ready", "partial"}:
        raise RuntimeError("Synthetic retrieval fixture failed to ingest")
    return service


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate QBR retrieval against a small versioned golden set")
    parser.add_argument("--database", type=Path, help="Evaluate an existing database instead of the synthetic fixture")
    parser.add_argument("--objects", type=Path, default=Path("data/objects"))
    parser.add_argument("--cases", type=Path, default=Path("tests/golden/retrieval_cases.json"))
    parser.add_argument("--workspace", default="ws_demo")
    parser.add_argument("--min-hit-at-5", type=float, default=1.0)
    parser.add_argument("--retrieval-strategy", choices=("fts", "vector", "hybrid"), default="fts")
    arguments = parser.parse_args()
    cases = json.loads(arguments.cases.read_text())
    if arguments.database:
        settings = Settings(
            arguments.database.parent.parent,
            arguments.database,
            arguments.objects,
            run_inline_worker=False,
            retrieval_strategy=arguments.retrieval_strategy,
            embedding_provider="hashing",
            embedding_model="hashing-v1",
            embedding_dimensions=384,
        )
        result = evaluate(QBRService(settings), cases, arguments.workspace)
    else:
        with tempfile.TemporaryDirectory(prefix="qbr-retrieval-eval-") as directory:
            result = evaluate(
                synthetic_service(Path(directory), arguments.retrieval_strategy),
                cases,
                arguments.workspace,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["hit_at_5"] < arguments.min_hit_at_5:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
