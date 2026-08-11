from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .chart_structure import chart_family, is_temporal_category, series_groups


def normalize_dimension(value: object) -> str:
    """Normalize a chart label without maintaining a business alias dictionary."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)


def label_is_mentioned(label: object, question: str) -> bool:
    normalized = normalize_dimension(label)
    return bool(normalized and normalized in normalize_dimension(question))


def units_compatible(rows: Iterable[dict[str, Any]]) -> bool:
    units = {
        normalize_dimension(row.get("unit"))
        for row in rows
        if normalize_dimension(row.get("unit")) not in {"", "0", "00", "general"}
    }
    return len(units) <= 1


@dataclass(frozen=True, slots=True)
class ChartDataCube:
    """A chart-local two-dimensional view: series × category → value.

    The cube deliberately preserves source rows. Calculators can therefore use
    one generic coordinate model for temporal charts, transposed comparisons,
    share charts and ordered categorical paths while retaining provenance.
    """

    scope_id: str
    rows: tuple[dict[str, Any], ...]
    groups: tuple[tuple[dict[str, Any], ...], ...]

    @classmethod
    def from_rows(cls, scope_id: str, rows: Iterable[dict[str, Any]]) -> ChartDataCube | None:
        numeric = [row for row in rows if row.get("y_value") is not None]
        groups = tuple(tuple(group) for group in series_groups(numeric))
        if not groups:
            return None
        return cls(scope_id=scope_id, rows=tuple(numeric), groups=groups)

    @property
    def series_names(self) -> tuple[str, ...]:
        return tuple(str(group[0].get("series_name") or "") for group in self.groups)

    @property
    def category_names(self) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for group in self.groups:
            for row in group:
                name = str(row.get("category") or "")
                key = normalize_dimension(name)
                if key and key not in seen:
                    seen.add(key)
                    names.append(name)
        return tuple(names)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(chart_family(str(group[0].get("chart_type") or "")) for group in self.groups))

    @property
    def is_share(self) -> bool:
        if "share" in self.families:
            return True
        if len(self.groups) != 1:
            return False
        values = [float(row["y_value"]) for row in self.groups[0]]
        total = sum(values)
        return len(values) >= 2 and all(value >= 0 for value in values) and math.isclose(total, 100.0, abs_tol=2.0)

    @property
    def is_ordered_categorical(self) -> bool:
        if len(self.groups) != 1 or self.is_share or len(self.groups[0]) < 3:
            return False
        temporal = sum(is_temporal_category(str(row.get("category") or "")) for row in self.groups[0])
        return temporal < max(2, math.ceil(len(self.groups[0]) * 0.7))

    @property
    def analytical_entity_count(self) -> int:
        if len(self.groups) > 1:
            return len(self.groups)
        return len(self.groups[0]) if self.is_share or self.is_ordered_categorical else 1

    @property
    def is_analyzable(self) -> bool:
        return self.analytical_entity_count >= 2 and sum(len(group) for group in self.groups) >= 2

    def mentioned_series(self, question: str) -> tuple[str, ...]:
        return tuple(name for name in self.series_names if label_is_mentioned(name, question))

    def mentioned_categories(self, question: str) -> tuple[str, ...]:
        return tuple(name for name in self.category_names if label_is_mentioned(name, question))

    def point(self, series: str, category: str) -> dict[str, Any] | None:
        series_key, category_key = normalize_dimension(series), normalize_dimension(category)
        return next(
            (
                row
                for row in self.rows
                if normalize_dimension(row.get("series_name")) == series_key
                and normalize_dimension(row.get("category")) == category_key
            ),
            None,
        )

    def series_rows(self, series: str) -> tuple[dict[str, Any], ...]:
        key = normalize_dimension(series)
        return next(
            (group for group in self.groups if normalize_dimension(group[0].get("series_name")) == key),
            (),
        )

    def category_rows(self, category: str) -> tuple[dict[str, Any], ...]:
        key = normalize_dimension(category)
        return tuple(row for row in self.rows if normalize_dimension(row.get("category")) == key)

    def relevance(self, question: str) -> int:
        series_matches = len(self.mentioned_series(question))
        category_matches = len(self.mentioned_categories(question))
        title_fields = (
            self.rows[0].get("chart_title"),
            self.rows[0].get("slide_title"),
            self.rows[0].get("slide_summary"),
        )
        title_matches = sum(label_is_mentioned(value, question) for value in title_fields if value)
        return series_matches * 8 + category_matches * 8 + title_matches * 2

    def composition(self) -> dict[str, Any] | None:
        if not self.is_share:
            return None
        rows = self.groups[0]
        segments = {str(row.get("category") or ""): float(row["y_value"]) for row in rows}
        total = sum(segments.values())
        largest = max(segments.items(), key=lambda item: item[1])
        shares = {name: (value / total * 100 if total else 0.0) for name, value in segments.items()}
        return {
            "total": total,
            "segments": segments,
            "shares_percent": shares,
            "is_percent_scale": math.isclose(total, 100.0, abs_tol=2.0),
            "largest_segment": {"category": largest[0], "value": largest[1], "share_percent": shares[largest[0]]},
        }

    def sequential_path(self) -> dict[str, Any] | None:
        if not self.is_ordered_categorical:
            return None
        rows = self.groups[0]
        steps = [
            {
                "from": str(previous.get("category") or ""),
                "to": str(current.get("category") or ""),
                "delta": float(current["y_value"]) - float(previous["y_value"]),
            }
            for previous, current in zip(rows[:-1], rows[1:], strict=True)
        ]
        return {
            "start_category": str(rows[0].get("category") or ""),
            "start_value": float(rows[0]["y_value"]),
            "end_category": str(rows[-1].get("category") or ""),
            "end_value": float(rows[-1]["y_value"]),
            "net_change": float(rows[-1]["y_value"]) - float(rows[0]["y_value"]),
            "steps": steps,
            "largest_decline": min(steps, key=lambda item: float(item["delta"])),
            "largest_increase": max(steps, key=lambda item: float(item["delta"])),
        }


def chart_cubes(scopes: dict[str, list[dict[str, Any]]]) -> tuple[ChartDataCube, ...]:
    cubes = [ChartDataCube.from_rows(scope_id, rows) for scope_id, rows in scopes.items()]
    return tuple(cube for cube in cubes if cube is not None)
