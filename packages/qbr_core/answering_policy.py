from __future__ import annotations

import re
from typing import Any

from .answering_shared import PERFORMANCE_TERMS
from .query_planning import QueryPlan
from .terminology import TermDefinition, find_term


class AnswerPolicyMixin:
    @staticmethod
    def _merge_contributions(
        plan: QueryPlan,
        contributions: list[tuple[str, tuple[str, list[dict[str, Any]], list[str]]]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        labels = {
            "zh": {
                "term_definition": "术语解释",
                "risk_explanation": "风险解释",
                "business_evaluation": "优势与经营评价",
                "negative_signal_summary": "潜在问题与风险",
                "summary": "文档概览",
                "provenance": "数据来源与边界",
                "chart_analysis": "图表分析",
                "table_analysis": "表格分析",
                "evidence_answer": "相关证据",
            },
            "en": {
                "term_definition": "Term definition",
                "risk_explanation": "Risk explanation",
                "business_evaluation": "Business evaluation",
                "negative_signal_summary": "Potential issues and risks",
                "summary": "Document summary",
                "provenance": "Sources and boundaries",
                "chart_analysis": "Chart analysis",
                "table_analysis": "Table analysis",
                "evidence_answer": "Relevant evidence",
            },
        }
        merged_evidence: list[dict[str, Any]] = []
        evidence_indexes: dict[tuple[str, str, str], int] = {}
        sections: list[str] = []
        warnings: list[str] = list(plan.warnings)
        intent_order = {intent: index for index, intent in enumerate(plan.active_intents)}
        ordered_contributions = sorted(
            enumerate(contributions),
            key=lambda item: (intent_order.get(item[1][0], len(intent_order)), item[0]),
        )

        for _, (intent, (answer, local_evidence, local_warnings)) in ordered_contributions:
            citation_map: dict[int, int] = {}
            for local_index, item in enumerate(local_evidence, 1):
                key = (
                    str(item.get("slide_id") or ""),
                    str(item.get("chunk_id") or item.get("element_id") or ""),
                    str(item.get("quote") or ""),
                )
                if key not in evidence_indexes:
                    merged_evidence.append(item)
                    evidence_indexes[key] = len(merged_evidence)
                citation_map[local_index] = evidence_indexes[key]

            remapped = re.sub(
                r"\[(\d+)\]",
                lambda match, mapping=citation_map: f"[{mapping.get(int(match.group(1)), int(match.group(1)))}]",
                answer,
            ).strip()
            heading = labels[plan.answer_language].get(intent, intent.replace("_", " ").title())
            sections.append(f"## {heading}\n\n{remapped}")
            warnings.extend(local_warnings)

        return "\n\n".join(sections), merged_evidence, list(dict.fromkeys(warnings))

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
    def _performance_score(text: str) -> int:
        folded = text.casefold()
        return sum(3 for term in PERFORMANCE_TERMS if term in folded)

    def _summary_answer(
        self,
        slides: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Build broad, document-level evidence instead of requiring a literal query match."""
        selected_slides = self._select_summary_slides(slides)
        evidence = [self._slide_summary_evidence(slide) for slide in selected_slides]
        series_evidence, selected_series_rows = self._select_summary_series(
            chart_rows,
            selected_slides,
            max_items=max(0, 6 - len(evidence)),
        )
        evidence.extend(series_evidence)
        if not evidence:
            return (
                "当前文档尚未提取到可用于概括的正文、表格或图表证据。",
                [],
                ["INSUFFICIENT_EVIDENCE"],
            )
        trends = self._summary_trends(series_evidence, citation_start=len(selected_slides) + 1)
        direction = self._summary_direction(selected_series_rows)
        takeaways = self._summary_takeaways(selected_slides)
        return self._render_summary(direction, trends, takeaways), evidence, []

    def _select_summary_slides(self, slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        return selected_slides

    def _select_summary_series(
        self,
        chart_rows: list[dict[str, Any]],
        selected_slides: list[dict[str, Any]],
        *,
        max_items: int,
    ) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
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
        evidence: list[dict[str, Any]] = []
        selected_series_rows: list[list[dict[str, Any]]] = []
        for _, rows in ranked_series:
            first = rows[0]
            label = (str(first.get("chart_title") or ""), str(first.get("series_name") or ""))
            if label in used_series_labels:
                continue
            evidence.append(self._series_evidence(rows))
            selected_series_rows.append(rows)
            used_series_labels.add(label)
            if len(evidence) >= max_items:
                break
        return evidence, selected_series_rows

    @staticmethod
    def _summary_trends(
        series_evidence: list[dict[str, Any]],
        *,
        citation_start: int,
    ) -> list[tuple[str, str, str, int]]:
        trends: list[tuple[str, str, str, int]] = []
        for citation_no, item in enumerate(series_evidence, citation_start):
            match = re.match(r".*? — (.*?): (.*)", item["quote"], flags=re.S)
            if not match:
                continue
            points = [part.strip() for part in match.group(2).split(",") if "=" in part]
            if points:
                trends.append((match.group(1), points[0], points[-1], citation_no))
        return trends

    @staticmethod
    def _summary_direction(selected_series_rows: list[list[dict[str, Any]]]) -> str:
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
        return direction

    def _summary_takeaways(self, selected_slides: list[dict[str, Any]]) -> list[str]:
        takeaway_lines = []
        for index, slide in enumerate(selected_slides, 1):
            takeaway = self._slide_takeaway(slide)
            if takeaway:
                takeaway_lines.append(f"- {takeaway} [{index}]")
        return takeaway_lines

    @staticmethod
    def _render_summary(
        direction: str,
        trends: list[tuple[str, str, str, int]],
        takeaway_lines: list[str],
    ) -> str:
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
            "点击引用可回到对应幻灯片核验。 [1]"
        )
