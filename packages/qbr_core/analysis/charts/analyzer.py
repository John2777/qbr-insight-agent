from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from packages.qbr_core.analysis.calculations import VerifiedCalculation
from packages.qbr_core.analysis.charts.reporting import render_chart_fallback
from packages.qbr_core.analysis.charts.semantics import ChartDataCube
from packages.qbr_core.analysis.charts.structure import (
    ChartRequestSignature,
    chart_family,
    chart_scopes,
    is_temporal_category,
    question_tokens,
    request_signature,
    series_groups,
)
from packages.qbr_core.planning.models import QueryPlan


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    """Summarize one chart series while retaining its supporting source rows."""
    series_id: str
    name: str
    family: str
    chart_type: str
    category_mode: str
    axis_id: str
    unit: str
    start_period: str
    start_value: float
    latest_period: str
    latest_value: float
    absolute_change: float
    relative_change_percent: float | None
    year_over_year_change_percent: float | None
    recent_change_percent: float | None
    latest_vs_peak_percent: float | None
    periods_since_maximum: int
    trailing_declines: int
    minimum_period: str
    minimum_value: float
    maximum_period: str
    maximum_value: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "series_id": self.series_id,
            "name": self.name,
            "family": self.family,
            "chart_type": self.chart_type,
            "category_mode": self.category_mode,
            "axis_id": self.axis_id,
            "unit": self.unit,
            "start": {"period": self.start_period, "value": self.start_value},
            "latest": {"period": self.latest_period, "value": self.latest_value},
            "absolute_change": self.absolute_change,
            "relative_change_percent": self.relative_change_percent,
            "year_over_year_change_percent": self.year_over_year_change_percent,
            "recent_change_percent": self.recent_change_percent,
            "latest_vs_peak_percent": self.latest_vs_peak_percent,
            "periods_since_maximum": self.periods_since_maximum,
            "trailing_declines": self.trailing_declines,
            "minimum": {"period": self.minimum_period, "value": self.minimum_value},
            "maximum": {"period": self.maximum_period, "value": self.maximum_value},
        }


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _percentage_change(start: float, end: float) -> float | None:
    return None if math.isclose(start, 0.0, abs_tol=1e-12) else (end - start) / abs(start) * 100


def _recent_change(values: list[float], window: int = 3) -> float | None:
    if len(values) < window * 2:
        return None
    previous = sum(values[-window * 2 : -window]) / window
    latest = sum(values[-window:]) / window
    return _percentage_change(previous, latest)


class ChartAnalyzer:
    """Build one bounded, chart-local analytical evidence bundle from native points.

    This component computes descriptive facts only. Business narrative remains an
    LLM responsibility, but the model no longer has to infer chart membership,
    trends, extrema or recent divergence from a flattened chunk.
    """

    def analyze(
        self,
        question: str,
        rows: list[dict[str, Any]],
        *,
        plan: QueryPlan,
        preferred_element_ids: set[str] | None = None,
        preferred_slide_ids: set[str] | None = None,
    ) -> VerifiedCalculation | None:
        """Analyze the supplied evidence and return structured findings."""
        if not rows:
            return None
        preferred = preferred_element_ids or set()
        preferred_slides = preferred_slide_ids or set()
        request = request_signature(plan)
        if not plan.needs_visuals and not (len(request.series_counts) >= 2 and (preferred or preferred_slides)):
            return None
        scopes = chart_scopes(rows)
        selected, selection = self._select_scope(scopes, request, preferred, preferred_slides)
        if not selected:
            return None

        selected_rows = scopes[selected]
        summaries = self._summaries(selected_rows)
        if not summaries:
            return None
        all_names = {str(row.get("series_name") or "").strip() for row in rows}
        selected_names = {summary.name for summary in summaries}
        excluded_names = sorted(name for name in all_names - selected_names if name)
        facts, text = self._render_bundle(selected_rows, summaries)
        scope = self._scope_metadata(selected_rows, summaries, excluded_names, request, selection)
        evidence = self._bundle_evidence(selected_rows, text, scope)
        return VerifiedCalculation(
            text=text,
            evidence=(evidence,),
            facts=tuple(facts),
            kind="chart_analysis",
            scope=scope,
            fallback_text=render_chart_fallback(question, summaries, facts),
        )

    def _select_scope(
        self,
        scopes: dict[str, list[dict[str, Any]]],
        request: ChartRequestSignature,
        preferred_element_ids: set[str],
        preferred_slide_ids: set[str],
    ) -> tuple[str | None, dict[str, Any]]:
        """Select one bounded chart scope relevant to the plan."""
        ranked: list[tuple[float, int, str, dict[str, Any]]] = []
        for scope, scope_rows in scopes.items():
            groups = series_groups(scope_rows)
            cube = ChartDataCube.from_rows(scope, scope_rows)
            if cube is None or not cube.is_analyzable:
                continue
            series_names = [str(group[0].get("series_name") or "").strip() for group in groups]
            families = [chart_family(str(group[0].get("chart_type") or "")) for group in groups]
            context = " ".join(
                [
                    *(str(row.get("chart_title") or "") for row in scope_rows[:2]),
                    str(scope_rows[0].get("slide_title") or ""),
                    str(scope_rows[0].get("slide_summary") or ""),
                    *series_names,
                ]
            ).casefold()
            context_terms = question_tokens(context)
            token_overlap = len(request.tokens & context_terms)
            element_overlap = len({str(row.get("element_id") or "") for row in scope_rows} & preferred_element_ids)
            slide_overlap = int(str(scope_rows[0].get("slide_id") or "") in preferred_slide_ids)
            family_counts = Counter(families)
            candidate_counts = Counter(count for count in family_counts.values() if count > 1)
            requested_counts = Counter(request.series_counts)
            count_matches = sum((candidate_counts & requested_counts).values())
            point_lengths = Counter(len(group) for group in groups)
            point_matches = sum(point_lengths[count] for count in request.point_counts)
            family_matches = sum(family in family_counts for family in request.requested_families)
            exact_names = sum(
                bool(name_tokens) and name_tokens <= request.tokens
                for name in series_names
                if (name_tokens := question_tokens(name))
            )
            score = (
                float(token_overlap)
                + 12.0 * element_overlap
                + 10.0 * slide_overlap
                + 9.0 * count_matches
                + 7.0 * int(bool(point_matches))
                + 5.0 * family_matches
                + 8.0 * exact_names
            )
            count_constraint_failed = bool(requested_counts) and count_matches == 0
            point_constraint_failed = bool(request.point_counts) and point_matches == 0
            if count_constraint_failed:
                score -= 48.0
            if point_constraint_failed:
                score -= 48.0
            diagnostics = {
                "scope": scope,
                "score": score,
                "token_overlap": token_overlap,
                "preferred_element_overlap": element_overlap,
                "preferred_slide_overlap": slide_overlap,
                "series_count_matches": count_matches,
                "point_count_matches": point_matches,
                "family_matches": family_matches,
                "count_constraint_failed": count_constraint_failed,
                "point_constraint_failed": point_constraint_failed,
                "family_counts": dict(family_counts),
                "point_counts": sorted(point_lengths),
            }
            diagnostics["analytical_entity_count"] = cube.analytical_entity_count
            diagnostics["structural_modes"] = {
                "share": cube.is_share,
                "ordered_categorical": cube.is_ordered_categorical,
            }
            ranked.append((score, cube.analytical_entity_count, scope, diagnostics))
        if not ranked:
            return None, {}
        best = max(ranked)
        return (best[2], best[3]) if best[0] > 0 else (None, best[3])

    @staticmethod
    def _summaries(rows: list[dict[str, Any]]) -> list[SeriesSummary]:
        """Compute provenance-preserving summaries for selected series."""
        summaries: list[SeriesSummary] = []
        for group in series_groups(rows):
            numeric = [row for row in group if row.get("y_value") is not None]
            if len(numeric) < 2:
                continue
            values = [float(row["y_value"]) for row in numeric]
            temporal = sum(is_temporal_category(str(row.get("category") or "")) for row in numeric) >= max(
                2, math.ceil(len(numeric) * 0.7)
            )
            start, latest = numeric[0], numeric[-1]
            minimum = min(numeric, key=lambda row: float(row["y_value"]))
            maximum = max(numeric, key=lambda row: float(row["y_value"]))
            maximum_index = numeric.index(maximum)
            trailing_declines = 0
            for previous, current in zip(reversed(values[:-1]), reversed(values[1:]), strict=True):
                if current < previous:
                    trailing_declines += 1
                else:
                    break
            year_over_year = _percentage_change(values[-13], values[-1]) if temporal and len(values) >= 13 else None
            unit = str(start.get("unit") or "").strip()
            if unit in {"0", "0.0", "general", "General"}:
                unit = ""
            summaries.append(
                SeriesSummary(
                    series_id=str(start.get("series_id") or start.get("series_name") or ""),
                    name=str(start.get("series_name") or "Unnamed series"),
                    family=chart_family(str(start.get("chart_type") or "")),
                    chart_type=str(start.get("chart_type") or "unknown"),
                    category_mode="temporal" if temporal else "categorical",
                    axis_id=str(start.get("axis_id") or ""),
                    unit=unit,
                    start_period=str(start.get("category") or start.get("point_order") or "start"),
                    start_value=float(start["y_value"]),
                    latest_period=str(latest.get("category") or latest.get("point_order") or "latest"),
                    latest_value=float(latest["y_value"]),
                    absolute_change=float(latest["y_value"]) - float(start["y_value"]),
                    relative_change_percent=_percentage_change(float(start["y_value"]), float(latest["y_value"])),
                    year_over_year_change_percent=year_over_year,
                    recent_change_percent=_recent_change(values) if temporal else None,
                    latest_vs_peak_percent=_percentage_change(float(maximum["y_value"]), float(latest["y_value"])),
                    periods_since_maximum=len(numeric) - 1 - maximum_index if temporal else 0,
                    trailing_declines=trailing_declines if temporal else 0,
                    minimum_period=str(minimum.get("category") or minimum.get("point_order") or ""),
                    minimum_value=float(minimum["y_value"]),
                    maximum_period=str(maximum.get("category") or maximum.get("point_order") or ""),
                    maximum_value=float(maximum["y_value"]),
                )
            )
        return summaries

    def _render_bundle(
        self,
        rows: list[dict[str, Any]],
        summaries: list[SeriesSummary],
    ) -> tuple[list[dict[str, Any]], str]:
        """Render the selected chart facts as model grounding context."""
        first = rows[0]
        title = str(first.get("slide_title") or first.get("chart_title") or "Chart")
        families: dict[str, list[SeriesSummary]] = defaultdict(list)
        for summary in summaries:
            families[summary.family].append(summary)
        lines = [
            f"Chart analytical evidence bundle: slide={first.get('slide_no')}; title={title}",
            "Series membership is authoritative for this selected slide; do not substitute metrics from other slides.",
        ]
        facts: list[dict[str, Any]] = []
        family_labels = {"bar": "bar/column series", "line": "line series", "share": "share series", "other": "other series"}
        for family in ("bar", "line", "share", "other"):
            if not families.get(family):
                continue
            lines.append(f"{family_labels[family]} ({len(families[family])}): " + ", ".join(item.name for item in families[family]))
            for item in families[family]:
                yoy = "n/a" if item.year_over_year_change_percent is None else f"{item.year_over_year_change_percent:.1f}%"
                recent = "n/a" if item.recent_change_percent is None else f"{item.recent_change_percent:.1f}%"
                peak_drawdown = "n/a" if item.latest_vs_peak_percent is None else f"{item.latest_vs_peak_percent:.1f}%"
                relative = "n/a" if item.relative_change_percent is None else f"{item.relative_change_percent:.1f}%"
                if item.category_mode == "temporal":
                    comparison = (
                        f"start={item.start_period}:{_format_number(item.start_value)}; "
                        f"latest={item.latest_period}:{_format_number(item.latest_value)}; "
                        f"absolute_change={_format_number(item.absolute_change)}; "
                        f"relative_change={relative}; year_over_year_change={yoy}; recent_3_vs_previous_3={recent}; "
                        f"latest_vs_peak={peak_drawdown}; periods_since_peak={item.periods_since_maximum}; "
                        f"trailing_declines={item.trailing_declines}"
                    )
                else:
                    comparison = (
                        f"first_category={item.start_period}:{_format_number(item.start_value)}; "
                        f"last_category={item.latest_period}:{_format_number(item.latest_value)}; "
                        f"first_to_last_change={_format_number(item.absolute_change)}; relative_change={relative}"
                    )
                lines.append(
                    f"- {item.name}: category_mode={item.category_mode}; {comparison}; "
                    f"minimum={_format_number(item.minimum_value)}@{item.minimum_period}; "
                    f"maximum={_format_number(item.maximum_value)}@{item.maximum_period}; "
                    f"axis_id={item.axis_id or 'unspecified'}; unit={item.unit or 'unspecified'}"
                )
                facts.append({"operation": "series_summary", **item.to_dict(), "basis": "derived_from_native_chart_points"})

        cube = ChartDataCube.from_rows("selected", rows)
        composition = cube.composition() if cube is not None else None
        if composition is not None:
            segments = dict(composition["segments"])
            largest = dict(composition["largest_segment"])
            lines.append(
                "Composition: total="
                + _format_number(float(composition["total"]))
                + "; segments="
                + ", ".join(f"{name}={_format_number(float(value))}" for name, value in segments.items())
                + f"; largest={largest['category']}:{_format_number(float(largest['value']))}"
            )
            facts.append(
                {
                    "operation": "share_composition",
                    **composition,
                    "basis": "derived_from_native_chart_points",
                }
            )

        path = cube.sequential_path() if cube is not None else None
        if path is not None:
            decline = dict(path["largest_decline"])
            increase = dict(path["largest_increase"])
            lines.append(
                f"Ordered categorical path: {path['start_category']}={_format_number(float(path['start_value']))}; "
                f"{path['end_category']}={_format_number(float(path['end_value']))}; "
                f"net_change={_format_number(float(path['net_change']))}; "
                f"largest_decline={decline['from']}→{decline['to']}:{_format_number(float(decline['delta']))}; "
                f"largest_increase={increase['from']}→{increase['to']}:{_format_number(float(increase['delta']))}"
            )
            facts.append(
                {
                    "operation": "sequential_path",
                    **path,
                    "basis": "derived_from_native_chart_points",
                }
            )

        bar_total = self._bar_total(rows, families.get("bar", [])) if families.get("line") or self._is_stacked(rows) else None
        if bar_total:
            lines.append(bar_total[0])
            facts.extend(bar_total[1])
            recent_total = bar_total[2]
            declining_lines = [
                item.name
                for item in families.get("line", [])
                if (item.recent_change_percent is not None and item.recent_change_percent < 0)
                or (
                    item.latest_vs_peak_percent is not None
                    and item.latest_vs_peak_percent <= -3
                    and item.periods_since_maximum <= 5
                    and item.trailing_declines >= 2
                )
            ]
            if recent_total is not None and recent_total > 0 and declining_lines:
                lines.append(
                    "Observed recent divergence: aggregate bar/column output increased while these line series declined: "
                    + ", ".join(declining_lines)
                    + ". This is a descriptive divergence, not proof of causation."
                )
                facts.append(
                    {
                        "operation": "recent_divergence",
                        "aggregate_bar_recent_change_percent": recent_total,
                        "declining_line_series": declining_lines,
                        "basis": "derived_from_native_chart_points",
                    }
                )
        if families.get("bar") and families.get("line"):
            lines.append(
                "Interpretation boundary: bar/column and line series share a slide, "
                "but no one-to-one mapping or causal relationship is declared."
            )
        elif any(item.category_mode == "categorical" for item in summaries):
            lines.append(
                "Interpretation boundary: the x-axis is categorical or scenario-based, not temporal; "
                "do not describe left-to-right differences as recent trends."
            )
        else:
            lines.append(
                "Interpretation boundary: series may use different units; do not sum heterogeneous measures "
                "or infer causality from co-movement."
            )
        return facts, "\n".join(lines)

    @staticmethod
    def _is_stacked(rows: list[dict[str, Any]]) -> bool:
        """Return whether the selected chart is structurally stacked."""
        for row in rows:
            raw = row.get("visual_json") or {}
            try:
                visual = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(visual, dict) and str(visual.get("grouping") or "").casefold() in {"stacked", "percentstacked"}:
                return True
        return False

    @staticmethod
    def _bar_total(
        rows: list[dict[str, Any]],
        bar_summaries: list[SeriesSummary],
    ) -> tuple[str, list[dict[str, Any]], float | None] | None:
        """Return an auditable stacked-bar total when values are compatible."""
        if len(bar_summaries) < 2:
            return None
        selected_ids = {summary.series_id for summary in bar_summaries}
        by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if str(row.get("series_id") or "") in selected_ids and row.get("y_value") is not None:
                by_series[str(row.get("series_id"))].append(row)
        ordered = [sorted(items, key=lambda item: int(item.get("point_order") or 0)) for items in by_series.values()]
        if len(ordered) != len(bar_summaries):
            return None
        category_maps = [
            {str(row.get("category") or row.get("point_order")): float(row["y_value"]) for row in items}
            for items in ordered
        ]
        common = set(category_maps[0]).intersection(*(set(mapping) for mapping in category_maps[1:]))
        categories = [
            str(row.get("category") or row.get("point_order"))
            for row in ordered[0]
            if str(row.get("category") or row.get("point_order")) in common
        ]
        if len(categories) < 2:
            return None
        if sum(is_temporal_category(category) for category in categories) < max(2, math.ceil(len(categories) * 0.7)):
            return None
        totals = [sum(mapping[category] for mapping in category_maps) for category in categories]
        relative = _percentage_change(totals[0], totals[-1])
        yoy = _percentage_change(totals[-13], totals[-1]) if len(totals) >= 13 else None
        recent = _recent_change(totals)
        latest_total = totals[-1]
        shares = {
            summary.name: (mapping[categories[-1]] / latest_total * 100 if latest_total else 0.0)
            for summary, mapping in zip(bar_summaries, category_maps, strict=True)
        }
        line = (
            f"Aggregate bar/column output: {categories[0]}={_format_number(totals[0])}; "
            f"{categories[-1]}={_format_number(totals[-1])}; "
            f"relative_change={'n/a' if relative is None else f'{relative:.1f}%'}; "
            f"year_over_year_change={'n/a' if yoy is None else f'{yoy:.1f}%'}; "
            f"recent_3_vs_previous_3={'n/a' if recent is None else f'{recent:.1f}%'}; "
            "latest shares="
            + ", ".join(f"{name}={share:.1f}%" for name, share in shares.items())
        )
        facts = [
            {
                "operation": "aggregate_series",
                "family": "bar",
                "start_period": categories[0],
                "start_value": totals[0],
                "latest_period": categories[-1],
                "latest_value": totals[-1],
                "relative_change_percent": relative,
                "year_over_year_change_percent": yoy,
                "recent_change_percent": recent,
                "latest_shares_percent": shares,
                "basis": "derived_from_native_chart_points",
            }
        ]
        return line, facts, recent

    @staticmethod
    def _scope_metadata(
        rows: list[dict[str, Any]],
        summaries: list[SeriesSummary],
        excluded_names: list[str],
        request: ChartRequestSignature,
        selection: dict[str, Any],
    ) -> dict[str, Any]:
        """Build chart-scope diagnostics for verification and observability."""
        family_series: dict[str, list[str]] = defaultdict(list)
        for summary in summaries:
            family_series[summary.family].append(summary.name)
        requested_counts = Counter(request.series_counts)
        minimum_mentions = {
            family: min(2, len(names)) if requested_counts[len(names)] else 1
            for family, names in family_series.items()
        }
        return {
            "kind": "chart_analysis",
            "slide_id": str(rows[0].get("slide_id") or ""),
            "slide_no": rows[0].get("slide_no"),
            "element_ids": sorted({str(row.get("element_id") or "") for row in rows if row.get("element_id")}),
            "chart_ids": sorted({str(row.get("chart_id") or row.get("element_id") or "") for row in rows}),
            "selected_series_names": [summary.name for summary in summaries],
            "family_series": dict(family_series),
            "excluded_document_series_names": excluded_names,
            "minimum_family_mentions": minimum_mentions,
            "no_pairwise_mapping": True,
            "request_signature": request.to_dict(),
            "selection": selection,
        }

    @staticmethod
    def _bundle_evidence(rows: list[dict[str, Any]], text: str, scope: dict[str, Any]) -> dict[str, Any]:
        """Return deduplicated evidence rows supporting the chart bundle."""
        row = rows[0]
        try:
            bbox = json.loads(str(row.get("bbox_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            bbox = {}
        confidences = [
            float(item.get(key) or 1.0)
            for item in rows
            for key in ("chart_confidence", "series_confidence", "confidence")
        ]
        return {
            "document_version_id": row["document_version_id"],
            "document_id": row.get("document_id"),
            "slide_id": row["slide_id"],
            "element_id": row.get("element_id"),
            "chunk_id": None,
            "quote": text,
            "bbox": bbox,
            "confidence": min(confidences) if confidences else 1.0,
            "source_kind": row.get("source_kind") or "native_ooxml",
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
            "slide_title": row.get("slide_title"),
            "content_role": "chart",
            "facet": "chart analysis",
            "extraction": "native_chart_analysis_bundle",
            "chart_scope": scope,
        }
