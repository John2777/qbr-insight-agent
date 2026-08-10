from __future__ import annotations

import math
import re
from typing import Any

from .evidence import EvidencePack, classify_content_role
from .negative_models import NegativeAssessment, NegativeSignal, SignalCategory
from .query_planning import QueryPlan

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
        synthetic_slides = self._synthetic_slides(chunks)
        signals, status_evidence, explicit_red_flags, counts = self._collect_signals(
            explicit_pack,
            chunks,
            chart_rows,
            synthetic_slides,
        )
        deduped, category_counts = self._select_signals(signals, max_signals)
        analyzed_sources = self._analyzed_sources(chunks, chart_rows)
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

    @staticmethod
    def _synthetic_slides(chunks: list[dict[str, Any]]) -> set[str]:
        return {
            str(row.get("slide_id"))
            for row in chunks
            if str(row.get("chunk_type") or "").casefold() == "notes"
            and any(marker in str(row.get("content") or "").casefold() for marker in SYNTHETIC_MARKERS)
        }

    def _collect_signals(
        self,
        explicit_pack: EvidencePack,
        chunks: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
        synthetic_slides: set[str],
    ) -> tuple[list[NegativeSignal], list[dict[str, Any]], bool, dict[str, int]]:
        signals, explicit_red_flags = self._explicit_pack_signals(explicit_pack, synthetic_slides)
        status_evidence: list[dict[str, Any]] = []
        counts = {
            "retrieved_atoms": len(explicit_pack.atoms),
            "table_trend_candidates": 0,
            "chart_trend_candidates": 0,
            "watchpoint_candidates": 0,
            "green_gates": 0,
        }
        for row in (item for item in chunks if str(item.get("chunk_type") or "").casefold() == "table"):
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
        return signals, status_evidence, explicit_red_flags, counts

    @staticmethod
    def _explicit_pack_signals(
        explicit_pack: EvidencePack,
        synthetic_slides: set[str],
    ) -> tuple[list[NegativeSignal], bool]:
        signals: list[NegativeSignal] = []
        red_flags = False
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
                red_flags = True
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
        return signals, red_flags

    @staticmethod
    def _select_signals(
        signals: list[NegativeSignal],
        max_signals: int,
    ) -> tuple[list[NegativeSignal], dict[SignalCategory, int]]:
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
        return deduped, category_counts

    @staticmethod
    def _analyzed_sources(chunks: list[dict[str, Any]], chart_rows: list[dict[str, Any]]) -> int:
        eligible_chunks = {
            str(row.get("id") or f"{row.get('slide_id')}:{row.get('element_id')}:{index}")
            for index, row in enumerate(chunks)
            if str(row.get("chunk_type") or "").casefold() in {"text", "table"}
            and classify_content_role(row) not in {"boilerplate", "methodology", "provenance"}
        }
        return len(eligible_chunks) + len({str(row.get("series_id")) for row in chart_rows})

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
        green_evidence = self._green_gate_evidence(source, headers, rows, lines, synthetic)
        if green_evidence is not None:
            return [], [green_evidence]
        return self._table_trend_signals(source, headers, rows, synthetic), []

    @staticmethod
    def _green_gate_evidence(
        source: dict[str, Any],
        headers: list[str],
        rows: list[list[str]],
        lines: list[str],
        synthetic: bool,
    ) -> dict[str, Any] | None:
        folded_headers = [header.casefold() for header in headers]
        green_index = next((i for i, header in enumerate(folded_headers) if header in {"green", "绿"}), None)
        current_index = next((i for i, header in enumerate(folded_headers) if header in {"current", "actual", "当前", "实际"}), None)
        if green_index is None or current_index is None:
            return None
        outcomes = [
            outcome for row in rows if (outcome := NegativeSignalAnalyzer._green_gate_outcome(row, current_index, green_index)) is not None
        ]
        if not outcomes or not all(outcomes):
            return None
        evidence = _chunk_evidence(source, "\n".join(lines), "threshold_status", "traffic_light_gate")
        evidence["synthetic"] = synthetic
        return evidence

    @staticmethod
    def _green_gate_outcome(row: list[str], current_index: int, green_index: int) -> bool | None:
        current, threshold = _number(row[current_index]), _number(row[green_index])
        if current is None or threshold is None:
            return None
        condition = row[green_index]
        if ">" in condition or "≥" in condition:
            return current >= threshold if ">=" in condition or "≥" in condition else current > threshold
        if "<" in condition or "≤" in condition:
            return current <= threshold if "<=" in condition or "≤" in condition else current < threshold
        return None

    @staticmethod
    def _table_trend_signals(
        source: dict[str, Any],
        headers: list[str],
        rows: list[list[str]],
        synthetic: bool,
    ) -> list[NegativeSignal]:
        period_columns = [(index, key) for index, header in enumerate(headers[1:], 1) if (key := _period_key(header))]
        if len(period_columns) < 2:
            return []
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
        return signals

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
