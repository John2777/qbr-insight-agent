from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VerifiedCalculation:
    text: str
    evidence: tuple[dict[str, Any], ...]
    facts: tuple[dict[str, Any], ...] = ()


def _numeric_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric": row.get("series_name"),
        "period": row.get("category"),
        "value": float(row["y_value"]),
        "display_value": _format_value(row),
        "unit": row.get("unit"),
        "basis": row.get("basis"),
        "source_type": row.get("source_kind") or "native_chart_point",
    }


def _point_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        bbox = json.loads(str(row.get("bbox_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        bbox = {}
    return {
        "document_version_id": row["document_version_id"],
        "slide_id": row["slide_id"],
        "element_id": row["element_id"],
        "chunk_id": None,
        "quote": f"{row.get('series_name')}: {row.get('category')}={_format_value(row)}",
        "bbox": bbox,
        "confidence": min(
            float(row.get("chart_confidence") or 1.0),
            float(row.get("series_confidence") or 1.0),
            float(row.get("confidence") or 1.0),
        ),
        "source_kind": row.get("source_kind") or "native_ooxml",
        "document_title": row.get("document_title"),
        "slide_no": row.get("slide_no"),
        "slide_title": row.get("slide_title"),
        "content_role": "chart",
        "facet": "verified calculation",
        "extraction": "native_chart_calculation",
        "numeric_identity": _numeric_identity(row),
    }


def _format_value(row: dict[str, Any]) -> str:
    display = str(row.get("display_value") or "").strip()
    return display or f"{float(row['y_value']):g}"


class ChartCalculator:
    """Compute chart facts without choosing an answer style or business narrative."""

    def analyze(self, question: str, rows: list[dict[str, Any]]) -> VerifiedCalculation | None:
        numeric = [row for row in rows if row.get("y_value") is not None]
        return self._period_change(question, numeric) or self._series_comparison(question, numeric) or self._point_lookup(question, numeric)

    @staticmethod
    def _point_lookup(question: str, rows: list[dict[str, Any]]) -> VerifiedCalculation | None:
        folded = question.casefold()
        if not any(marker in folded for marker in ("多少", "数值", "value", "what is")):
            return None
        candidates = [
            row
            for row in rows
            if str(row.get("series_name") or "").casefold() in folded
            and str(row.get("category") or "").casefold() in folded
        ]
        if not candidates:
            return None
        row = candidates[0]
        text = f"{row.get('series_name')}: {row.get('category')}={_format_value(row)}. [1]"
        return VerifiedCalculation(text, (_point_evidence(row),), (_numeric_identity(row),))

    @staticmethod
    def _period_change(question: str, rows: list[dict[str, Any]]) -> VerifiedCalculation | None:
        folded = question.casefold()
        if not any(marker in folded for marker in ("增长", "变化", "增加", "下降", "从", "growth", "change", "from")):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            series_name = str(row.get("series_name") or "").strip()
            if series_name and series_name.casefold() in folded:
                groups.setdefault(str(row.get("series_id") or series_name), []).append(row)
        if not groups:
            return None
        candidates: list[tuple[int, list[dict[str, Any]]]] = []
        for series_rows in groups.values():
            ordered = sorted(series_rows, key=lambda row: int(row.get("point_order") or 0))
            mentioned = [row for row in ordered if str(row.get("category") or "").casefold() in folded]
            pair = mentioned[:1] + mentioned[-1:] if len(mentioned) >= 2 else ordered[-2:]
            if len(pair) == 2 and pair[0] is not pair[1]:
                candidates.append((len(mentioned), pair))
        if not candidates:
            return None
        _, pair = max(candidates, key=lambda item: item[0])
        start, end = pair
        start_value, end_value = float(start["y_value"]), float(end["y_value"])
        absolute = end_value - start_value
        relative = None if start_value == 0 else absolute / abs(start_value) * 100
        relative_text = "undefined (zero baseline)" if relative is None else f"{relative:.1f}%"
        text = (
            f"{end.get('series_name')}: {start.get('category')}={_format_value(start)}; "
            f"{end.get('category')}={_format_value(end)}; absolute change={absolute:g}; "
            f"relative change={relative_text}. [1][2]"
        )
        facts = (
            _numeric_identity(start),
            _numeric_identity(end),
            {
                "operation": "period_change",
                "metric": end.get("series_name"),
                "absolute_change": absolute,
                "relative_change_percent": relative,
                "basis": "derived_from_chart_points",
            },
        )
        return VerifiedCalculation(text, (_point_evidence(start), _point_evidence(end)), facts)

    @staticmethod
    def _series_comparison(question: str, rows: list[dict[str, Any]]) -> VerifiedCalculation | None:
        folded = question.casefold()
        if not any(marker in folded for marker in ("哪个更高", "高多少", "低多少", "compare", "higher", "lower")):
            return None
        mentioned = [row for row in rows if str(row.get("series_name") or "").casefold() in folded]
        by_series: dict[str, dict[str, Any]] = {}
        for row in mentioned:
            category = str(row.get("category") or "").casefold()
            if category and category not in folded:
                continue
            by_series.setdefault(str(row.get("series_id") or row.get("series_name")), row)
        if len(by_series) < 2:
            return None
        first, second = list(by_series.values())[:2]
        first_unit = str(first.get("unit") or "").strip().casefold()
        second_unit = str(second.get("unit") or "").strip().casefold()
        if first_unit and second_unit and first_unit != second_unit:
            return None
        high, low = sorted((first, second), key=lambda row: float(row["y_value"]), reverse=True)
        difference = float(high["y_value"]) - float(low["y_value"])
        text = (
            f"{first.get('series_name')}={_format_value(first)}; {second.get('series_name')}={_format_value(second)}; "
            f"higher series={high.get('series_name')}; difference={difference:g}. [1][2]"
        )
        facts = (
            _numeric_identity(first),
            _numeric_identity(second),
            {
                "operation": "series_comparison",
                "higher_metric": high.get("series_name"),
                "difference": difference,
                "unit": high.get("unit") or low.get("unit"),
                "basis": "derived_from_chart_points",
            },
        )
        return VerifiedCalculation(text, (_point_evidence(first), _point_evidence(second)), facts)
