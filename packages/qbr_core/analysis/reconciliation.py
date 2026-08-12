from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from packages.qbr_core.analysis.calculations import VerifiedCalculation, _bundle_evidence, _format_value
from packages.qbr_core.analysis.charts.semantics import ChartDataCube, chart_cubes, normalize_dimension
from packages.qbr_core.analysis.charts.structure import chart_scopes
from packages.qbr_core.analysis.table_reasoning import ParsedTable, _is_total_label, _number

_RECONCILIATION_CUES = (
    "勾稽",
    "核对",
    "矛盾",
    "对不上",
    "差异",
    "不一致",
    "一致吗",
    "为什么不同",
    "reconcil",
    "contradict",
    "discrepancy",
    "inconsistent",
    "do not match",
    "doesn't match",
)


@dataclass(frozen=True, slots=True)
class _DimensionProjection:
    """Represent one source as metric × member → value for set reconciliation."""

    source_kind: str
    metric: str
    aliases: tuple[str, ...]
    values: tuple[tuple[str, float], ...]
    unit_family: str
    source: dict[str, Any] | None = None
    cube: ChartDataCube | None = None
    rows: tuple[dict[str, Any], ...] = ()

    @property
    def keyed_values(self) -> dict[str, tuple[str, float]]:
        """Return member values keyed by normalized source labels."""

        return {normalize_dimension(label): (label, value) for label, value in self.values}


@dataclass(frozen=True, slots=True)
class _ScopeMatch:
    """Describe a proven aggregation relationship between two projections."""

    narrow: _DimensionProjection
    broad: _DimensionProjection
    target_key: str
    component_keys: tuple[str, ...]
    stable_keys: tuple[str, ...]
    score: int


def _unit_family(*values: object) -> str:
    text = " ".join(str(value or "") for value in values).casefold()
    if "%" in text or "percent" in text or "percentage" in text:
        return "percent"
    if any(marker in text for marker in ("us$", "usd", "million", "billion", "百万", "十亿", "亿")):
        return "currency"
    return "unknown"


def _close(left: float, right: float) -> bool:
    """Allow presentation rounding without accepting materially different values."""

    return math.isclose(left, right, rel_tol=0.001, abs_tol=0.5)


def _display_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return f"{int(round(value)):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


class ScopeReconciler:
    """Reconcile a repeated label whose membership changes across a table and chart.

    The implementation is deliberately vocabulary-free. It proves a scope
    change from source member sets and arithmetic identity, rather than from a
    maintained list of markets, products, business units, or catch-all labels.
    """

    @staticmethod
    def is_requested(question: str) -> bool:
        """Return whether the question explicitly asks to reconcile sources."""

        folded = question.casefold()
        return any(cue in folded for cue in _RECONCILIATION_CUES)

    def analyze(
        self,
        question: str,
        sources: list[dict[str, Any]],
        chart_rows: list[dict[str, Any]],
    ) -> VerifiedCalculation | None:
        """Return a result only when the member delta exactly explains the value delta."""

        if not self.is_requested(question):
            return None
        candidates: list[_ScopeMatch] = []
        for table in self._table_projections(sources):
            for chart in self._chart_projections(chart_rows):
                if not self._units_compatible(table, chart):
                    continue
                candidates.extend(self._matches(question, table, chart))
                candidates.extend(self._matches(question, chart, table))
        if not candidates:
            return None
        return self._result(question, max(candidates, key=lambda item: item.score))

    @staticmethod
    def _table_projections(sources: list[dict[str, Any]]) -> tuple[_DimensionProjection, ...]:
        """Extract numeric metric columns from authoritative table chunks."""

        projections: list[_DimensionProjection] = []
        for source in sources:
            if str(source.get("chunk_type") or "").casefold() != "table":
                continue
            table = ParsedTable.from_source(source)
            if table is None:
                continue
            for column, header in enumerate(table.headers[1:], 1):
                values: list[tuple[str, float]] = []
                displays: list[str] = []
                seen: set[str] = set()
                for row in table.rows:
                    if not row or _is_total_label(row[0]):
                        continue
                    key = normalize_dimension(row[0])
                    value = _number(row[column])
                    if not key or key in seen or value is None:
                        continue
                    seen.add(key)
                    values.append((row[0], float(value)))
                    displays.append(row[column])
                if len(values) >= 3:
                    projections.append(
                        _DimensionProjection(
                            source_kind="table",
                            metric=header,
                            aliases=tuple(dict.fromkeys((header, str(source.get("slide_title") or "")))),
                            values=tuple(values),
                            unit_family=_unit_family(header, *displays),
                            source=source,
                        )
                    )
        return tuple(projections)

    @staticmethod
    def _chart_projections(rows: list[dict[str, Any]]) -> tuple[_DimensionProjection, ...]:
        """Extract both native and transposed metric projections from chart rows."""

        projections: list[_DimensionProjection] = []
        for cube in chart_cubes(chart_scopes([row for row in rows if row.get("y_value") is not None])):
            title_aliases = tuple(
                str(value)
                for value in (
                    cube.rows[0].get("chart_title"),
                    cube.rows[0].get("slide_title"),
                    cube.rows[0].get("slide_summary"),
                )
                if value
            )
            for group in cube.groups:
                values = tuple(
                    (str(row.get("category") or ""), float(row["y_value"]))
                    for row in group
                    if normalize_dimension(row.get("category"))
                )
                if len(values) >= 2 and len({normalize_dimension(label) for label, _ in values}) == len(values):
                    metric = str(group[0].get("series_name") or "")
                    projections.append(
                        _DimensionProjection(
                            source_kind="chart",
                            metric=metric,
                            aliases=tuple(dict.fromkeys((metric, *title_aliases))),
                            values=values,
                            unit_family=_unit_family(group[0].get("unit"), *(_format_value(row) for row in group)),
                            cube=cube,
                            rows=tuple(group),
                        )
                    )
            for category in cube.category_names:
                category_rows = cube.category_rows(category)
                values = tuple(
                    (str(row.get("series_name") or ""), float(row["y_value"]))
                    for row in category_rows
                    if normalize_dimension(row.get("series_name"))
                )
                if len(values) >= 2 and len({normalize_dimension(label) for label, _ in values}) == len(values):
                    projections.append(
                        _DimensionProjection(
                            source_kind="chart",
                            metric=category,
                            aliases=tuple(dict.fromkeys((category, *title_aliases))),
                            values=values,
                            unit_family=_unit_family(
                                *(row.get("unit") for row in category_rows),
                                *(_format_value(row) for row in category_rows),
                            ),
                            cube=cube,
                            rows=tuple(category_rows),
                        )
                    )
        return tuple(projections)

    @staticmethod
    def _units_compatible(left: _DimensionProjection, right: _DimensionProjection) -> bool:
        """Accept matching unit families or a source with no explicit unit."""

        return "unknown" in {left.unit_family, right.unit_family} or left.unit_family == right.unit_family

    @staticmethod
    def _metric_score(question: str, left: _DimensionProjection, right: _DimensionProjection) -> int:
        """Score metric identity from source vocabulary and question mentions."""

        question_key = normalize_dimension(question)
        left_keys = {normalize_dimension(alias) for alias in left.aliases if normalize_dimension(alias)}
        right_keys = {normalize_dimension(alias) for alias in right.aliases if normalize_dimension(alias)}
        exact = left_keys & right_keys
        related = any(
            len(first) >= 3 and len(second) >= 3 and (first in second or second in first)
            for first in left_keys
            for second in right_keys
        )
        mentioned_left = any(len(key) >= 2 and key in question_key for key in left_keys)
        mentioned_right = any(len(key) >= 2 and key in question_key for key in right_keys)
        mention_score = 4 if mentioned_left and mentioned_right else 2 if mentioned_left or mentioned_right else 0
        return (8 if exact else 5 if related else 0) + mention_score

    def _matches(
        self,
        question: str,
        narrow: _DimensionProjection,
        broad: _DimensionProjection,
    ) -> list[_ScopeMatch]:
        """Prove that one source folds its omitted members into one shared label."""

        narrow_values, broad_values = narrow.keyed_values, broad.keyed_values
        common = set(narrow_values) & set(broad_values)
        components = set(narrow_values) - set(broad_values)
        if len(common) < 2 or not components or set(broad_values) - set(narrow_values):
            return []
        metric_score = self._metric_score(question, narrow, broad)
        if metric_score == 0 and len(common) < 4:
            return []
        mismatches = [key for key in common if not _close(narrow_values[key][1], broad_values[key][1])]
        stable = tuple(sorted(key for key in common if key not in mismatches))
        if len(mismatches) != 1 or not stable:
            return []
        target = mismatches[0]
        expected = narrow_values[target][1] + sum(narrow_values[key][1] for key in components)
        if not _close(expected, broad_values[target][1]):
            return []
        narrow_total = sum(value for _, value in narrow_values.values())
        broad_total = sum(value for _, value in broad_values.values())
        if not _close(narrow_total, broad_total):
            return []
        target_mentioned = normalize_dimension(narrow_values[target][0]) in normalize_dimension(question)
        score = metric_score + len(stable) * 2 + (6 if target_mentioned else 0) - len(components)
        return [
            _ScopeMatch(
                narrow=narrow,
                broad=broad,
                target_key=target,
                component_keys=tuple(sorted(components)),
                stable_keys=stable,
                score=score,
            )
        ]

    def _result(self, question: str, match: _ScopeMatch) -> VerifiedCalculation:
        """Build a cited calculation contract from a proven scope match."""

        narrow_values, broad_values = match.narrow.keyed_values, match.broad.keyed_values
        target_label, narrow_value = narrow_values[match.target_key]
        broad_value = broad_values[match.target_key][1]
        components = [narrow_values[key] for key in match.component_keys]
        component_text = " + ".join(_display_number(value) for _, value in components)
        formula = f"{_display_number(narrow_value)} + {component_text} = {_display_number(broad_value)}"
        aggregate_source = "图表" if match.broad.source_kind == "chart" else "表格"
        detail_source = "表格" if match.narrow.source_kind == "table" else "图表"
        included_zh = "、".join(f"“{label}”（{_display_number(value)}）" for label, value in components)
        if re.search(r"[\u4e00-\u9fff]", question):
            text = (
                f"不矛盾。{aggregate_source}与{detail_source}的成员集合不同：{aggregate_source}将{included_zh}并入"
                f"“{target_label}”。因此，“{target_label}”在{detail_source}明细口径下为{_display_number(narrow_value)}，"
                f"在{aggregate_source}聚合口径下为{_display_number(broad_value)}；{formula}。[1][2]"
            )
        else:
            aggregate_en = "chart" if match.broad.source_kind == "chart" else "table"
            detail_en = "table" if match.narrow.source_kind == "table" else "chart"
            included_en = ", ".join(f'"{label}" ({_display_number(value)})' for label, value in components)
            text = (
                f"The figures are not contradictory. The {aggregate_en} uses a broader member set: it folds "
                f"{included_en} into \"{target_label}\". \"{target_label}\" is {_display_number(narrow_value)} on the "
                f"{detail_en}'s detailed basis and {_display_number(broad_value)} on the {aggregate_en}'s aggregated basis; "
                f"{formula}. [1][2]"
            )
        evidence = (self._projection_evidence(match.narrow), self._projection_evidence(match.broad))
        scope = self._scope(match, target_label, components)
        fact = {
            "operation": "aggregation_scope_reconciliation",
            "metric": scope["metric"],
            "target_label": target_label,
            "narrow_value": narrow_value,
            "included_components": {label: value for label, value in components},
            "aggregate_value": broad_value,
            "formula": formula,
            "basis": "derived_from_member_set_difference",
        }
        return VerifiedCalculation(
            text=text,
            evidence=evidence,
            facts=(fact,),
            kind="scope_reconciliation",
            scope=scope,
            fallback_text=text,
        )

    @staticmethod
    def _scope(match: _ScopeMatch, target_label: str, components: list[tuple[str, float]]) -> dict[str, Any]:
        """Describe both source scopes and the inferred member inclusion."""

        rows = [*match.narrow.rows, *match.broad.rows]
        sources = (match.narrow.source or {}, match.broad.source or {})
        document_ids = (*(source.get("document_id") for source in sources), *(row.get("document_id") for row in rows))
        slide_ids = (*(source.get("slide_id") for source in sources), *(row.get("slide_id") for row in rows))
        return {
            "kind": "cross_source_reconciliation",
            "operation": "aggregation_scope_reconciliation",
            "metric": match.narrow.metric or match.broad.metric,
            "target_label": target_label,
            "narrow_source": match.narrow.source_kind,
            "broad_source": match.broad.source_kind,
            "included_labels": [label for label, _ in components],
            "stable_labels": [match.narrow.keyed_values[key][0] for key in match.stable_keys],
            "document_ids": sorted({str(value) for value in document_ids if value}),
            "slide_ids": sorted({str(value) for value in slide_ids if value}),
        }

    @staticmethod
    def _projection_evidence(projection: _DimensionProjection) -> dict[str, Any]:
        """Build provenance-preserving evidence for one source projection."""

        if projection.source_kind == "chart":
            assert projection.cube is not None and projection.rows
            structure = "; ".join(f"{label}={_display_number(value)}" for label, value in projection.values)
            return _bundle_evidence(
                projection.cube,
                list(projection.rows),
                operation="aggregation_scope_reconciliation",
                quote=f"{projection.metric}: {structure}",
            )
        source = projection.source or {}
        try:
            bbox = json.loads(str(source.get("bbox_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            bbox = {}
        return {
            "document_version_id": source.get("document_version_id"),
            "document_id": source.get("document_id"),
            "slide_id": source.get("slide_id"),
            "element_id": source.get("element_id"),
            "chunk_id": source.get("id"),
            "quote": str(source.get("content") or ""),
            "bbox": bbox if isinstance(bbox, dict) else {},
            "confidence": float(source.get("confidence") or source.get("element_confidence") or 1.0),
            "source_kind": source.get("source_kind") or "native_ooxml",
            "document_title": source.get("document_title"),
            "slide_no": source.get("slide_no"),
            "slide_title": source.get("slide_title"),
            "content_role": "table",
            "facet": "verified scope reconciliation",
            "extraction": "native_table_reconciliation",
            "numeric_identities": [
                {
                    "metric": projection.metric,
                    "period": label,
                    "value": value,
                    "unit": projection.unit_family,
                    "source_type": "native_table_cell",
                }
                for label, value in projection.values
            ],
            "calculation_operation": "aggregation_scope_reconciliation",
        }
