from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .evidence import EvidencePack, classify_content_role
from .negative_analysis import (
    SYNTHETIC_MARKERS,
    NegativeSignal,
    NegativeSignalAnalyzer,
    _chunk_evidence,
    _format_number,
    _is_rate,
    _metric_direction,
    _number,
    _period_key,
)
from .query_planning import QueryPlan

EvaluationFacet = Literal[
    "growth_momentum",
    "profitability_value",
    "cash_capital",
    "operating_quality",
    "portfolio_resilience",
    "execution_delivery",
]

FACET_ORDER: tuple[EvaluationFacet, ...] = (
    "growth_momentum",
    "profitability_value",
    "cash_capital",
    "operating_quality",
    "portfolio_resilience",
    "execution_delivery",
)

FACET_LABELS: dict[EvaluationFacet, tuple[str, str]] = {
    "growth_momentum": ("增长与动量", "Growth and momentum"),
    "profitability_value": ("盈利与价值创造", "Profitability and value creation"),
    "cash_capital": ("现金与资本", "Cash and capital"),
    "operating_quality": ("运营质量", "Operating quality"),
    "portfolio_resilience": ("组合与韧性", "Portfolio resilience"),
    "execution_delivery": ("执行与目标达成", "Execution and target delivery"),
}

POSITIVE_PREDICATE_MARKERS = (
    "above target",
    "grew",
    "growth",
    "improv",
    "leading",
    "momentum",
    "outperform",
    "record",
    "strong",
    "增长",
    "提升",
    "改善",
    "领先",
    "动量",
    "新高",
    "创纪录",
    "强劲",
    "达标",
    "多元",
    "双位数",
    "延续",
)


def _facet(text: str) -> EvaluationFacet:
    folded = text.casefold()
    if any(marker in folded for marker in ("growth", "growth rate", "momentum", "record", "增长", "增速", "动量", "新高", "创纪录")):
        return "growth_momentum"
    if any(marker in folded for marker in ("profit", "earnings", "margin", "roe", "roev", "value", "vonb", "利润", "盈利", "回报", "价值")):
        return "profitability_value"
    if any(marker in folded for marker in ("cash", "capital", "fsg", "solvency", "现金", "资本", "自由盈余", "偿付")):
        return "cash_capital"
    if any(
        marker in folded
        for marker in ("persistency", "productivity", "retention", "stp", "quality", "继续率", "产能", "留存", "直通率", "质量")
    ):
        return "operating_quality"
    if any(
        marker in folded
        for marker in (
            "mix",
            "concentration",
            "diversif",
            "portfolio",
            "regional",
            "market",
            "share",
            "组合",
            "集中",
            "多元",
            "区域",
            "市场",
            "占比",
        )
    ):
        return "portfolio_resilience"
    return "execution_delivery"


def _positive_change(
    metric: str,
    previous_value: float,
    current_value: float,
    previous_raw: str,
    current_raw: str,
    *,
    context: str = "",
) -> tuple[bool, float, str, str]:
    direction = _metric_direction(metric, context)
    if direction == 0 or math.isclose(previous_value, current_value, abs_tol=1e-12):
        return False, 0.0, "", ""
    favorable = current_value > previous_value if direction > 0 else current_value < previous_value
    if not favorable:
        return False, 0.0, "", ""
    difference = current_value - previous_value
    movement = "increase" if difference > 0 else "decrease"
    if _is_rate(metric, previous_raw, current_raw):
        magnitude = abs(difference)
        return magnitude >= 0.5, magnitude, "percentage_points", movement
    relative = abs(difference) / max(abs(previous_value), 1e-9) * 100
    return relative >= 1.0, relative, "percent", movement


def _comparison_matches(value: float, expression: str) -> bool | None:
    match = re.search(r"(>=|<=|>|<|≥|≤)\s*[-+]?\s*(\d+(?:\.\d+)?)", expression.replace(",", ""))
    if match is None:
        return None
    operator, raw_threshold = match.groups()
    threshold = float(raw_threshold)
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "≥": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "≤": value <= threshold,
    }[operator]


def _chart_evidence(previous: dict[str, Any], current: dict[str, Any], synthetic: bool) -> dict[str, Any]:
    quote = (
        f"{current.get('chart_title') or 'Chart'} — {current.get('series_name')}: "
        f"{previous.get('category')}={previous.get('display_value') or previous.get('y_value')}; "
        f"{current.get('category')}={current.get('display_value') or current.get('y_value')}"
    )
    confidence = min(
        float(current.get("chart_confidence") or 1.0),
        float(current.get("series_confidence") or 1.0),
        float(current.get("confidence") or 1.0),
    )
    return {
        "document_version_id": current["document_version_id"],
        "slide_id": current["slide_id"],
        "element_id": current.get("element_id"),
        "chunk_id": None,
        "quote": quote,
        "bbox": {},
        "confidence": confidence,
        "source_kind": current.get("source_kind") or "chart_cache_or_literal",
        "document_title": current.get("document_title"),
        "slide_no": current.get("slide_no"),
        "slide_title": current.get("slide_title"),
        "content_role": "chart",
        "facet": _facet(str(current.get("series_name") or "")),
        "analysis_method": "structured_chart_improvement",
        "synthetic": synthetic,
    }


@dataclass(frozen=True, slots=True)
class EvaluationSignal:
    facet: EvaluationFacet
    claim_zh: str
    claim_en: str
    evidence: dict[str, Any]
    score: float
    synthetic: bool = False

    def claim(self, language: str) -> str:
        return self.claim_zh if language == "zh" else self.claim_en


@dataclass(frozen=True, slots=True)
class EvaluationAssessment:
    signals: tuple[EvaluationSignal, ...]
    caveats: tuple[NegativeSignal, ...]
    analyzed_sources: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def render(self, plan: QueryPlan) -> tuple[str, list[dict[str, Any]], list[str]]:
        language = plan.answer_language
        evidence: list[dict[str, Any]] = []
        evidence_index: dict[tuple[str, str, str], int] = {}

        def citation(item: dict[str, Any]) -> int:
            key = (
                str(item.get("slide_id") or ""),
                str(item.get("chunk_id") or item.get("element_id") or ""),
                str(item.get("quote") or ""),
            )
            if key not in evidence_index:
                evidence.append(item)
                evidence_index[key] = len(evidence)
            return evidence_index[key]

        if not self.signals:
            if self.caveats and plan.evaluation_polarity == "balanced":
                intro = (
                    "文档中没有找到具备比较依据的优势证据，但发现以下需要关注的事项："
                    if language == "zh"
                    else "No strength was supported by comparative evidence, but the following concerns were identified:"
                )
                lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in self.caveats]
                warnings = ["SYNTHETIC_DATA_SIGNAL"] if any(signal.synthetic for signal in self.caveats) else []
                return intro + "\n\n" + "\n".join(lines), evidence, warnings
            if self.analyzed_sources == 0:
                answer = (
                    "当前文档范围内没有可用于评价该问题的业务内容。"
                    if language == "zh"
                    else "The current document scope contains no business content from which this question can be evaluated."
                )
                return answer, [], ["INSUFFICIENT_EVIDENCE"]
            if plan.evaluation_polarity == "opportunity":
                answer = (
                    "文档中存在业务内容，但没有找到由管理动作、未达目标或明确增长抓手支持的机会证据。"
                    "因此不能仅凭一般业务描述推断下一阶段机会。"
                    if language == "zh"
                    else "The document contains business content, but no opportunity is supported by a management action, "
                    "target gap, or explicit growth lever. General business descriptions do not justify an opportunity claim."
                )
                return answer, [], ["NO_COMPARATIVE_OPPORTUNITY_EVIDENCE"]
            answer = (
                "文档中存在业务内容，但没有找到具备趋势、目标、阈值或相对比较依据的优势证据。"
                "现有内容可以视为业务描述，不能据此断言为公司优势。"
                if language == "zh"
                else "The document contains business content, but no strength is supported by a trend, target, threshold, "
                "or relative comparison. The available material is descriptive and does not justify a company-strength claim."
            )
            return answer, [], ["NO_COMPARATIVE_STRENGTH_EVIDENCE"]

        if plan.evaluation_polarity == "opportunity":
            intro = (
                "根据文档中的管理动作，当前可执行的改进或增长机会主要包括："
                if language == "zh"
                else "Based on management actions in the presentation, the main actionable improvement or growth opportunities are:"
            )
        elif plan.evaluation_polarity == "balanced":
            intro = (
                "根据文档中具备比较依据的指标，公司的优势及需要平衡看待的方面如下："
                if language == "zh"
                else "Based on indicators with a documented comparison basis, the company's strengths and important qualifications are:"
            )
        else:
            intro = (
                "根据文档中具备比较依据的指标，公司的优势主要体现在以下方面："
                if language == "zh"
                else "Based on indicators with a documented comparison basis, the company's main strengths are:"
            )
        sections = [intro]
        for facet in FACET_ORDER:
            selected = [signal for signal in self.signals if signal.facet == facet]
            if not selected:
                continue
            label = FACET_LABELS[facet][0 if language == "zh" else 1]
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in selected]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))

        if self.caveats:
            label = "需要平衡看待" if language == "zh" else "Important qualifications"
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in self.caveats]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))

        synthetic = any(signal.synthetic for signal in self.signals) or any(signal.synthetic for signal in self.caveats)
        warnings = ["SYNTHETIC_DATA_SIGNAL"] if synthetic else []
        return "\n\n".join(sections), evidence, warnings


class EvaluativeSignalAnalyzer:
    """Evaluate broad business questions from text, tables and charts without relying on evaluative keywords."""

    def __init__(self) -> None:
        self.negative_analyzer = NegativeSignalAnalyzer()

    def analyze(
        self,
        plan: QueryPlan,
        *,
        explicit_pack: EvidencePack,
        chunks: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
        max_signals: int = 5,
    ) -> EvaluationAssessment:
        synthetic_slides = {
            str(row.get("slide_id"))
            for row in chunks
            if str(row.get("chunk_type") or "").casefold() == "notes"
            and any(marker in str(row.get("content") or "").casefold() for marker in SYNTHETIC_MARKERS)
        }
        if plan.evaluation_polarity == "opportunity":
            return self._opportunity_assessment(plan, chunks, chart_rows, synthetic_slides, max_signals)

        candidates = [
            *self._explicit_signals(explicit_pack, synthetic_slides),
            *self._table_signals(chunks, synthetic_slides),
            *self._chart_signals(chart_rows, synthetic_slides),
        ]
        selected = self._select(candidates, max_signals)
        selected_facets = {signal.facet for signal in selected}

        empty_pack = EvidencePack((), (), (), False)
        negative = self.negative_analyzer.analyze(
            plan,
            explicit_pack=empty_pack,
            chunks=chunks,
            chart_rows=chart_rows,
        )
        if plan.evaluation_polarity == "balanced":
            caveats = tuple(negative.signals[:3])
        else:
            caveats = tuple(
                signal
                for signal in negative.signals
                if signal.category in {"deterioration", "threshold"} and _facet(f"{signal.claim_zh} {signal.claim_en}") in selected_facets
            )[:2]
        analyzed_sources = self._analyzed_sources(chunks, chart_rows)
        method_counts: dict[str, int] = {}
        for signal in candidates:
            method = str(signal.evidence.get("analysis_method") or "unknown")
            method_counts[method] = method_counts.get(method, 0) + 1
        return EvaluationAssessment(
            tuple(selected),
            caveats,
            analyzed_sources,
            {
                "polarity": plan.evaluation_polarity,
                "candidate_count": len(candidates),
                "signal_count": len(selected),
                "caveat_count": len(caveats),
                "selected_facets": list(dict.fromkeys(signal.facet for signal in selected)),
                "candidate_methods": method_counts,
                "synthetic_slide_count": len(synthetic_slides),
                "analyzed_sources": analyzed_sources,
            },
        )

    @staticmethod
    def _analyzed_sources(chunks: list[dict[str, Any]], chart_rows: list[dict[str, Any]]) -> int:
        eligible_chunks = {
            str(row.get("id") or f"{row.get('slide_id')}:{row.get('element_id')}:{index}")
            for index, row in enumerate(chunks)
            if str(row.get("chunk_type") or "").casefold() in {"text", "table"}
            and classify_content_role(row) not in {"boilerplate", "methodology", "provenance"}
        }
        return len(eligible_chunks) + len({str(row.get("series_id")) for row in chart_rows})

    def _explicit_signals(
        self,
        pack: EvidencePack,
        synthetic_slides: set[str],
    ) -> list[EvaluationSignal]:
        signals: list[EvaluationSignal] = []
        for atom in pack.atoms:
            quote = atom.quote.strip()
            folded = quote.casefold()
            if (
                not quote
                or atom.content_role in {"table", "chart"}
                or len(quote) > 800
                or not any(marker in folded for marker in POSITIVE_PREDICATE_MARKERS)
            ):
                continue
            synthetic = str(atom.source.get("slide_id")) in synthetic_slides
            evidence = atom.to_evidence()
            evidence["analysis_method"] = "explicit_positive_evidence"
            evidence["synthetic"] = synthetic
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            signals.append(
                EvaluationSignal(
                    _facet(quote),
                    f"文档明确指出：{quote}{suffix_zh}",
                    f"The presentation explicitly states: {quote}{suffix_en}",
                    evidence,
                    6.0 + atom.relevance_score,
                    synthetic,
                )
            )
        return signals

    def _table_signals(
        self,
        chunks: list[dict[str, Any]],
        synthetic_slides: set[str],
    ) -> list[EvaluationSignal]:
        signals: list[EvaluationSignal] = []
        for source in chunks:
            if str(source.get("chunk_type") or "").casefold() != "table":
                continue
            lines = [line.strip() for line in str(source.get("content") or "").splitlines() if line.strip()]
            cells = [[cell.strip() for cell in line.split("|")] for line in lines]
            if len(cells) < 2:
                continue
            headers = cells[0]
            width = len(headers)
            rows = [(row + [""] * width)[:width] for row in cells[1:]]
            synthetic = str(source.get("slide_id")) in synthetic_slides
            signals.extend(self._threshold_signals(source, headers, rows, synthetic))
            signals.extend(self._trend_signals(source, headers, rows, synthetic))
        return signals

    def _threshold_signals(
        self,
        source: dict[str, Any],
        headers: list[str],
        rows: list[list[str]],
        synthetic: bool,
    ) -> list[EvaluationSignal]:
        folded_headers = [header.casefold() for header in headers]

        def column(markers: tuple[str, ...]) -> int | None:
            return next((index for index, header in enumerate(folded_headers) if any(marker in header for marker in markers)), None)

        current_index = column(("current", "actual", "当前", "实际"))
        threshold_index = column(("green", "threshold", "target", "绿", "阈值", "目标"))
        if current_index is None or threshold_index is None:
            return []
        signals: list[EvaluationSignal] = []
        for row in rows:
            if max(current_index, threshold_index) >= len(row):
                continue
            current = _number(row[current_index])
            if current is None or _comparison_matches(current, row[threshold_index]) is not True:
                continue
            metric = row[0]
            facet = _facet(metric)
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            claim_zh = f"{metric}当前为{row[current_index]}，满足有利阈值{row[threshold_index]}，说明该指标处于目标或绿色区间{suffix_zh}。"
            claim_en = (
                f"{metric} is {row[current_index]}, meeting the favorable threshold {row[threshold_index]} and therefore "
                f"sitting in the target or green range{suffix_en}."
            )
            evidence = _chunk_evidence(
                source,
                " | ".join(headers) + "\n" + " | ".join(row),
                facet,
                "structured_threshold_strength",
            )
            evidence["synthetic"] = synthetic
            signals.append(EvaluationSignal(facet, claim_zh, claim_en, evidence, 7.0, synthetic))
        return signals

    def _trend_signals(
        self,
        source: dict[str, Any],
        headers: list[str],
        rows: list[list[str]],
        synthetic: bool,
    ) -> list[EvaluationSignal]:
        period_columns = [(index, key) for index, header in enumerate(headers[1:], 1) if (key := _period_key(header))]
        if len(period_columns) < 2:
            return []
        period_columns.sort(key=lambda item: item[1])
        previous_index, current_index = period_columns[-2][0], period_columns[-1][0]
        change_index = next(
            (
                index
                for index, header in enumerate(headers)
                if any(marker in header.casefold() for marker in ("change", "yoy", "同比", "变化"))
            ),
            None,
        )
        signals: list[EvaluationSignal] = []
        for row in rows:
            if not row or any(marker in row[0] for marker in ("合计", "总计")):
                continue
            previous_value, current_value = _number(row[previous_index]), _number(row[current_index])
            if previous_value is None or current_value is None:
                continue
            material, magnitude, unit, movement = _positive_change(
                row[0], previous_value, current_value, row[previous_index], row[current_index]
            )
            if not material:
                continue
            reported = row[change_index] if change_index is not None and change_index < len(row) else ""
            reported_zh = f"；文档报告变化为{reported}" if reported else ""
            reported_en = f"; the document reports a change of {reported}" if reported else ""
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            if movement == "increase":
                movement_zh = "上升"
                movement_en = "increased"
            else:
                movement_zh = "下降并改善"
                movement_en = "decreased favorably"
            claim_zh = (
                f"{row[0]}从{headers[previous_index]}的{row[previous_index]}{movement_zh}至"
                f"{headers[current_index]}的{row[current_index]}{reported_zh}{suffix_zh}。"
            )
            claim_en = (
                f"{row[0]} {movement_en} from {headers[previous_index]} {row[previous_index]} to "
                f"{headers[current_index]} {row[current_index]}{reported_en}{suffix_en}."
            )
            facet = _facet(row[0])
            evidence = _chunk_evidence(
                source,
                " | ".join(headers) + "\n" + " | ".join(row),
                facet,
                "structured_table_improvement",
            )
            evidence["synthetic"] = synthetic
            importance = 1.0 if facet in {"growth_momentum", "profitability_value", "cash_capital"} else 0.0
            signals.append(
                EvaluationSignal(
                    facet,
                    claim_zh,
                    claim_en,
                    evidence,
                    7.5 + importance + magnitude / (20 if unit == "percentage_points" else 50),
                    synthetic,
                )
            )
        return signals

    def _chart_signals(
        self,
        rows: list[dict[str, Any]],
        synthetic_slides: set[str],
    ) -> list[EvaluationSignal]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id") or ""), []).append(row)
        signals: list[EvaluationSignal] = []
        for series_rows in groups.values():
            ordered = sorted(series_rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 2:
                continue
            previous, current = ordered[-2], ordered[-1]
            if _period_key(str(previous.get("category") or "")) is None or _period_key(str(current.get("category") or "")) is None:
                continue
            previous_value, current_value = float(previous["y_value"]), float(current["y_value"])
            metric = str(current.get("series_name") or "")
            context = " ".join(str(current.get(key) or "") for key in ("chart_title", "slide_title", "slide_summary", "document_title"))
            previous_raw = str(previous.get("display_value") or previous_value)
            current_raw = str(current.get("display_value") or current_value)
            material, magnitude, unit, movement = _positive_change(
                metric,
                previous_value,
                current_value,
                previous_raw,
                current_raw,
                context=context,
            )
            if not material:
                continue
            synthetic = str(current.get("slide_id")) in synthetic_slides
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            change_zh = f"变化{_format_number(magnitude)}个百分点" if unit == "percentage_points" else f"改善约{magnitude:.1f}%"
            change_en = (
                f"a favorable change of {_format_number(magnitude)} percentage points"
                if unit == "percentage_points"
                else f"an improvement of approximately {magnitude:.1f}%"
            )
            movement_zh = "升至" if movement == "increase" else "降至"
            movement_en = "rose to" if movement == "increase" else "declined favorably to"
            claim_zh = (
                f"{metric}从{previous.get('category')}的{previous_raw}{movement_zh}"
                f"{current.get('category')}的{current_raw}，{change_zh}{suffix_zh}。"
            )
            claim_en = (
                f"{metric} {movement_en} {current.get('category')} {current_raw} from "
                f"{previous.get('category')} {previous_raw} ({change_en}){suffix_en}."
            )
            signals.append(
                EvaluationSignal(
                    _facet(f"{metric} {context}"),
                    claim_zh,
                    claim_en,
                    _chart_evidence(previous, current, synthetic),
                    5.0 + magnitude / 20,
                    synthetic,
                )
            )
        return signals

    @staticmethod
    def _select(candidates: list[EvaluationSignal], max_signals: int) -> list[EvaluationSignal]:
        selected: list[EvaluationSignal] = []
        seen_claims: set[str] = set()
        source_counts: dict[tuple[str, str], int] = {}
        facet_counts: dict[EvaluationFacet, int] = {facet: 0 for facet in FACET_ORDER}

        def rank(signal: EvaluationSignal) -> tuple[float, int, int]:
            return (
                signal.score - (1.0 if signal.synthetic else 0.0),
                int(not signal.synthetic),
                -int(signal.evidence.get("slide_no") or 0),
            )

        for signal in sorted(candidates, key=rank, reverse=True):
            normalized = re.sub(r"\s+", " ", signal.claim_zh).casefold()
            source_key = (
                str(signal.evidence.get("document_version_id") or ""),
                str(signal.evidence.get("slide_id") or ""),
            )
            if normalized in seen_claims or facet_counts[signal.facet] >= 1 or source_counts.get(source_key, 0) >= 2:
                continue
            seen_claims.add(normalized)
            facet_counts[signal.facet] += 1
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            selected.append(signal)
            if len(selected) >= max_signals:
                break
        return selected

    def _opportunity_assessment(
        self,
        plan: QueryPlan,
        chunks: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
        synthetic_slides: set[str],
        max_signals: int,
    ) -> EvaluationAssessment:
        empty_pack = EvidencePack((), (), (), False)
        negative = self.negative_analyzer.analyze(
            plan,
            explicit_pack=empty_pack,
            chunks=chunks,
            chart_rows=chart_rows,
            max_signals=max_signals,
        )
        signals: list[EvaluationSignal] = []
        for item in negative.signals:
            if item.category != "watchpoint":
                continue
            evidence = dict(item.evidence)
            evidence["analysis_method"] = "management_action_opportunity"
            action = str(evidence.get("quote") or item.claim_zh).strip()
            suffix_zh = "（文档标注为模拟管理内容）" if item.synthetic else ""
            suffix_en = " (document-labelled synthetic management content)" if item.synthetic else ""
            signals.append(
                EvaluationSignal(
                    _facet(f"{item.claim_zh} {item.claim_en}"),
                    f"管理动作“{action}”可视为下一阶段可执行的改进或增长抓手{suffix_zh}。",
                    f"The management action “{action}” is an actionable improvement or growth lever{suffix_en}.",
                    evidence,
                    item.score,
                    item.synthetic,
                )
            )
        analyzed_sources = self._analyzed_sources(chunks, chart_rows)
        selected = self._select(signals, max_signals)
        return EvaluationAssessment(
            tuple(selected),
            (),
            analyzed_sources,
            {
                "polarity": "opportunity",
                "candidate_count": len(signals),
                "signal_count": len(selected),
                "caveat_count": 0,
                "selected_facets": list(dict.fromkeys(signal.facet for signal in selected)),
                "synthetic_slide_count": len(synthetic_slides),
                "analyzed_sources": analyzed_sources,
            },
        )
