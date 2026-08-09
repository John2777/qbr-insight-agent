from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .evidence import EvidencePack, classify_content_role
from .query_planning import QueryPlan

SignalCategory = Literal["explicit", "deterioration", "threshold", "watchpoint"]

POSITIVE_DIRECTION_METRICS = (
    "anp",
    "capital ratio",
    "cash",
    "digital stp",
    "ev equity",
    "fsg",
    "growth",
    "margin",
    "opat",
    "persistency",
    "productivity",
    "profit",
    "revenue",
    "roe",
    "roev",
    "stp",
    "ufsg",
    "vonb",
    "产能",
    "价值",
    "保费",
    "利润",
    "增长",
    "收入",
    "现金",
    "继续率",
    "资本比率",
    "直通率",
)

NEGATIVE_DIRECTION_METRICS = (
    "capital intensity",
    "churn",
    "claim ratio",
    "complaint",
    "cost",
    "expense",
    "lapse",
    "loss",
    "risk utilization",
    "投诉",
    "成本",
    "流失",
    "赔付率",
    "资本强度",
    "风险限额利用",
)

MANAGEMENT_WATCH_PATTERNS = (
    r"\b(?:control|reduce|repair|remediate|mitigate|optimi[sz]e)\b.{0,30}\b(?:risk|concentration|cost|intensity|base|mix)\b",
    r"修复.{0,20}(?:基数|业务|市场|渠道)",
    r"控制.{0,20}(?:集中|风险|成本|强度)",
    r"降低.{0,20}(?:资本强度|成本|风险|流失)",
    r"优化.{0,20}(?:渠道组合|产品节奏|成本|结构)",
)

EXPLICIT_ADVERSE_MARKERS = (
    "above limit",
    "below target",
    "breach",
    "deteriorat",
    "red warning",
    "underperform",
    "低于目标",
    "恶化",
    "未达",
    "超限",
    "红色预警",
    "承压",
)

SYNTHETIC_MARKERS = ("synthetic", "illustrative", "test data", "模拟", "测试数据", "测试用估算")


def _number(value: Any) -> float | None:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(value).replace("−", "-"))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return f"{int(round(value)):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _metric_direction(metric: str, context: str = "") -> int:
    folded = f"{metric} {context}".casefold()
    if any(marker in folded for marker in NEGATIVE_DIRECTION_METRICS):
        return -1
    if any(marker in folded for marker in POSITIVE_DIRECTION_METRICS):
        return 1
    return 0


def _is_rate(metric: str, previous: str, current: str) -> bool:
    folded = metric.casefold()
    return (
        "%" in previous
        or "%" in current
        or any(marker in folded for marker in ("margin", "rate", "ratio", "roev", "roe", "persistency", "stp", "率", "比率"))
    )


def _material_adverse_change(
    metric: str,
    previous_value: float,
    current_value: float,
    previous_raw: str,
    current_raw: str,
    *,
    context: str = "",
) -> tuple[bool, float, str]:
    direction = _metric_direction(metric, context)
    if direction == 0 or math.isclose(previous_value, current_value, abs_tol=1e-12):
        return False, 0.0, ""
    adverse = current_value < previous_value if direction > 0 else current_value > previous_value
    if not adverse:
        return False, 0.0, ""
    difference = current_value - previous_value
    if _is_rate(metric, previous_raw, current_raw):
        magnitude = abs(difference)
        return magnitude >= 0.5, magnitude, "percentage_points"
    relative = abs(difference) / max(abs(previous_value), 1e-9) * 100
    return relative >= 1.0, relative, "percent"


def _period_key(value: str) -> tuple[int, int] | None:
    folded = value.casefold()
    year = re.search(r"20\d{2}", folded)
    if year is None:
        month = re.fullmatch(r"(\d{2})/(\d{2})", folded.strip())
        return (2000 + int(month.group(1)), int(month.group(2))) if month else None
    quarter = re.search(r"q([1-4])", folded)
    month = re.search(r"(?:^|[^\d])(0?[1-9]|1[0-2])(?:[^\d]|$)", folded[year.end() :])
    return (int(year.group(0)), int(quarter.group(1)) * 3 if quarter else int(month.group(1)) if month else 12)


def _chunk_evidence(row: dict[str, Any], quote: str, facet: str, method: str) -> dict[str, Any]:
    return {
        "document_version_id": row["document_version_id"],
        "slide_id": row["slide_id"],
        "element_id": row.get("element_id"),
        "chunk_id": row.get("id"),
        "quote": quote,
        "bbox": {},
        "confidence": float(row.get("confidence") or 1.0),
        "source_kind": row.get("source_kind") or "native_ooxml",
        "document_title": row.get("document_title"),
        "slide_no": row.get("slide_no"),
        "slide_title": row.get("slide_title"),
        "content_role": row.get("content_role") or classify_content_role(row),
        "facet": facet,
        "analysis_method": method,
    }


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
        "facet": "deteriorating_metrics",
        "analysis_method": "structured_chart_trend",
        "synthetic": synthetic,
    }


@dataclass(frozen=True, slots=True)
class NegativeSignal:
    category: SignalCategory
    claim_zh: str
    claim_en: str
    evidence: dict[str, Any]
    score: float
    synthetic: bool = False

    def claim(self, language: str) -> str:
        return self.claim_zh if language == "zh" else self.claim_en


@dataclass(frozen=True, slots=True)
class NegativeAssessment:
    signals: tuple[NegativeSignal, ...]
    status_evidence: tuple[dict[str, Any], ...]
    explicit_red_flags: bool
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
            if not self.status_evidence and self.analyzed_sources == 0:
                answer = (
                    "当前文档范围内没有足够的相关业务证据回答这个问题。"
                    if language == "zh"
                    else "The current document does not contain enough relevant business evidence to answer this question."
                )
                return answer, [], ["INSUFFICIENT_EVIDENCE"]
            if not self.status_evidence:
                answer = (
                    "已分析的业务内容中没有发现明确的负面陈述、实质性恶化趋势或阈值突破。"
                    "这不等于证明没有下行风险；只能说明当前文档没有提供足以支持具体“坏消息”结论的证据。"
                    if language == "zh"
                    else "No explicit negative statement, material deterioration, or threshold breach was found in the "
                    "business content analyzed. This does not prove that no downside exists; it means the presentation "
                    "does not support a specific bad-news claim."
                )
                return answer, [], []
            references = [citation(item) for item in self.status_evidence[:2]]
            suffix = "" if not references else " " + "".join(f"[{index}]" for index in references)
            answer = (
                "文档中没有发现明确的负面结果或突破阈值的红灯事项；现有指标主要处于绿色/达标状态。因此不应为了回答“坏消息”而制造负面结论。"
                if language == "zh"
                else "No explicit negative result or breached threshold was found in the presentation. "
                "The available indicators are predominantly green or on target, so it would be misleading to manufacture bad news."
            )
            return answer + suffix, evidence, []

        if self.explicit_red_flags:
            intro = "文档中最明确的负面信号是：" if language == "zh" else "The clearest negative signals in the presentation are:"
        else:
            intro = (
                "文档中没有发现突破阈值的红灯事项，但存在以下基于数据和管理动作的关注点："
                if language == "zh"
                else "No red/amber threshold breach was found, but the evidence supports the following watchpoints:"
            )
        labels = {
            "explicit": ("明确负面信息", "Explicit negative evidence"),
            "deterioration": ("恶化趋势", "Deteriorating metrics"),
            "threshold": ("阈值事项", "Threshold issues"),
            "watchpoint": ("管理关注点", "Management watchpoints"),
        }
        sections: list[str] = [intro]
        for category in ("explicit", "deterioration", "threshold", "watchpoint"):
            selected = [signal for signal in self.signals if signal.category == category]
            if not selected:
                continue
            label = labels[category][0 if language == "zh" else 1]
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in selected]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))
        warnings = ["SYNTHETIC_DATA_SIGNAL"] if any(signal.synthetic for signal in self.signals) else []
        return "\n\n".join(sections), evidence, warnings


class NegativeSignalAnalyzer:
    """Derive adverse QBR signals from explicit text, tables, charts, gates and management actions."""

    def analyze(
        self,
        plan: QueryPlan,
        *,
        explicit_pack: EvidencePack,
        chunks: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
        max_signals: int = 6,
    ) -> NegativeAssessment:
        del plan
        synthetic_slides = {
            str(row.get("slide_id"))
            for row in chunks
            if str(row.get("chunk_type") or "").casefold() == "notes"
            and any(marker in str(row.get("content") or "").casefold() for marker in SYNTHETIC_MARKERS)
        }
        signals: list[NegativeSignal] = []
        status_evidence: list[dict[str, Any]] = []
        explicit_red_flags = False
        counts = {
            "retrieved_atoms": 0,
            "table_trend_candidates": 0,
            "chart_trend_candidates": 0,
            "watchpoint_candidates": 0,
            "green_gates": 0,
        }

        for atom in explicit_pack.atoms:
            quote = atom.quote
            folded = quote.casefold()
            category: SignalCategory = "watchpoint"
            if atom.facet == "deteriorating_metrics":
                category = "deterioration"
            elif atom.facet == "threshold_pressure" and any(marker in folded for marker in EXPLICIT_ADVERSE_MARKERS):
                category = "threshold"
            elif atom.facet == "explicit_negative_statements" and any(marker in folded for marker in EXPLICIT_ADVERSE_MARKERS):
                category = "explicit"
            if category in {"explicit", "threshold"}:
                explicit_red_flags = True
            synthetic = str(atom.source.get("slide_id")) in synthetic_slides
            evidence = atom.to_evidence()
            evidence["analysis_method"] = "explicit_semantic_evidence"
            evidence["synthetic"] = synthetic
            signals.append(
                NegativeSignal(
                    category,
                    quote,
                    quote,
                    evidence,
                    atom.relevance_score + (3.0 if category in {"explicit", "threshold"} else 0.0),
                    synthetic,
                )
            )
            counts["retrieved_atoms"] += 1

        table_chunks = [row for row in chunks if str(row.get("chunk_type") or "").casefold() == "table"]
        for row in table_chunks:
            table_signals, green_evidence = self._table_signals(row, str(row.get("slide_id")) in synthetic_slides)
            signals.extend(table_signals)
            status_evidence.extend(green_evidence)
            counts["table_trend_candidates"] += sum(signal.category == "deterioration" for signal in table_signals)
            counts["green_gates"] += len(green_evidence)

        chart_signals = self._chart_signals(chart_rows, synthetic_slides)
        signals.extend(chart_signals)
        counts["chart_trend_candidates"] = len(chart_signals)

        action_signals = self._management_watchpoints(chunks, synthetic_slides)
        signals.extend(action_signals)
        counts["watchpoint_candidates"] = len(action_signals)

        deduped: list[NegativeSignal] = []
        seen_claims: set[tuple[str, str]] = set()
        seen_sources: set[tuple[str, str]] = set()
        category_counts: dict[SignalCategory, int] = {
            "explicit": 0,
            "deterioration": 0,
            "threshold": 0,
            "watchpoint": 0,
        }
        category_limits: dict[SignalCategory, int] = {
            "explicit": 2,
            "deterioration": 3,
            "threshold": 2,
            "watchpoint": 2,
        }

        def signal_rank(item: NegativeSignal) -> tuple[float, int, int]:
            return (item.score, int(not item.synthetic), -int(item.evidence.get("slide_no") or 0))

        for signal in sorted(signals, key=signal_rank, reverse=True):
            key = (signal.category, re.sub(r"\s+", " ", signal.claim_zh).casefold())
            source_identity = str(signal.evidence.get("chunk_id") or signal.evidence.get("element_id") or "")
            if signal.evidence.get("analysis_method") == "structured_chart_trend":
                source_identity = f"{source_identity}:{signal.evidence.get('quote') or ''}"
            source_key = (
                str(signal.evidence.get("slide_id") or ""),
                source_identity,
            )
            if (
                key in seen_claims
                or (source_key[1] and source_key in seen_sources)
                or category_counts[signal.category] >= category_limits[signal.category]
            ):
                continue
            seen_claims.add(key)
            seen_sources.add(source_key)
            deduped.append(signal)
            category_counts[signal.category] += 1
            if len(deduped) >= max_signals:
                break

        eligible_chunks = {
            str(row.get("id") or f"{row.get('slide_id')}:{row.get('element_id')}:{index}")
            for index, row in enumerate(chunks)
            if str(row.get("chunk_type") or "").casefold() in {"text", "table"}
            and classify_content_role(row) not in {"boilerplate", "methodology", "provenance"}
        }
        analyzed_sources = len(eligible_chunks) + len({str(row.get("series_id")) for row in chart_rows})
        return NegativeAssessment(
            tuple(deduped),
            tuple(status_evidence[:2]),
            explicit_red_flags,
            analyzed_sources,
            {
                **counts,
                "selected_categories": category_counts,
                "synthetic_slide_count": len(synthetic_slides),
                "signal_count": len(deduped),
                "analyzed_sources": analyzed_sources,
            },
        )

    def _table_signals(
        self,
        source: dict[str, Any],
        synthetic: bool,
    ) -> tuple[list[NegativeSignal], list[dict[str, Any]]]:
        lines = [line.strip() for line in str(source.get("content") or "").splitlines() if line.strip()]
        cells = [[cell.strip() for cell in line.split("|")] for line in lines]
        if len(cells) < 2:
            return [], []
        headers = cells[0]
        width = len(headers)
        rows = [(row + [""] * width)[:width] for row in cells[1:]]
        folded_headers = [header.casefold() for header in headers]
        green_index = next((i for i, header in enumerate(folded_headers) if header in {"green", "绿"}), None)
        current_index = next((i for i, header in enumerate(folded_headers) if header in {"current", "actual", "当前", "实际"}), None)
        if green_index is not None and current_index is not None:
            outcomes: list[bool] = []
            for row in rows:
                current, threshold = _number(row[current_index]), _number(row[green_index])
                if current is None or threshold is None:
                    continue
                condition = row[green_index]
                if ">" in condition or "≥" in condition:
                    outcomes.append(current >= threshold if ">=" in condition or "≥" in condition else current > threshold)
                elif "<" in condition or "≤" in condition:
                    outcomes.append(current <= threshold if "<=" in condition or "≤" in condition else current < threshold)
            if outcomes and all(outcomes):
                evidence = _chunk_evidence(source, "\n".join(lines), "threshold_status", "traffic_light_gate")
                evidence["synthetic"] = synthetic
                return [], [evidence]

        period_columns = [(index, key) for index, header in enumerate(headers[1:], 1) if (key := _period_key(header))]
        if len(period_columns) < 2:
            return [], []
        period_columns.sort(key=lambda item: item[1])
        previous_index, current_index = period_columns[-2][0], period_columns[-1][0]
        signals: list[NegativeSignal] = []
        for row in rows:
            if not row or any(marker in row[0] for marker in ("合计", "总计")):
                continue
            previous_value, current_value = _number(row[previous_index]), _number(row[current_index])
            if previous_value is None or current_value is None:
                continue
            material, magnitude, unit = _material_adverse_change(
                row[0], previous_value, current_value, row[previous_index], row[current_index]
            )
            if not material:
                continue
            change_zh = f"下降{_format_number(magnitude)}个百分点" if unit == "percentage_points" else f"下降约{magnitude:.1f}%"
            change_en = (
                f"down {_format_number(magnitude)} percentage points"
                if unit == "percentage_points"
                else f"down approximately {magnitude:.1f}%"
            )
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            claim_zh = (
                f"{row[0]}从{headers[previous_index]}的{row[previous_index]}降至"
                f"{headers[current_index]}的{row[current_index]}，{change_zh}{suffix_zh}。"
            )
            claim_en = (
                f"{row[0]} declined from {headers[previous_index]} {row[previous_index]} to "
                f"{headers[current_index]} {row[current_index]} ({change_en}){suffix_en}."
            )
            evidence = _chunk_evidence(
                source,
                " | ".join(headers) + "\n" + " | ".join(row),
                "deteriorating_metrics",
                "structured_table_trend",
            )
            evidence["synthetic"] = synthetic
            signals.append(NegativeSignal("deterioration", claim_zh, claim_en, evidence, 7.0 + magnitude / 10, synthetic))
        return signals, []

    def _chart_signals(self, rows: list[dict[str, Any]], synthetic_slides: set[str]) -> list[NegativeSignal]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id") or ""), []).append(row)
        signals: list[NegativeSignal] = []
        for series_rows in groups.values():
            ordered = sorted(series_rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 2:
                continue
            previous, current = ordered[-2], ordered[-1]
            previous_value, current_value = float(previous["y_value"]), float(current["y_value"])
            metric = str(current.get("series_name") or "")
            context = " ".join(str(current.get(key) or "") for key in ("chart_title", "slide_title", "slide_summary", "document_title"))
            previous_raw = str(previous.get("display_value") or previous_value)
            current_raw = str(current.get("display_value") or current_value)
            material, magnitude, unit = _material_adverse_change(
                metric, previous_value, current_value, previous_raw, current_raw, context=context
            )
            if not material:
                continue
            synthetic = str(current.get("slide_id")) in synthetic_slides
            suffix_zh = "（文档标注为模拟数据）" if synthetic else ""
            suffix_en = " (document-labelled synthetic data)" if synthetic else ""
            change_zh = f"下降{_format_number(magnitude)}个百分点" if unit == "percentage_points" else f"下降约{magnitude:.1f}%"
            change_en = (
                f"down {_format_number(magnitude)} percentage points"
                if unit == "percentage_points"
                else f"down approximately {magnitude:.1f}%"
            )
            claim_zh = (
                f"{metric}从{previous.get('category')}的{previous_raw}降至{current.get('category')}的{current_raw}，"
                f"{change_zh}{suffix_zh}。"
            )
            claim_en = (
                f"{metric} declined from {previous.get('category')} {previous_raw} to {current.get('category')} "
                f"{current_raw} ({change_en}){suffix_en}."
            )
            signals.append(
                NegativeSignal(
                    "deterioration",
                    claim_zh,
                    claim_en,
                    _chart_evidence(previous, current, synthetic),
                    6.0
                    + magnitude / 10
                    + (
                        1.0
                        if any(
                            marker in f"{metric} {context}".casefold()
                            for marker in ("capital ratio", "margin", "persistency", "stp", "继续率", "资本比率", "直通率")
                        )
                        else 0.0
                    ),
                    synthetic,
                )
            )
        return signals

    def _management_watchpoints(
        self,
        chunks: list[dict[str, Any]],
        synthetic_slides: set[str],
    ) -> list[NegativeSignal]:
        signals: list[NegativeSignal] = []
        for row in chunks:
            if str(row.get("chunk_type") or "").casefold() != "text":
                continue
            role = classify_content_role(row)
            if role in {"boilerplate", "methodology", "provenance"}:
                continue
            content = str(row.get("content") or "").strip()
            if not content or not any(re.search(pattern, content, flags=re.I) for pattern in MANAGEMENT_WATCH_PATTERNS):
                continue
            synthetic = str(row.get("slide_id")) in synthetic_slides
            suffix_zh = "（文档标注为模拟管理内容）" if synthetic else ""
            suffix_en = " (document-labelled synthetic management content)" if synthetic else ""
            evidence = _chunk_evidence(row, content, "management_concerns", "management_action_inference")
            evidence["synthetic"] = synthetic
            folded = content.casefold()
            action_priority = 1.0 if any(marker in folded for marker in ("concentration", "capital intensity", "集中", "资本强度")) else 0.0
            signals.append(
                NegativeSignal(
                    "watchpoint",
                    f"管理动作“{content}”表明该事项需要持续关注{suffix_zh}。",
                    f"The management action “{content}” identifies an area requiring continued attention{suffix_en}.",
                    evidence,
                    5.0 + action_priority + (0.25 if len(content) > 12 else 0.0),
                    synthetic,
                )
            )
        return signals
