from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .chart_semantics import ChartDataCube, chart_cubes, normalize_dimension, units_compatible
from .chart_structure import chart_scopes


@dataclass(frozen=True, slots=True)
class VerifiedCalculation:
    text: str
    evidence: tuple[dict[str, Any], ...]
    facts: tuple[dict[str, Any], ...] = ()
    kind: str = "calculation"
    scope: dict[str, Any] = field(default_factory=dict)
    fallback_text: str | None = None


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


def _point_evidence(
    row: dict[str, Any],
    *,
    operation: str = "point_lookup",
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        bbox = json.loads(str(row.get("bbox_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        bbox = {}
    return {
        "document_version_id": row["document_version_id"],
        "document_id": row.get("document_id"),
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
        "calculation_operation": operation,
        "calculation_scope": scope or {},
    }


def _format_value(row: dict[str, Any]) -> str:
    display = str(row.get("display_value") or "").strip()
    return display or f"{float(row['y_value']):g}"


_CHANGE_CUES = ("增长", "变化", "变动", "增加", "下降", "降低", "从", "到", "比", "growth", "change", "from", " to ")
_COMPARE_CUES = ("比较", "哪个", "高多少", "低多少", "compare", "higher", "lower", "difference")
_VALUE_CUES = ("多少", "数值", "value", "what is", "how much")
_SHARE_CUES = ("占比", "份额", "构成", "结构", "share", "mix", "composition")
_SHARE_VISUAL_CUES = ("饼图", "饼状图", "环形图", "pie", "doughnut", "donut")
_COMPLEMENT_CUES = ("非", "其余", "剩余", "之外", "other", "remaining", "non-")
_STEP_CUES = ("单步", "逐步", "阶段", "路径", "step", "path", "bridge")
_ADVERSE_CUES = ("拖累", "下降", "减少", "降幅", "decline", "decrease", "drag", "drop")
_EXTREME_CUES = ("最大", "最主要", "largest", "biggest", "most")


def _contains_any(question: str, cues: tuple[str, ...]) -> bool:
    folded = question.casefold()
    return any(cue in folded for cue in cues)


def _ordered_mentions(question: str, labels: tuple[str, ...]) -> list[str]:
    folded = normalize_dimension(question)
    return sorted(labels, key=lambda label: folded.find(normalize_dimension(label)))


def _comparison_order(question: str, labels: tuple[str, ...]) -> tuple[str, str] | None:
    if len(labels) < 2:
        return None
    ordered = _ordered_mentions(question, labels)[:2]
    folded = question.casefold()
    first_position = folded.find(ordered[0].casefold())
    second_position = folded.find(ordered[1].casefold())
    between = folded[first_position + len(ordered[0]) : second_position] if 0 <= first_position < second_position else ""
    # "A 比 B" means B is the baseline and A is the target. Other forms keep
    # the user's mention order, which naturally handles "from A to B".
    if "比" in between or re.search(r"\bthan\b", between):
        return ordered[1], ordered[0]
    return ordered[0], ordered[1]


def _calculation_scope(cube: ChartDataCube, operation: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0]
    return {
        "kind": "chart_calculation",
        "operation": operation,
        "scope_id": cube.scope_id,
        "document_id": first.get("document_id"),
        "slide_id": first.get("slide_id"),
        "slide_no": first.get("slide_no"),
        "element_ids": sorted({str(row.get("element_id") or "") for row in rows if row.get("element_id")}),
        "series_names": list(dict.fromkeys(str(row.get("series_name") or "") for row in rows)),
        "categories": list(dict.fromkeys(str(row.get("category") or "") for row in rows)),
    }


def _bundle_evidence(
    cube: ChartDataCube,
    rows: list[dict[str, Any]],
    *,
    operation: str,
    quote: str,
) -> dict[str, Any]:
    evidence = _point_evidence(rows[0], operation=operation, scope=_calculation_scope(cube, operation, rows))
    evidence.update(
        {
            "quote": quote,
            "numeric_identities": [_numeric_identity(row) for row in rows],
            "numeric_identity": None,
            "facet": "verified chart calculation",
        }
    )
    return evidence


class ChartCalculator:
    """Compute chart facts without choosing an answer style or business narrative."""

    def analyze(self, question: str, rows: list[dict[str, Any]]) -> VerifiedCalculation | None:
        cubes = sorted(
            chart_cubes(chart_scopes([row for row in rows if row.get("y_value") is not None])),
            key=lambda cube: (cube.relevance(question), len(cube.rows)),
            reverse=True,
        )
        candidates: list[tuple[int, VerifiedCalculation]] = []
        operations = (
            self._cross_dimension_change,
            self._share_composition,
            self._path_attribution,
            self._period_change,
            self._series_comparison,
            self._point_lookup,
        )
        for cube in cubes:
            relevance = cube.relevance(question)
            for priority, operation in enumerate(operations, 1):
                result = operation(question, cube)
                if result is not None:
                    # In a multi-chart document, a deterministic calculator may
                    # not claim a chart merely because its shape supports an
                    # operation. At least one source-derived label/title must
                    # match; otherwise broad structural requests belong to the
                    # chart selector, which also evaluates cardinality and type.
                    if len(cubes) > 1 and relevance <= 0:
                        break
                    if (
                        len(cubes) > 1
                        and result.scope.get("operation") == "share_composition"
                        and not cube.mentioned_series(question)
                        and not _contains_any(question, _SHARE_VISUAL_CUES)
                    ):
                        break
                    candidates.append((relevance * 10 + (len(operations) - priority), result))
                    break
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _point_lookup(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        if not _contains_any(question, _VALUE_CUES):
            return None
        series, categories = cube.mentioned_series(question), cube.mentioned_categories(question)
        if len(series) != 1 or len(categories) != 1:
            return None
        row = cube.point(series[0], categories[0])
        if row is None:
            return None
        scope = _calculation_scope(cube, "point_lookup", [row])
        text = f"{row.get('series_name')}: {row.get('category')}={_format_value(row)}. [1]"
        return VerifiedCalculation(
            text,
            (_point_evidence(row, operation="point_lookup", scope=scope),),
            (_numeric_identity(row),),
            scope=scope,
        )

    @staticmethod
    def _period_change(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        if not _contains_any(question, _CHANGE_CUES):
            return None
        mentioned_series = cube.mentioned_series(question)
        if len(mentioned_series) != 1:
            return None
        ordered = list(cube.series_rows(mentioned_series[0]))
        mentioned_categories = cube.mentioned_categories(question)
        if len(mentioned_categories) >= 2:
            order = _comparison_order(question, mentioned_categories)
            pair = [cube.point(mentioned_series[0], category) for category in order or ()]
        else:
            pair = ordered[-2:]
        if len(pair) != 2 or any(row is None for row in pair):
            return None
        start, end = pair
        assert start is not None and end is not None
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
        scope = _calculation_scope(cube, "period_change", [start, end])
        return VerifiedCalculation(
            text,
            (
                _point_evidence(start, operation="period_change", scope=scope),
                _point_evidence(end, operation="period_change", scope=scope),
            ),
            facts,
            scope=scope,
        )

    @staticmethod
    def _series_comparison(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        if not _contains_any(question, _COMPARE_CUES):
            return None
        series, categories = cube.mentioned_series(question), cube.mentioned_categories(question)
        if len(series) < 2 or len(categories) != 1:
            return None
        first, second = (cube.point(name, categories[0]) for name in _ordered_mentions(question, series)[:2])
        if first is None or second is None or not units_compatible((first, second)):
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
        scope = _calculation_scope(cube, "series_comparison", [first, second])
        return VerifiedCalculation(
            text,
            (
                _point_evidence(first, operation="series_comparison", scope=scope),
                _point_evidence(second, operation="series_comparison", scope=scope),
            ),
            facts,
            scope=scope,
        )

    @staticmethod
    def _cross_dimension_change(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        """Compare two series at one category, including transposed year/market charts."""

        if not _contains_any(question, _CHANGE_CUES):
            return None
        series, categories = cube.mentioned_series(question), cube.mentioned_categories(question)
        if len(series) < 2 or len(categories) != 1:
            return None
        order = _comparison_order(question, series)
        if order is None:
            return None
        start = cube.point(order[0], categories[0])
        end = cube.point(order[1], categories[0])
        if start is None or end is None or not units_compatible((start, end)):
            return None
        start_value, end_value = float(start["y_value"]), float(end["y_value"])
        absolute = end_value - start_value
        relative = None if math.isclose(start_value, 0.0, abs_tol=1e-12) else absolute / abs(start_value) * 100
        relative_text = "undefined (zero baseline)" if relative is None else f"{relative:.1f}%"
        text = (
            f"{categories[0]}: {start.get('series_name')}={_format_value(start)}; "
            f"{end.get('series_name')}={_format_value(end)}; absolute change={absolute:g}; "
            f"relative change={relative_text}. [1][2]"
        )
        facts = (
            _numeric_identity(start),
            _numeric_identity(end),
            {
                "operation": "cross_dimension_change",
                "category": categories[0],
                "baseline_series": start.get("series_name"),
                "target_series": end.get("series_name"),
                "absolute_change": absolute,
                "relative_change_percent": relative,
                "basis": "derived_from_chart_points",
            },
        )
        scope = _calculation_scope(cube, "cross_dimension_change", [start, end])
        return VerifiedCalculation(
            text,
            (
                _point_evidence(start, operation="cross_dimension_change", scope=scope),
                _point_evidence(end, operation="cross_dimension_change", scope=scope),
            ),
            facts,
            scope=scope,
        )

    @staticmethod
    def _share_composition(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        if not cube.is_share or not (_contains_any(question, _SHARE_CUES) or cube.mentioned_categories(question)):
            return None
        rows = list(cube.groups[0])
        composition = cube.composition()
        assert composition is not None
        total = float(composition["total"])
        segments = dict(composition["segments"])
        shares = dict(composition["shares_percent"])
        percent_scale = bool(composition["is_percent_scale"])

        def display(name: str, value: float) -> str:
            return f"{name}={value:g}%" if percent_scale else f"{name}={value:g} ({shares[name]:.1f}%)"

        structure = ", ".join(display(name, value) for name, value in segments.items())
        text = f"Composition total={total:g}{'%' if percent_scale else ''}; {structure}."
        mentioned = cube.mentioned_categories(question)
        complement: float | None = None
        if len(mentioned) == 1 and _contains_any(question, _COMPLEMENT_CUES):
            target = mentioned[0]
            complement = total - segments[target]
            complement_share = 0.0 if math.isclose(total, 0.0) else complement / total * 100
            if percent_scale:
                text += f" {target}={segments[target]:g}%; complement={complement:g}%."
            else:
                text += (
                    f" {target}={segments[target]:g} ({shares[target]:.1f}%); "
                    f"complement={complement:g} ({complement_share:.1f}%)."
                )
        text += " [1]"
        facts: list[dict[str, Any]] = [
            {
                "operation": "share_composition",
                "total": total,
                "segments": segments,
                "shares_percent": shares,
                "basis": "derived_from_chart_points",
            }
        ]
        if complement is not None:
            facts.append(
                {
                    "operation": "share_complement",
                    "selected_category": mentioned[0],
                    "selected_value": segments[mentioned[0]],
                    "complement_value": complement,
                    "basis": "derived_from_chart_points",
                }
            )
        scope = _calculation_scope(cube, "share_composition", rows)
        evidence = _bundle_evidence(cube, rows, operation="share_composition", quote=f"{rows[0].get('series_name')}: {structure}")
        return VerifiedCalculation(text, (evidence,), tuple(facts), scope=scope)

    @staticmethod
    def _path_attribution(question: str, cube: ChartDataCube) -> VerifiedCalculation | None:
        if not cube.is_ordered_categorical:
            return None
        explicit_path = _contains_any(question, _STEP_CUES)
        asks_extreme_decline = _contains_any(question, _EXTREME_CUES) and _contains_any(question, _ADVERSE_CUES)
        if not (explicit_path or asks_extreme_decline):
            return None
        rows = list(cube.groups[0])
        path = cube.sequential_path()
        assert path is not None
        steps = list(path["steps"])
        largest_decline = dict(path["largest_decline"])
        largest_increase = dict(path["largest_increase"])
        start, end = rows[0], rows[-1]
        net_change = float(path["net_change"])
        step_text = "; ".join(f"{item['from']}→{item['to']}={float(item['delta']):g}" for item in steps)
        text = (
            f"{start.get('series_name')}: {_format_value(start)}→{_format_value(end)}; net change={net_change:g}. "
            f"Steps: {step_text}. Largest decline={largest_decline['from']}→{largest_decline['to']} "
            f"({float(largest_decline['delta']):g}). [1]"
        )
        facts = (
            {
                "operation": "sequential_path",
                "start_category": start.get("category"),
                "start_value": float(start["y_value"]),
                "end_category": end.get("category"),
                "end_value": float(end["y_value"]),
                "net_change": net_change,
                "steps": steps,
                "largest_decline": largest_decline,
                "largest_increase": largest_increase,
                "basis": "derived_from_chart_points",
            },
        )
        sequence = "; ".join(f"{row.get('category')}={_format_value(row)}" for row in rows)
        scope = _calculation_scope(cube, "sequential_path", rows)
        evidence = _bundle_evidence(cube, rows, operation="sequential_path", quote=f"{start.get('series_name')}: {sequence}")
        return VerifiedCalculation(text, (evidence,), facts, scope=scope)
