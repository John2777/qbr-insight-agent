from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.qbr_core import QBRService
from packages.qbr_core.config import Settings
from packages.qbr_core.verification import markdown_format_integrity

QUESTIONS = (
    "本季度公司整体经营表现如何？哪些核心指标高于计划，哪些低于计划？",
    "VONB、OPAT和新业务价值率分别同比增长多少？增长主要来自哪些业务板块？",
    "最近24个月的经营数据中，哪些月份出现了收入增长但利润率下降的情况？",
    "哪些产品对本季度新增业务价值贡献最大？请列出贡献排名和同比变化。",
    "从季度趋势看，公司目前处于加速增长、稳定增长还是增长放缓阶段？依据是什么？",
    "本季度实际结果与预算之间最大的三个差异是什么？分别造成了多大的财务影响？",
    "如果剔除表现最好的区域，公司整体增长率和盈利水平会发生什么变化？",
    "哪些区域同时实现了业务规模增长、利润率提升和客户质量改善？",
    "表现最差的三个区域分别存在哪些问题？是销售能力、产品结构还是成本效率问题？",
    "代理人、银保和数字渠道中，哪个渠道的综合投入产出比最高？",
    "哪个渠道的新业务增长最快，但续保率或客户质量存在潜在风险？",
    "代理人数量、活跃率、人均产能和新单保费之间有什么关系？",
    "数字渠道渗透率提升是否真正带来了单位获客成本下降？请用数据说明。",
    "客户满意度、投诉率、续保率和交叉销售率之间是否存在明显关联？",
    "哪些客户群体增长最快？这些客户是否同时具备较高价值和较低流失风险？",
    "当前资本充足率和偿付能力水平是否处于安全区间？距离管理层预警线还有多少缓冲？",
    "在不同压力测试情景下，哪个风险因素对资本水平和盈利影响最大？",
    "哪些压力情景会导致关键指标跌破管理层风险阈值？需要提前采取什么措施？",
    "利率、市场波动、退保率和理赔恶化同时发生时，公司最脆弱的业务区域或产品是什么？",
    "综合QBR02、QBR03和9MB综合版的数据，下季度管理层最应该优先推动的五项行动是什么？请给出数据依据、责任领域和预期影响。",
)
COMPLEX_QUESTIONS = {3, 7, 11, 14, 18, 20}
WORKSPACE_ID = "ws_demo"
HEADERS = {"X-Workspace-ID": WORKSPACE_ID, "X-User-ID": "user_demo"}
PPT_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
QWEN_SINGAPORE_BASE_URL = (
    "https://ws-iuzncg7jj8e6onlg.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
)
QWEN_SINGAPORE_MODELS = {
    "answer": "qwen3.7-plus",
    "planner": "qwen3.6-flash",
    "deep": "qwen3.7-max",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the 20-question E2E matrix against output/*.pptx.")
    parser.add_argument("--workers", type=int, default=4, help="Number of independent queue consumers.")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results"), help="Report directory.")
    parser.add_argument("--ppt-dir", type=Path, default=Path("output"), help="PPT directory.")
    parser.add_argument("--only-cross", action="store_true", help="Run only the QBR02/QBR03/9MB Q20 check.")
    parser.add_argument("--skip-cross", action="store_true", help="Do not queue the cross-document Q20 check.")
    parser.add_argument("--single-scope", choices=("QBR01", "QBR02", "QBR03", "9MB"))
    parser.add_argument("--questions", help="Comma-separated single-document question numbers (default: all 20).")
    parser.add_argument(
        "--model-profile",
        choices=("current", "qwen-singapore"),
        default="current",
        help="LLM configuration profile. qwen-singapore requires DASHSCOPE_API_KEY or QWEN_API_KEY.",
    )
    return parser.parse_args()


def _settings_for_model_profile(base: Settings, profile: str) -> Settings:
    if profile == "current":
        return base
    if profile != "qwen-singapore":
        raise ValueError(f"Unknown model profile: {profile}")

    api_key = (os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY") or "").strip()
    current_qwen_identity = " ".join(
        (base.llm_provider, base.llm_model, base.planner_model, base.deep_llm_model)
    ).casefold()
    current_is_qwen_singapore = (
        "ap-southeast-1.maas.aliyuncs.com" in base.llm_base_url.casefold()
        and any(marker in current_qwen_identity for marker in ("qwen", "dashscope", "bailian"))
    )
    if not api_key and current_is_qwen_singapore:
        api_key = base.llm_api_key
    if not api_key:
        raise RuntimeError(
            "qwen-singapore requires DASHSCOPE_API_KEY or QWEN_API_KEY. "
            "The current LLM_API_KEY is reused only when the active configuration already targets "
            "Qwen on the Singapore Bailian endpoint."
        )

    settings = replace(
        base,
        llm_enabled=True,
        llm_provider="qwen-bailian-singapore",
        llm_base_url=os.getenv("QWEN_SINGAPORE_BASE_URL", QWEN_SINGAPORE_BASE_URL).strip().rstrip("/"),
        llm_api_key=api_key,
        llm_model=os.getenv("QWEN_ANSWER_MODEL", QWEN_SINGAPORE_MODELS["answer"]).strip(),
        planner_model=os.getenv("QWEN_PLANNER_MODEL", QWEN_SINGAPORE_MODELS["planner"]).strip(),
        deep_llm_model=os.getenv("QWEN_DEEP_MODEL", QWEN_SINGAPORE_MODELS["deep"]).strip(),
    )
    settings.validate()
    if not settings.llm_configured:
        raise RuntimeError("qwen-singapore profile is incomplete after applying its overrides.")
    return settings


def _checked(response: Any, context: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"{context}: HTTP {response.status_code}: {response.text[:1000]}") from exc
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{context}: expected object response")
    return value


def _ppt_paths(directory: Path) -> list[Path]:
    paths = sorted(path.resolve() for path in directory.glob("*.pptx") if not path.name.startswith(".~"))
    if len(paths) != 4:
        names = ", ".join(path.name for path in paths) or "none"
        raise RuntimeError(f"Expected exactly four non-lock PPTX files in {directory}; found {len(paths)}: {names}")
    return paths


def _document_key(path: Path) -> str:
    name = path.stem.casefold()
    if "qbr_01" in name:
        return "QBR01"
    if "qbr_02" in name:
        return "QBR02"
    if "qbr_03" in name:
        return "QBR03"
    if "9mb" in name:
        return "9MB"
    return path.stem


def _document_profiles(service: QBRService, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    with service.db.read() as conn:
        for document in documents:
            doc_id = document["document_id"]
            base = conn.execute(
                """
                SELECT d.status,d.title,dv.id version_id,pr.status parser_status,pr.quality_json
                FROM documents d JOIN document_versions dv ON dv.document_id=d.id
                LEFT JOIN parser_runs pr ON pr.id=dv.active_parser_run_id
                WHERE d.id=? ORDER BY dv.version_no DESC LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
            counts = conn.execute(
                """
                SELECT
                  count(DISTINCT s.id) slides,
                  count(DISTINCT e.id) elements,
                  count(DISTINCT CASE WHEN e.element_type='table' THEN e.id END) tables,
                  count(DISTINCT c.id) charts,
                  count(DISTINCT cs.id) chart_series,
                  count(DISTINCT cp.id) chart_points
                FROM document_versions dv
                LEFT JOIN slides s ON s.document_version_id=dv.id AND s.parser_run_id=dv.active_parser_run_id
                LEFT JOIN elements e ON e.slide_id=s.id
                LEFT JOIN charts c ON c.element_id=e.id
                LEFT JOIN chart_series cs ON cs.chart_id=c.id
                LEFT JOIN chart_points cp ON cp.series_id=cs.id
                WHERE dv.document_id=?
                """,
                (doc_id,),
            ).fetchone()
            chunk_count = conn.execute(
                """
                SELECT count(*) FROM chunks ch JOIN document_versions dv ON dv.id=ch.document_version_id
                WHERE dv.document_id=? AND ch.active=1
                """,
                (doc_id,),
            ).fetchone()[0]
            profiles.append(
                {
                    "key": document["key"],
                    "file": document["path"],
                    "size_bytes": document["size_bytes"],
                    "document_id": doc_id,
                    "status": base["status"] if base else None,
                    "parser_status": base["parser_status"] if base else None,
                    "parser_quality": json.loads(base["quality_json"] or "{}") if base else {},
                    **dict(counts),
                    "chunks": chunk_count,
                }
            )
    return profiles


def _run_record(spec: dict[str, Any], result: dict[str, Any], elapsed: float) -> dict[str, Any]:
    message = result.get("message") or {}
    metadata = message.get("metadata") or {}
    model = result.get("model") or {}
    verification = metadata.get("verification") or {}
    evidence_pack = metadata.get("evidence_pack") or {}
    citations = result.get("citations") or []
    answer = str(message.get("content") or "")
    warnings = list(result.get("warnings") or [])
    citation_documents = list(
        dict.fromkeys(str(item.get("document_title") or "") for item in citations if item.get("document_title"))
    )
    citation_slides = sorted(
        {int(item["slide_no"]) for item in citations if isinstance(item.get("slide_no"), int)}
    )
    checks = {
        "completed": result.get("status") == "completed",
        "not_numbered_evidence_dump": not answer.lstrip().startswith("编号证据"),
        "has_substantive_answer": len(answer.strip()) >= 80,
        "has_citation_or_insufficient_signal": bool(citations) or "INSUFFICIENT_EVIDENCE" in warnings,
        "markdown_bold_balanced": markdown_format_integrity(answer),
    }
    checks["basic_pass"] = all(checks.values())
    return {
        **spec,
        "status": result.get("status"),
        "warnings": warnings,
        "model": model,
        "answer_source": model.get("answer_source"),
        "model_status": model.get("status"),
        "planner": model.get("planner"),
        "verification": verification,
        "verification_disposition": verification.get("disposition"),
        "evidence_pack": evidence_pack,
        "citation_count": len(citations),
        "citation_documents": citation_documents,
        "citation_slides": citation_slides,
        "citations": citations,
        "answer": answer,
        "answer_length": len(answer),
        "elapsed_seconds": round(elapsed, 3),
        "checks": checks,
        "error_detail": result.get("error_detail"),
    }


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": record["scope"],
        "question_no": record["question_no"],
        "status": record["status"],
        "answer_source": record["answer_source"],
        "verification": record["verification_disposition"],
        "warnings": record["warnings"],
        "citations": record["citation_count"],
        "answer_length": record["answer_length"],
        "elapsed_seconds": record["elapsed_seconds"],
        "basic_pass": record["checks"]["basic_pass"],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in records if item["status"] == "completed"]
    sources = {source: sum(item["answer_source"] == source for item in records) for source in {
        "model", "model_repaired", "safe_fallback", None
    }}
    warning_counts: dict[str, int] = {}
    for item in records:
        for warning in item["warnings"]:
            warning_counts[warning] = warning_counts.get(warning, 0) + 1
    return {
        "runs": len(records),
        "completed": len(completed),
        "failed": len(records) - len(completed),
        "basic_pass": sum(item["checks"]["basic_pass"] for item in records),
        "answer_sources": {str(key): value for key, value in sources.items() if value},
        "warning_counts": dict(sorted(warning_counts.items())),
        "avg_citations": round(statistics.mean(item["citation_count"] for item in records), 2) if records else 0,
        "avg_answer_length": round(statistics.mean(item["answer_length"] for item in records), 1) if records else 0,
        "avg_elapsed_seconds": round(statistics.mean(item["elapsed_seconds"] for item in records), 2) if records else 0,
    }


def _markdown(report: dict[str, Any]) -> str:
    records = report["runs"]
    individual = [item for item in records if item["scope_type"] == "single_document"]
    lines = [
        "# Output PPT 端到端问答矩阵",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 配置：{report['configuration']['model_profile']} / "
        f"{report['configuration']['provider']} / {report['configuration']['model']} / "
        f"{report['configuration']['retrieval_strategy']}，{report['configuration']['workers']} workers",
        f"- 规模：{len(individual)} 个单文档问题 + {len(records) - len(individual)} 个跨文档问题",
        "",
        "## 总览",
        "",
        "| 范围 | 运行 | 完成 | 基础通过 | Model | Repaired | Fallback | 平均引用 | 警告 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    scopes = [profile["key"] for profile in report["document_profiles"]] + ["QBR02+QBR03+9MB"]
    for scope in scopes:
        scoped = [item for item in records if item["scope"] == scope]
        if not scoped:
            continue
        stats = _aggregate(scoped)
        sources = stats["answer_sources"]
        warnings = ", ".join(f"{key}:{value}" for key, value in stats["warning_counts"].items()) or "-"
        lines.append(
            f"| {scope} | {stats['runs']} | {stats['completed']} | {stats['basic_pass']} | "
            f"{sources.get('model', 0)} | {sources.get('model_repaired', 0)} | "
            f"{sources.get('safe_fallback', 0)} | {stats['avg_citations']} | {warnings} |"
        )

    lines.extend(
        [
            "",
            "## 文档解析覆盖",
            "",
            "| 文档 | 状态 | 页 | 元素 | 表格 | 图表 | 序列 | 数据点 | Chunks |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in report["document_profiles"]:
        lines.append(
            f"| {profile['key']} | {profile['status']}/{profile['parser_status']} | {profile['slides']} | "
            f"{profile['elements']} | {profile['tables']} | {profile['charts']} | {profile['chart_series']} | "
            f"{profile['chart_points']} | {profile['chunks']} |"
        )

    lines.extend(
        [
            "",
            "## 按题汇总（四份单文档）",
            "",
            "| 题号 | 复杂题 | 完成 | 基础通过 | Model/Repaired/Fallback | 平均引用 | 平均长度 | 警告 |",
            "|---:|:---:|---:|---:|---|---:|---:|---|",
        ]
    )
    for question_no in range(1, len(QUESTIONS) + 1):
        scoped = [item for item in individual if item["question_no"] == question_no]
        stats = _aggregate(scoped)
        sources = stats["answer_sources"]
        warnings = ", ".join(f"{key}:{value}" for key, value in stats["warning_counts"].items()) or "-"
        lines.append(
            f"| {question_no} | {'是' if question_no in COMPLEX_QUESTIONS else '-'} | "
            f"{stats['completed']}/4 | {stats['basic_pass']}/4 | {sources.get('model', 0)}/"
            f"{sources.get('model_repaired', 0)}/{sources.get('safe_fallback', 0)} | "
            f"{stats['avg_citations']} | {stats['avg_answer_length']} | {warnings} |"
        )

    lines.extend(
        [
            "",
            "## 复杂题诊断",
            "",
            "| 范围 | 题号 | 来源 | 校验 | 引用 | 警告 | 答案摘要 |",
            "|---|---:|---|---|---:|---|---|",
        ]
    )
    complex_records = [item for item in records if item["complex"]]
    for item in complex_records:
        excerpt = " ".join(item["answer"].replace("|", "\\|").split())[:180]
        warnings = ", ".join(item["warnings"]) or "-"
        lines.append(
            f"| {item['scope']} | {item['question_no']} | {item['answer_source']} | "
            f"{item['verification_disposition']} | {item['citation_count']} | {warnings} | {excerpt} |"
        )

    notable = [
        item for item in records
        if item["answer_source"] != "model" or item["warnings"] or not item["checks"]["basic_pass"]
    ]
    lines.extend(["", "## 降级与异常明细", ""])
    if not notable:
        lines.append("无。")
    else:
        for item in notable:
            lines.append(
                f"- {item['scope']} Q{item['question_no']}：status={item['status']}，"
                f"source={item['answer_source']}，verification={item['verification_disposition']}，"
                f"warnings={item['warnings'] or '-'}，citations={item['citation_count']}，"
                f"basic_pass={item['checks']['basic_pass']}"
            )

    cross = [item for item in records if item["scope_type"] == "cross_document"]
    if cross:
        item = cross[0]
        lines.extend(["", "## 跨文档 Q20 完整回答", "", item["answer"], ""])
    return "\n".join(lines)


def main() -> None:
    args = _arguments()
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.only_cross and args.skip_cross:
        raise SystemExit("--only-cross and --skip-cross cannot be combined")
    selected_questions = set(range(1, len(QUESTIONS) + 1))
    if args.questions:
        try:
            selected_questions = {int(value.strip()) for value in args.questions.split(",") if value.strip()}
        except ValueError as exc:
            raise SystemExit("--questions must contain comma-separated integers") from exc
        if not selected_questions or min(selected_questions) < 1 or max(selected_questions) > len(QUESTIONS):
            raise SystemExit(f"--questions values must be between 1 and {len(QUESTIONS)}")
    paths = _ppt_paths(args.ppt_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = args.output.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"output_ppt_e2e_matrix_{timestamp}.json"
    markdown_path = json_path.with_suffix(".md")
    partial_path = report_dir / f"output_ppt_e2e_matrix_{timestamp}.partial.json"

    base = _settings_for_model_profile(Settings.from_env(), args.model_profile)
    if not base.llm_configured:
        raise RuntimeError("A real E2E run requires a configured LLM provider.")
    logging.disable(logging.CRITICAL)
    started_at = time.monotonic()
    run_label = "cross-document Q20 only" if args.only_cross else "selected single-document matrix"
    print(
        f"E2E matrix: {run_label}; profile={args.model_profile}; provider={base.llm_provider}; "
        f"answer={base.llm_model}; planner={base.planner_model or base.llm_model}; "
        f"deep={base.deep_llm_model or base.llm_model}; workers={args.workers}",
        flush=True,
    )

    with tempfile.TemporaryDirectory(prefix="qbr_output_matrix_") as temp_name:
        temp_root = Path(temp_name)
        data_dir = temp_root / "data"
        settings = replace(
            base,
            data_dir=data_dir,
            database_path=data_dir / "app.sqlite3",
            object_dir=data_dir / "objects",
            vector_index_dir=data_dir / "vector_indexes",
            auth_mode="demo",
            app_env="local",
            run_inline_worker=False,
            worker_poll_seconds=0.1,
            vision_enabled=False,
        )
        app = create_app(settings)
        documents: list[dict[str, Any]] = []
        run_specs: dict[str, dict[str, Any]] = {}

        with TestClient(app) as client:
            for path in paths:
                key = _document_key(path)
                with path.open("rb") as handle:
                    response = client.post(
                        "/api/v1/documents",
                        headers=HEADERS,
                        files={"file": (path.name, handle, PPT_MIME)},
                        data={"title": path.stem, "deduplication": "reject"},
                    )
                upload = _checked(response, f"upload {path.name}")
                documents.append(
                    {
                        "key": key,
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "document_id": upload["document"]["id"],
                        "job_id": upload["job"]["id"],
                    }
                )
                print(f"uploaded {key}: {path.name}", flush=True)

            processed_jobs = 0
            while True:
                try:
                    job_id = app.state.service.process_next_job("matrix-ingest")
                except Exception as exc:
                    print(f"ingestion exception: {type(exc).__name__}: {exc}", flush=True)
                    continue
                if not job_id:
                    break
                processed_jobs += 1
                job = app.state.service.get_job(job_id, WORKSPACE_ID)
                print(
                    f"ingested {processed_jobs}/4: {job_id} status={job['status']} "
                    f"slides={job['processed_slides']}/{job['total_slides']}",
                    flush=True,
                )
            if processed_jobs != 4:
                raise RuntimeError(f"Expected four ingestion jobs, processed {processed_jobs}")
            for document in documents:
                job = app.state.service.get_job(document["job_id"], WORKSPACE_ID)
                if job["status"] not in {"ready", "partial"}:
                    raise RuntimeError(f"Ingestion failed for {document['key']}: {job}")

            profiles = _document_profiles(app.state.service, documents)
            print("document profiles: " + json.dumps(profiles, ensure_ascii=False), flush=True)

            if not args.only_cross:
                for document in documents:
                    if args.single_scope and document["key"] != args.single_scope:
                        continue
                    for question_no, question in enumerate(QUESTIONS, 1):
                        if question_no not in selected_questions:
                            continue
                        conversation = _checked(
                            client.post(
                                "/api/v1/conversations",
                                headers=HEADERS,
                                json={
                                    "title": f"Matrix {document['key']} Q{question_no}",
                                    "document_ids": [document["document_id"]],
                                },
                            ),
                            f"create conversation {document['key']} Q{question_no}",
                        )
                        queued = _checked(
                            client.post(
                                f"/api/v1/conversations/{conversation['id']}/messages",
                                headers=HEADERS,
                                json={
                                    "content": question,
                                    "client_message_id": f"matrix-{document['key']}-{question_no}-{timestamp}",
                                },
                            ),
                            f"queue {document['key']} Q{question_no}",
                        )
                        run_specs[queued["run_id"]] = {
                            "run_id": queued["run_id"],
                            "scope": document["key"],
                            "scope_type": "single_document",
                            "document_ids": [document["document_id"]],
                            "question_no": question_no,
                            "question": question,
                            "complex": question_no in COMPLEX_QUESTIONS,
                        }

            if not args.skip_cross:
                cross_documents = [item for item in documents if item["key"] in {"QBR02", "QBR03", "9MB"}]
                if len(cross_documents) != 3:
                    raise RuntimeError("Could not resolve QBR02, QBR03 and 9MB for cross-document Q20")
                question_no = 20
                question = QUESTIONS[question_no - 1]
                conversation = _checked(
                    client.post(
                        "/api/v1/conversations",
                        headers=HEADERS,
                        json={
                            "title": "Matrix cross-document Q20",
                            "document_ids": [item["document_id"] for item in cross_documents],
                        },
                    ),
                    "create cross-document conversation Q20",
                )
                queued = _checked(
                    client.post(
                        f"/api/v1/conversations/{conversation['id']}/messages",
                        headers=HEADERS,
                        json={"content": question, "client_message_id": f"matrix-cross-20-{timestamp}"},
                    ),
                    "queue cross-document Q20",
                )
                run_specs[queued["run_id"]] = {
                    "run_id": queued["run_id"],
                    "scope": "QBR02+QBR03+9MB",
                    "scope_type": "cross_document",
                    "document_ids": [item["document_id"] for item in cross_documents],
                    "question_no": question_no,
                    "question": question,
                    "complex": True,
                }
            if not run_specs:
                raise RuntimeError("No runs selected")
            print(f"queued {len(run_specs)} runs", flush=True)

            records: list[dict[str, Any]] = []
            records_lock = threading.Lock()
            completed = 0

            def checkpoint() -> None:
                _write_json(
                    partial_path,
                    {
                        "generated_at": datetime.now().isoformat(timespec="seconds"),
                        "expected_runs": len(run_specs),
                        "document_profiles": profiles,
                        "runs": sorted(records, key=lambda item: (item["scope"], item["question_no"])),
                    },
                )

            def worker(worker_no: int) -> None:
                nonlocal completed
                service = QBRService(settings)
                worker_id = f"matrix-worker-{worker_no}"
                while True:
                    try:
                        call_started = time.monotonic()
                        run_id = service.process_next_run(worker_id)
                    except Exception as exc:
                        print(f"{worker_id} exception: {type(exc).__name__}: {exc}", flush=True)
                        time.sleep(0.2)
                        continue
                    if run_id:
                        result = service.get_run(run_id, WORKSPACE_ID)
                        elapsed = time.monotonic() - call_started
                        record = _run_record(run_specs[run_id], result, elapsed)
                        with records_lock:
                            records.append(record)
                            completed += 1
                            checkpoint()
                            print(
                                f"[{completed}/{len(run_specs)}] {record['scope']} Q{record['question_no']} "
                                f"status={record['status']} source={record['answer_source']} "
                                f"verify={record['verification_disposition']} warnings={record['warnings']} "
                                f"citations={record['citation_count']}",
                                flush=True,
                            )
                        continue
                    with service.db.read() as conn:
                        unfinished = conn.execute(
                            "SELECT count(*) FROM runs WHERE status IN ('pending','running')"
                        ).fetchone()[0]
                    if unfinished == 0:
                        return
                    time.sleep(0.1)

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [executor.submit(worker, worker_no) for worker_no in range(1, args.workers + 1)]
                for future in futures:
                    future.result()

            seen = {item["run_id"] for item in records}
            for run_id, spec in run_specs.items():
                if run_id in seen:
                    continue
                result = _checked(client.get(f"/api/v1/runs/{run_id}", headers=HEADERS), f"collect {run_id}")
                records.append(_run_record(spec, result, 0.0))

        records.sort(key=lambda item: (item["scope_type"], item["scope"], item["question_no"]))
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "configuration": {
                "model_profile": args.model_profile,
                "provider": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "model": settings.llm_model,
                "planner_model": settings.planner_model or settings.llm_model,
                "deep_model": settings.deep_llm_model or settings.llm_model,
                "retrieval_strategy": settings.retrieval_strategy,
                "vector_configured": settings.vector_configured,
                "rerank_configured": settings.rerank_configured,
                "vision_enabled": settings.vision_enabled,
                "workers": args.workers,
            },
            "questions": [
                {"number": index, "question": question, "complex": index in COMPLEX_QUESTIONS}
                for index, question in enumerate(QUESTIONS, 1)
            ],
            "document_profiles": profiles,
            "summary": _aggregate(records),
            "runs": records,
        }
        _write_json(json_path, report)
        markdown_path.write_text(_markdown(report), encoding="utf-8")
        partial_path.unlink(missing_ok=True)
        print("FINAL_SUMMARY=" + json.dumps(report["summary"], ensure_ascii=False), flush=True)
        print(f"JSON_REPORT={json_path}", flush=True)
        print(f"MARKDOWN_REPORT={markdown_path}", flush=True)


if __name__ == "__main__":
    main()
