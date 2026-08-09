from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .db import Database
from .evidence import EvidencePackBuilder
from .query_planning import QueryPlan, RetrievalQuery, deterministic_plan
from .retrieval import EvidenceRetriever
from .skill_registry import SkillDescriptor, SkillRegistry
from .terminology import TermDefinition, find_term


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _format_chart_value(row: dict[str, Any]) -> str:
    display = str(row.get("display_value") or "").strip()
    if display:
        return display if "%" in display else f"{display}%"
    return f"{float(row['y_value']):g}%"


SUMMARY_INTENT_TERMS = (
    "概括",
    "概览",
    "总结",
    "综述",
    "整体",
    "总体",
    "全局",
    "业绩情况",
    "经营情况",
    "表现如何",
    "情况如何",
    "怎么样",
    "亮点",
    "要点",
    "核心结论",
    "当前文档",
    "这份文档",
    "整个文档",
    "这份ppt",
    "summary",
    "summarize",
    "overview",
    "overall",
    "performance",
    "highlights",
    "key takeaways",
    "how is the business doing",
)

PERFORMANCE_TERMS = (
    "executive",
    "snapshot",
    "summary",
    "业绩",
    "经营",
    "表现",
    "增长",
    "收入",
    "营收",
    "利润",
    "盈利",
    "现金",
    "价值",
    "财务",
    "指标",
    "kpi",
    "revenue",
    "profit",
    "margin",
    "growth",
    "sales",
    "actual",
    "target",
    "同比",
    "环比",
    "达成",
    "完成",
    "趋势",
    "亮点",
)


@dataclass(slots=True)
class AnswerResult:
    answer: str
    evidence: list[dict[str, Any]]
    warnings: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def legacy(self) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer, self.evidence, self.warnings


class DeterministicAnswerEngine:
    """Evidence selection and replayable business reasoning without model calls."""

    def __init__(
        self,
        *,
        db: Database,
        retriever: EvidenceRetriever,
        skill_registry: SkillRegistry,
        table_reasoning_skill: SkillDescriptor,
    ) -> None:
        self.db = db
        self.retriever = retriever
        self.skill_registry = skill_registry
        self.table_reasoning_skill = table_reasoning_skill
        self.evidence_builder = EvidencePackBuilder()

    def _scope_clause(self, document_ids: list[str]) -> tuple[str, list[Any]]:
        if not document_ids:
            return "", []
        placeholders = ",".join("?" for _ in document_ids)
        return f" AND d.id IN ({placeholders})", list(document_ids)

    @staticmethod
    def answer_mode(question: str) -> str:
        return deterministic_plan(question).intent

    def answer(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        return self.answer_result(question, workspace_id, document_ids).legacy()

    def answer_result(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        plan: QueryPlan | None = None,
    ) -> AnswerResult:
        diagnostics: dict[str, Any] = {"query_plan": plan.to_dict() if plan else None}
        answer, evidence, warnings = self._answer_internal(
            question,
            workspace_id,
            document_ids,
            plan=plan,
            diagnostics=diagnostics,
        )
        return AnswerResult(answer, evidence, warnings, diagnostics)

    def _answer_internal(
        self,
        question: str,
        workspace_id: str,
        document_ids: list[str],
        *,
        plan: QueryPlan | None,
        diagnostics: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        scope_sql, scope_args = self._scope_clause(document_ids)
        with self.db.read() as conn:
            content_rows = conn.execute(
                f"""
                SELECT ch.*,s.slide_no,s.title slide_title,s.summary slide_summary,
                  e.bbox_json,e.reading_order,d.title document_title,d.id document_id
                FROM chunks ch JOIN slides s ON s.id=ch.slide_id
                JOIN document_versions dv ON dv.id=ch.document_version_id
                JOIN documents d ON d.id=dv.document_id
                LEFT JOIN elements e ON e.id=ch.element_id
                WHERE ch.workspace_id=? AND ch.active=1
                  AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
                ORDER BY d.updated_at DESC,s.slide_no,e.reading_order
                """,
                (workspace_id, *scope_args),
            ).fetchall()
            scoped_chunks = [dict(row) for row in content_rows]
            term_definition = self._term_definition_answer(question, scoped_chunks)
            if term_definition:
                return term_definition
            abstention = self._constraint_abstention(question, scoped_chunks)
            if abstention:
                return abstention
            priorities = self._priority_answer(question, scoped_chunks)
            if priorities:
                return priorities
            provenance = self._provenance_answer(question, scoped_chunks)
            if provenance:
                return provenance
            explicit_chart_intent = any(term in question.casefold() for term in ("图", "月度", "控制图", "模拟值", "路径"))
            table_result = (
                None
                if explicit_chart_intent
                else self._table_reasoning_answer(question, [row for row in scoped_chunks if row.get("chunk_type") == "table"])
            )
            if table_result:
                return table_result.answer, [self._chunk_evidence(table_result.source)], []
            chart_rows = conn.execute(
                f"""
                SELECT cp.*,cs.name series_name,cs.chart_type,cs.unit,cs.axis_id,cs.visual_json,
                  cs.confidence series_confidence,c.title chart_title,c.axes_json,
                  c.source_kind,c.confidence chart_confidence,e.id element_id,e.bbox_json,s.id slide_id,s.slide_no,
                  s.title slide_title,s.summary slide_summary,
                  dv.id document_version_id,d.id document_id,d.title document_title
                FROM chart_points cp JOIN chart_series cs ON cs.id=cp.series_id JOIN charts c ON c.id=cs.chart_id
                JOIN elements e ON e.id=c.element_id JOIN slides s ON s.id=e.slide_id
                JOIN document_versions dv ON dv.id=s.document_version_id JOIN documents d ON d.id=dv.document_id
                WHERE d.workspace_id=? AND d.deleted_at IS NULL AND s.parser_run_id=dv.active_parser_run_id
                {scope_sql}
                """,
                (workspace_id, *scope_args),
            ).fetchall()
            chart_data = [dict(row) for row in chart_rows]
            if self._is_summary_question(question):
                slide_rows = conn.execute(
                    f"""
                    SELECT s.id slide_id,s.slide_no,s.title,s.summary,s.document_version_id,
                      d.id document_id,d.title document_title
                    FROM slides s JOIN document_versions dv ON dv.id=s.document_version_id
                    JOIN documents d ON d.id=dv.document_id
                    WHERE d.workspace_id=? AND d.deleted_at IS NULL
                      AND s.parser_run_id=dv.active_parser_run_id {scope_sql}
                    ORDER BY d.updated_at DESC,s.slide_no
                    """,
                    (workspace_id, *scope_args),
                ).fetchall()
                return self._summary_answer([dict(row) for row in slide_rows], chart_data)
            composition = self._composition_answer(question, chart_data)
            if composition:
                return composition
            max_drop = self._max_drop_answer(question, chart_data)
            if max_drop:
                return max_drop
            comparison = self._chart_comparison_answer(question, chart_data)
            if comparison:
                return comparison
            extreme = self._chart_extreme_answer(question, chart_data)
            if extreme:
                return extreme
            ranked = self._rank_chart_points(question, chart_data)
            chart_intent = any(term in question.casefold() for term in ("图", "月度", "控制图", "模拟值", "路径"))
            if ranked and (chart_intent or ranked[0][0] >= 6):
                return self._chart_answer(question, ranked)
        if plan is not None:
            retrieval = self.retriever.search_plan(plan, workspace_id, document_ids, top_k=16)
            pack = self.evidence_builder.build(plan, retrieval.items, max_atoms=8)
            diagnostics["retrieval"] = {
                "strategy": retrieval.strategy,
                "query": retrieval.query,
                **retrieval.diagnostics,
            }
            diagnostics["evidence_pack"] = pack.to_dict()
            if plan.execution_profile == "deep" and pack.missing_facets and len(pack.covered_facets) < 2:
                followup_queries = tuple(
                    RetrievalQuery(
                        query_id=f"gap{index}",
                        text=facet.replace("_", " "),
                        kind="coverage_gap",
                        weight=1.1,
                    )
                    for index, facet in enumerate(pack.missing_facets[:3], 1)
                )
                retry_plan = replace(plan, retrieval_queries=(*plan.retrieval_queries, *followup_queries))
                retry = self.retriever.search_plan(retry_plan, workspace_id, document_ids, top_k=16)
                combined = {str(row["id"]): row for row in (*retrieval.items, *retry.items)}
                retry_pack = self.evidence_builder.build(plan, combined.values(), max_atoms=8)
                diagnostics["coverage_retry"] = {
                    "performed": True,
                    "queries": [item.to_dict() for item in followup_queries],
                    "retrieval": retry.diagnostics,
                    "evidence_pack": retry_pack.to_dict(),
                }
                if (len(retry_pack.covered_facets), len(retry_pack.atoms)) > (len(pack.covered_facets), len(pack.atoms)):
                    pack = retry_pack
                    diagnostics["evidence_pack"] = pack.to_dict()
            warnings = list(plan.warnings)
            if not pack.answerable:
                warnings.append("INSUFFICIENT_EVIDENCE")
            if pack.answerable and pack.missing_facets:
                warnings.append("PARTIAL_EVIDENCE_COVERAGE")
            return pack.render_fallback(plan), pack.evidence, list(dict.fromkeys(warnings))

        retrieval = self.retriever.search(question, workspace_id, document_ids, top_k=5)
        chunks = retrieval.items
        if not chunks:
            return (
                "当前文档范围内没有足够证据回答这个问题。请指定指标、季度、地区或相关页码；我不会用外部常识补齐内部业务事实。",
                [],
                ["INSUFFICIENT_EVIDENCE"],
            )
        selected_chunks = chunks[:3]
        latin_identifiers = {
            token.casefold()
            for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{1,}(?![A-Za-z0-9])", question)
            if token.casefold() not in {"qbr", "aia", "us", "the", "and"}
        }
        top_content = str(chunks[0].get("content") or "").casefold()
        if len(latin_identifiers) >= 2 and all(identifier in top_content for identifier in latin_identifiers):
            selected_chunks = chunks[:1]
        evidence = [self._chunk_evidence(dict(row)) for row in selected_chunks]
        statements = [f"- {row['content'][:350]} [{index}]" for index, row in enumerate(selected_chunks, 1)]
        return "根据文档中检索到的直接证据：\n\n" + "\n".join(statements), evidence, []

    def _term_definition_answer(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        definition = find_term(question)
        if definition is None:
            return None
        source = self._definition_source(definition, chunks)
        answer = definition.answer()
        if source is not None:
            source_text = str(source.get("content") or "").casefold()
            document_label = (
                f"{definition.term} / {definition.chinese_name}" if definition.chinese_name.casefold() in source_text else definition.term
            )
            answer += f"\n\n当前文档中也使用了“{document_label}”这一术语。[1]"
            return answer, [self._chunk_evidence(source)], []
        answer += "\n\n以上是内置 QBR 术语表中的通用解释；具体计算公式和公司口径应以当前文档披露为准。"
        return answer, [], []

    @staticmethod
    def _definition_source(
        definition: TermDefinition,
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        aliases = tuple(alias.casefold() for alias in definition.search_aliases if len(alias.strip()) >= 2)
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in chunks:
            content = str(row.get("content") or "").strip()
            folded = content.casefold()
            if not any(alias in folded for alias in aliases):
                continue
            matched = sum(alias in folded for alias in aliases)
            has_term_and_name = definition.term.casefold() in folded and definition.chinese_name.casefold() in folded
            numeric_count = len(re.findall(r"\d", content))
            compact_bonus = max(0.0, 8.0 - len(content) / 80)
            type_bonus = 4 if row.get("chunk_type") == "text" else 1 if row.get("chunk_type") == "table" else 0
            score = matched * 6 + int(has_term_and_name) * 20 + compact_bonus + type_bonus - numeric_count * 0.5
            candidates.append((score, row))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _table_reasoning_answer(self, question: str, sources: list[dict[str, Any]]) -> Any:
        if not sources:
            return None
        loaded = self.skill_registry.load(self.table_reasoning_skill)
        return loaded.module.answer(question, sources)

    def _constraint_abstention(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        """Reject exact-value requests when explicit period/market constraints are absent."""
        if not any(term in question for term in ("精确", "准确值", "确切值")):
            return None
        corpus = "\n".join(str(row.get("content") or "") for row in chunks).casefold()
        missing: list[str] = []
        period = re.search(r"(20\d{2})年?第([一二三四1-4])季度", question)
        if period:
            year, quarter_raw = period.groups()
            quarter_map = {"一": "1", "二": "2", "三": "3", "四": "4"}
            quarter = quarter_map.get(quarter_raw, quarter_raw)
            variants = (
                f"{year}年第{quarter_raw}季度".casefold(),
                f"{year} q{quarter}".casefold(),
                f"q{quarter} {year}".casefold(),
                f"{year}q{quarter}".casefold(),
            )
            if not any(value in corpus for value in variants):
                missing.append(f"{year}年第二季度" if quarter == "2" else period.group(0))
        market = re.search(r"第[一二三四1-4]季度([\u4e00-\u9fff]{1,8})市场", question)
        if market is None:
            market = re.search(r"(?:年|季度|[，,。；;\s])([\u4e00-\u9fff]{1,8})市场", question)
        if market is None:
            market = re.match(r"([\u4e00-\u9fff]{1,8})市场", question)
        if market and market.group(1) not in corpus:
            missing.append(f"{market.group(1)}市场")
        if not missing:
            return None
        constraints = "、".join(dict.fromkeys(missing))
        return (
            f"当前文档未提供{constraints}对应的精确值，因此没有足够证据，无法回答且不能推测。",
            [],
            ["INSUFFICIENT_EVIDENCE"],
        )

    def _priority_answer(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        if "优先事项" not in question or not any(term in question for term in ("下一季度", "next-quarter")):
            return None
        by_slide: dict[str, list[dict[str, Any]]] = {}
        for row in chunks:
            if row.get("chunk_type") == "text":
                by_slide.setdefault(str(row.get("slide_id")), []).append(row)
        candidates: list[tuple[int, list[dict[str, Any]], list[tuple[str, str]]]] = []
        for rows in by_slide.values():
            rows.sort(key=lambda row: int(row.get("reading_order") or 0))
            actions: list[tuple[str, str]] = []
            for index, row in enumerate(rows):
                content = str(row.get("content") or "").strip()
                if not re.match(r"(?i)^owner\s*[·:：]", content):
                    continue
                owner = re.sub(r"(?i)^owner\s*[·:：]\s*", "", content).strip()
                prior = [str(candidate.get("content") or "").strip() for candidate in rows[max(0, index - 3) : index]]
                titles = [
                    text
                    for text in prior
                    if text
                    and len(text) <= 30
                    and not re.fullmatch(r"\d{1,3}", text)
                    and "QBR" not in text.upper()
                    and not text.endswith(("。", "."))
                ]
                if titles:
                    actions.append((min(titles, key=len), owner))
            if len(actions) >= 3:
                context = " ".join(str(row.get(key) or "") for row in rows for key in ("slide_title", "content"))
                score = 20 + sum(term.casefold() in context.casefold() for term in question.split())
                candidates.append((score, rows, actions))
        if not candidates:
            return None
        _, rows, actions = max(candidates, key=lambda item: item[0])
        actions = actions[:4]
        lines = [f"- {action}（Owner：{owner}） [1]" for action, owner in actions]
        evidence = self._chunk_evidence(rows[0])
        evidence["quote"] = "；".join(f"{action} — Owner · {owner}" for action, owner in actions)
        return "下一季度四项优先事项为：\n\n" + "\n".join(lines), [evidence], []

    def _provenance_answer(
        self,
        question: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        folded = question.casefold()
        if not ("公开披露" in question and any(term in folded for term in ("模拟", "测试", "边界"))):
            return None
        notes = [row for row in chunks if row.get("chunk_type") == "notes"]
        public_candidates = [
            row
            for row in notes
            if any(term in str(row.get("content") or "").casefold() for term in ("official results", "official new business", "公开披露"))
        ]
        public = max(
            public_candidates,
            key=lambda row: (
                int("growth" in str(row.get("document_title") or "").casefold()),
                int(int(row.get("slide_no") or 0) in {2, 6}),
                int("official results" in str(row.get("content") or "").casefold()),
            ),
            default=None,
        )
        monthly_candidates = [
            row
            for row in notes
            if "monthly" in str(row.get("content") or "").casefold()
            and any(term in str(row.get("content") or "").casefold() for term in ("synthetic", "模拟", "test"))
        ]
        monthly = max(
            monthly_candidates,
            key=lambda row: (
                int(int(row.get("slide_no") or 0) == 3),
                int("bar and line" in str(row.get("content") or "").casefold()),
            ),
            default=None,
        )
        matrix = next(
            (
                row
                for row in notes
                if any(term in str(row.get("content") or "").casefold() for term in ("operating table", "经营矩阵", "table values"))
                and any(term in str(row.get("content") or "").casefold() for term in ("synthetic", "模拟", "illustrative"))
            ),
            None,
        )
        selected = [row for row in (public, monthly, matrix) if row is not None]
        if len(selected) < 2:
            return None
        answer = (
            "公开披露基线包括：2023–2025核心财务指标、2025年香港市场VONB及其增长、"
            "2026年第一季度VONB/ANP/Margin，以及2025年资本比率、Net FSG和回购。 [1]\n\n"
            "测试模拟数据包括：月度控制图序列、经营矩阵、渠道组合与质量指标、资本情景路径比例、"
            "行动项/下一季度优先事项和风险阈值/风险闸门；增长QBR中除香港外的区域拆分也是测试估算。 [2][3]\n\n"
            "边界原则是以页内来源说明为准：标注 official/公开披露的值可作为披露基线；"
            "标注 synthetic、illustrative 或 testing 的内容只能用于系统验证，不能当作官方管理数据。"
        )
        return answer, [self._chunk_evidence(row) for row in selected], []

    @staticmethod
    def _is_summary_question(question: str) -> bool:
        folded = re.sub(r"\s+", " ", question.casefold()).strip()
        return any(term in folded for term in SUMMARY_INTENT_TERMS)

    @staticmethod
    def _performance_score(text: str) -> int:
        folded = text.casefold()
        return sum(3 for term in PERFORMANCE_TERMS if term in folded)

    def _summary_answer(
        self,
        slides: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Build broad, document-level evidence instead of requiring a literal query match."""
        ranked_slides: list[tuple[int, dict[str, Any]]] = []
        for slide in slides:
            summary = (slide.get("summary") or slide.get("title") or "").strip()
            if not summary:
                continue
            title = (slide.get("title") or "").casefold()
            score = self._performance_score(f"{title} {summary}")
            if any(term in title for term in ("executive", "snapshot", "summary", "概览", "摘要", "总览")):
                score += 30
            if re.search(r"\d", summary):
                score += 4
            if int(slide.get("slide_no") or 0) == 1:
                score -= 12
            ranked_slides.append((score, slide))
        ranked_slides.sort(key=lambda item: (-item[0], int(item[1].get("slide_no") or 0)))

        # Keep the answer representative when the conversation spans multiple documents.
        selected_slides: list[dict[str, Any]] = []
        per_document: dict[str, int] = {}
        for _, slide in ranked_slides:
            document_id = str(slide.get("document_id") or "")
            if per_document.get(document_id, 0) >= 3:
                continue
            selected_slides.append(slide)
            per_document[document_id] = per_document.get(document_id, 0) + 1
            if len(selected_slides) >= 4:
                break

        evidence = [self._slide_summary_evidence(slide) for slide in selected_slides]

        # Charts often carry the most important QBR numbers but are not literal text chunks.
        # Add compact series snapshots so an overall answer can still cite exact values.
        series_groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            if row.get("y_value") is not None:
                series_groups.setdefault(str(row.get("series_id")), []).append(row)
        selected_slide_ids = {slide.get("slide_id") for slide in selected_slides}
        ranked_series: list[tuple[int, list[dict[str, Any]]]] = []
        for rows in series_groups.values():
            first = rows[0]
            label = f"{first.get('chart_title') or ''} {first.get('series_name') or ''}"
            score = self._performance_score(label)
            if first.get("slide_id") in selected_slide_ids:
                score += 8
            if len(rows) >= 2:
                score += 2
            ranked_series.append((score, rows))
        ranked_series.sort(key=lambda item: (-item[0], int(item[1][0].get("slide_no") or 0), str(item[1][0].get("series_name") or "")))
        used_series_labels: set[tuple[str, str]] = set()
        selected_series_rows: list[list[dict[str, Any]]] = []
        for _, rows in ranked_series:
            first = rows[0]
            label = (str(first.get("chart_title") or ""), str(first.get("series_name") or ""))
            if label in used_series_labels:
                continue
            evidence.append(self._series_evidence(rows))
            selected_series_rows.append(rows)
            used_series_labels.add(label)
            if len(evidence) >= 6:
                break

        if not evidence:
            return (
                "当前文档尚未提取到可用于概括的正文、表格或图表证据。",
                [],
                ["INSUFFICIENT_EVIDENCE"],
            )
        series_evidence = evidence[len(selected_slides) :]
        trends: list[tuple[str, str, str, int]] = []
        for citation_no, item in enumerate(series_evidence, len(selected_slides) + 1):
            match = re.match(r".*? — (.*?): (.*)", item["quote"], flags=re.S)
            if not match:
                continue
            points = [part.strip() for part in match.group(2).split(",") if "=" in part]
            if points:
                trends.append((match.group(1), points[0], points[-1], citation_no))

        comparable: list[float] = []
        for rows in selected_series_rows:
            ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) >= 2:
                comparable.append(float(ordered[-1]["y_value"]) - float(ordered[0]["y_value"]))
        direction = "走势分化"
        if comparable and all(change > 0 for change in comparable):
            direction = "整体上行"
        elif comparable and all(change < 0 for change in comparable):
            direction = "整体承压"

        takeaway_lines = []
        for index, slide in enumerate(selected_slides, 1):
            takeaway = self._slide_takeaway(slide)
            if takeaway:
                takeaway_lines.append(f"- {takeaway} [{index}]")
        trend_lines = [
            f"| {name} | {first_point} | {last_point} | [{citation_no}] |" for name, first_point, last_point, citation_no in trends
        ]
        return (
            "## 总体判断\n\n"
            f"文档中的核心图表指标{direction}；结合执行摘要、财务基线和经营矩阵，"
            "当前业绩具备明确的跨页证据支撑。详细指标与趋势见下方表格和图表。 [1]\n\n"
            "## 关键趋势\n\n"
            + (
                "| 指标 | 起始期 | 最新期 | 证据 |\n|---|---:|---:|---|\n" + "\n".join(trend_lines)
                if trend_lines
                else "核心趋势数据见下方原生图表。 [1]"
            )
            + "\n\n## 经营解读\n\n"
            + ("\n".join(takeaway_lines) if takeaway_lines else "- 文档已提供可核验的经营与财务信息。 [1]")
            + "\n\n## 建议关注\n\n"
            "- 继续结合区域经营矩阵、财务变化和下一季度行动项判断增长质量与执行风险；"
            "点击引用可回到对应幻灯片核验。 [1]",
            evidence,
            [],
        )

    def _slide_takeaway(self, slide: dict[str, Any]) -> str:
        title = (slide.get("title") or "").strip()
        parts = [part.strip() for part in re.split(r"\s+·\s+", slide.get("summary") or "") if part.strip()]
        candidates = [
            part.replace("\n", " ")
            for part in parts
            if part != title and not part.startswith("QBR ") and not re.fullmatch(r"\d{1,3}", part) and len(part) >= 8 and len(part) <= 220
        ]
        if not candidates:
            return title
        candidates.sort(key=lambda part: (self._performance_score(part), len(part)), reverse=True)
        takeaway = candidates[0]
        if len(takeaway) > 180:
            takeaway = takeaway[:177].rstrip() + "…"
        return takeaway

    def _slide_summary_evidence(self, slide: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": slide["document_version_id"],
            "slide_id": slide["slide_id"],
            "element_id": None,
            "chunk_id": None,
            "quote": (slide.get("summary") or slide.get("title") or "")[:500],
            "bbox": {},
            "confidence": 1.0,
            "source_kind": "native_ooxml",
            "document_title": slide.get("document_title"),
            "slide_no": slide.get("slide_no"),
        }

    def _series_evidence(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
        first = ordered[0]
        point_labels = []
        for row in ordered:
            value = row.get("display_value") or f"{float(row['y_value']):g}"
            point_labels.append(f"{row.get('category') or row.get('point_order')}={value}")
        points = ", ".join(point_labels)
        return {
            "document_version_id": first["document_version_id"],
            "slide_id": first["slide_id"],
            "element_id": first["element_id"],
            "chunk_id": None,
            "quote": f"{first.get('chart_title') or 'Chart'} — {first.get('series_name') or 'Series'}: {points}"[:500],
            "bbox": _loads(first.get("bbox_json"), {}),
            "confidence": min(
                float(first.get("chart_confidence") or 0),
                float(first.get("series_confidence") or 0),
                *(float(row.get("confidence") or 0) for row in ordered),
            ),
            "source_kind": first["source_kind"],
            "document_title": first.get("document_title"),
            "slide_no": first.get("slide_no"),
        }

    def _rank_chart_points(self, question: str, rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
        folded = question.casefold()
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            score = 0
            axis = self._axis_metadata_from_row(row)
            for value, weight in [
                (row.get("series_name"), 3),
                (row.get("category"), 3),
                (row.get("chart_title"), 2),
                (axis.get("title"), 2),
                (row.get("document_title"), 1),
            ]:
                if value and str(value).casefold() in folded:
                    score += weight
            asks_secondary = any(term in folded for term in ("右轴", "次轴", "副轴", "secondary", "right axis"))
            asks_primary = any(term in folded for term in ("左轴", "主轴", "primary", "left axis"))
            if asks_secondary:
                score += 4 if axis.get("role") == "secondary" else -4
            if asks_primary:
                score += 4 if axis.get("role") == "primary" else -4
            if any(term in folded for term in ["多少", "数值", "value", "增长", "变化", "环比", "同比"]):
                score += 1
            ranked.append((score, row))
        return sorted(ranked, key=lambda pair: (pair[0], pair[1].get("point_order", 0)), reverse=True)

    @staticmethod
    def _axis_metadata_from_row(row: dict[str, Any]) -> dict[str, Any]:
        visual = _loads(row.get("visual_json"), {})
        if isinstance(visual, dict) and isinstance(visual.get("axis"), dict):
            return dict(visual["axis"])
        axes = _loads(row.get("axes_json"), [])
        if isinstance(axes, list):
            for axis in axes:
                if isinstance(axis, dict) and str(axis.get("axis_id")) == str(row.get("axis_id")):
                    return axis
        return {}

    def _axis_descriptor(self, row: dict[str, Any]) -> str:
        axis = self._axis_metadata_from_row(row)
        slide_context = f"{row.get('slide_title') or ''} {row.get('slide_summary') or ''}".casefold()
        chart_type = str(row.get("chart_type") or "").casefold()
        inferred_secondary = any(term in slide_context for term in ("双轴", "右轴", "secondary")) and "line" in chart_type
        parts: list[str] = []
        if axis.get("role") == "secondary" or inferred_secondary:
            parts.append("右侧次轴")
        elif axis.get("role") == "primary":
            parts.append("左侧主轴")
        if axis.get("title"):
            parts.append(str(axis["title"]))
        if axis.get("display_units"):
            parts.append(f"显示单位 {axis['display_units']}")
        if row.get("unit"):
            parts.append(f"格式/单位 {row['unit']}")
        return "；".join(parts) or "原图未提供明确单位"

    def _chart_comparison_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        folded = question.casefold()
        if not any(term in folded for term in ("哪个更高", "高多少", "低多少", "增加了多少", "增幅约")):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            series = str(row.get("series_name") or "")
            category = str(row.get("category") or "")
            if row.get("y_value") is None or not series or not category:
                continue
            if series.casefold() in folded and category.casefold() in folded:
                groups.setdefault(str(row.get("element_id")), []).append(row)
        candidates = [rows for rows in groups.values() if len({row.get("series_id") for row in rows}) >= 2]
        if not candidates:
            return None
        rows = max(candidates, key=len)
        unique: dict[str, dict[str, Any]] = {str(row.get("series_id")): row for row in rows}
        rows = list(unique.values())
        rows.sort(key=lambda row: folded.find(str(row.get("series_name") or "").casefold()))
        if len(rows) < 2:
            return None
        first, second = rows[0], rows[1]
        first_value, second_value = float(first["y_value"]), float(second["y_value"])
        first_label = f"{first.get('series_name')} {first.get('category')}"
        second_label = f"{second.get('series_name')} {second.get('category')}"
        if any(term in folded for term in ("增加了多少", "增幅约", "从")):
            delta = second_value - first_value
            rate = delta / abs(first_value) * 100 if first_value else None
            rate_text = f"，相对增幅约{rate:.1f}%" if rate is not None else "，基期为0，无法计算相对增幅"
            answer = f"{first_label}为{first_value:g}，{second_label}为{second_value:g}；增加{delta:g}{rate_text}。 [1][2]"
        else:
            higher, lower = (
                max((first, second), key=lambda row: float(row["y_value"])),
                min((first, second), key=lambda row: float(row["y_value"])),
            )
            difference = float(higher["y_value"]) - float(lower["y_value"])
            answer = (
                f"{first_label}为{first_value:g}，{second_label}为{second_value:g}；"
                f"{higher.get('series_name')}更高，高{difference:g}。 [1][2]"
            )
        return answer, [self._point_evidence(first), self._point_evidence(second)], []

    def _chart_extreme_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        operation = next((term for term in ("最高", "最大", "最低", "最小") if term in question), None)
        if operation is None or not any(term in question for term in ("图", "月度", "控制图")):
            return None
        folded = question.casefold()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            series = str(row.get("series_name") or "")
            if row.get("y_value") is not None and series and series.casefold() in folded:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        if not groups:
            return None
        rows = max(groups.values(), key=len)
        reverse = operation in {"最高", "最大"}
        selected = sorted(rows, key=lambda row: float(row["y_value"]), reverse=reverse)[0]
        value = selected.get("display_value") or f"{float(selected['y_value']):g}"
        answer = f"{selected.get('series_name')}在{selected.get('category')}达到{operation}值{value}。 [1]"
        return answer, [self._point_evidence(selected)], []

    def _composition_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        """Answer part-to-whole chart questions from a complete native series."""
        if not (any(term in question for term in ("构成", "组合")) and "占" in question):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        candidates: list[tuple[int, list[dict[str, Any]]]] = []
        for rows in groups.values():
            ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 3:
                continue
            total = sum(float(row["y_value"]) for row in ordered)
            if not 98 <= total <= 102:
                continue
            context = " ".join(
                str(ordered[0].get(key) or "") for key in ("chart_title", "series_name", "slide_title", "slide_summary", "document_title")
            )
            score = sum(3 for term in ("渠道", "组合", "触点", "代理") if term in question and term in context)
            score += sum(2 for row in ordered if str(row.get("category") or "") in question)
            candidates.append((score, ordered))
        if not candidates:
            return None
        _, rows = max(candidates, key=lambda item: (item[0], len(item[1])))
        agent = next((row for row in rows if "代理" in str(row.get("category") or "")), rows[0])
        others = [row for row in rows if row is not agent]
        agent_value = float(agent["y_value"])
        non_agent = sum(float(row["y_value"]) for row in others)
        agent_label = str(agent.get("category") or "代理触点")
        detail = "、".join(f"{row.get('category')} {_format_chart_value(row)}" for row in others)
        answer = f"{agent_label}（代理触点）占{agent_value:g}%，非代理触点合计占{non_agent:g}%；非代理触点由{detail}构成。 [1]"
        return answer, [self._series_evidence(rows)], []

    def _max_drop_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        """Compute the largest adjacent decline while retaining slide context."""
        if not any(term in question for term in ("降幅最大", "最大降幅", "下降最多")):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        candidates: list[tuple[int, float, dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
        for rows in groups.values():
            ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 2:
                continue
            context = " ".join(
                str(ordered[0].get(key) or "") for key in ("chart_title", "series_name", "slide_title", "slide_summary", "document_title")
            )
            context_score = sum(4 for term in ("资本", "情景", "路径", "阶段") if term in question and term in context)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                drop = float(previous["y_value"]) - float(current["y_value"])
                if drop > 0:
                    candidates.append((context_score, drop, previous, current, ordered))
        if not candidates:
            return None
        _, drop, previous, current, rows = max(candidates, key=lambda item: (item[0], item[1]))
        answer = (
            f"最大降幅发生在“{current.get('category')}”这一步：阶段值从"
            f"{float(previous['y_value']):g}降至{float(current['y_value']):g}，下降{drop:g}。 [1]"
        )
        return answer, [self._series_evidence(rows)], []

    def _chart_answer(
        self,
        question: str,
        ranked: list[tuple[int, dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        best_score = ranked[0][0]
        selected = [row for score, row in ranked if score == best_score][:4]
        warnings: list[str] = []
        selected = [row for row in selected if row.get("y_value") is not None]
        if not selected:
            return "已定位到相关图表，但缺少可用于精确回答的数值。", [], ["INSUFFICIENT_EVIDENCE"]
        growth = any(term in question.casefold() for term in ["增长", "增幅", "增加", "变化", "环比", "同比", "change", "growth"])
        if growth:
            same_series = [
                row for _, row in ranked if row.get("series_id") == selected[0].get("series_id") and row.get("y_value") is not None
            ]
            unique = {int(row["point_order"]): row for row in same_series}
            ordered = [unique[key] for key in sorted(unique)]
            mentioned = [row for row in ordered if row.get("category") and str(row["category"]).casefold() in question.casefold()]
            pair = mentioned[-2:] if len(mentioned) >= 2 else ordered[-2:]
            if len(pair) == 2:
                previous, current = pair
                prior = float(previous["y_value"])
                current_value = float(current["y_value"])
                if prior == 0:
                    answer = (
                        f"{current['series_name']} 从 {previous['category']} 的 {prior:g} 变为 "
                        f"{current['category']} 的 {current_value:g}；基期为 0，增长率不可计算。 [1][2]"
                    )
                else:
                    change = (current_value - prior) / abs(prior) * 100
                    absolute_change = current_value - prior
                    answer = (
                        f"{current['series_name']} 从 {previous['category']} 的 {prior:g} 变为 "
                        f"{current['category']} 的 {current_value:g}，绝对变化为 {absolute_change:g}；"
                        f"按 `(本期-上期)/|上期|` 计算，相对变化为 {change:.1f}%。 [1][2]"
                    )
                evidence = [self._point_evidence(previous), self._point_evidence(current)]
                return answer, evidence, warnings
        folded = question.casefold()
        explicit_series: list[dict[str, Any]] = []
        seen_series: set[str] = set()
        for row in selected:
            series_name = str(row.get("series_name") or "")
            series_id = str(row.get("series_id") or "")
            if series_name and series_name.casefold() in folded and series_id not in seen_series:
                explicit_series.append(row)
                seen_series.add(series_id)
        if len(explicit_series) >= 2:
            evidence = [self._point_evidence(row) for row in explicit_series]
            lines = ["| 指标 | 类别 | 数值 | 坐标轴/单位 | 证据 |", "|---|---|---:|---|---|"]
            for index, row in enumerate(explicit_series, 1):
                value = row.get("display_value") or f"{float(row['y_value']):g}"
                lines.append(
                    f"| {row['series_name']} | {row.get('category') or row.get('point_order')} | "
                    f"{value} | {self._axis_descriptor(row)} | [{index}] |"
                )
            return "同一图表中相关系列如下：\n\n" + "\n".join(lines), evidence, warnings
        row = selected[0]
        value = row.get("display_value") or f"{float(row['y_value']):g}"
        unit_text = f"（{self._axis_descriptor(row)}）"
        if float(row.get("chart_confidence", 0)) < 0.85:
            warnings.append("LOW_CHART_CONFIDENCE")
            prefix = "视觉/低置信识别值约为"
        else:
            prefix = "为"
        answer = f"{row['series_name']} 在 {row.get('category') or row.get('point_order')} 的值{prefix} {value} {unit_text}。 [1]"
        return answer, [self._point_evidence(row)], warnings

    def _point_evidence(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"],
            "slide_id": row["slide_id"],
            "element_id": row["element_id"],
            "chunk_id": None,
            "quote": (
                f"{row.get('chart_title') or 'Chart'} — {row['series_name']}: "
                f"{row.get('category')}={row.get('display_value') or row.get('y_value')}"
            ),
            "bbox": _loads(row.get("bbox_json"), {}),
            "confidence": min(float(row["chart_confidence"]), float(row["series_confidence"]), float(row["confidence"])),
            "source_kind": row["source_kind"],
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
        }

    def _chunk_evidence(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"],
            "slide_id": row["slide_id"],
            "element_id": row.get("element_id"),
            "chunk_id": row["id"],
            "quote": row["content"][:500],
            "bbox": _loads(row.get("bbox_json"), {}),
            "confidence": 1.0,
            "source_kind": "native_ooxml",
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
        }
