from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from packages.qbr_core.planning.models import QueryPlan

_ENGLISH_CARDINALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True, slots=True)
class ChartRequestSignature:
    """Language-neutral structural hints extracted from the user's task frame."""

    tokens: frozenset[str]
    series_counts: tuple[int, ...]
    point_counts: tuple[int, ...]
    requested_families: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "series_counts": list(self.series_counts),
            "point_counts": list(self.point_counts),
            "requested_families": list(self.requested_families),
        }


def chart_family(chart_type: str) -> str:
    folded = chart_type.casefold()
    if "bar" in folded or "column" in folded:
        return "bar"
    if "line" in folded or "scatter" in folded:
        return "line"
    if "pie" in folded or "doughnut" in folded:
        return "share"
    return "other"


def is_temporal_category(value: str) -> bool:
    folded = value.strip().casefold()
    return bool(
        re.fullmatch(r"(?:fy|cy)?20\d{2}[aef]?", folded)
        or re.fullmatch(r"\d{2,4}[/.-]\d{1,2}", folded)
        or re.fullmatch(r"(?:q[1-4][ -]?(?:20)?\d{2}|(?:20)?\d{2}[ -]?q[1-4])", folded)
    )


def question_tokens(question: str) -> set[str]:
    folded = question.casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9%_-]{2,}", folded))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        tokens.add(phrase)
        tokens.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return tokens


def _chinese_cardinal(text: str) -> int | None:
    if not text or any(character not in {*_CHINESE_DIGITS, "十"} for character in text):
        return None
    if "十" not in text:
        value = 0
        for character in text:
            value = value * 10 + _CHINESE_DIGITS[character]
        return value
    tens, ones = text.split("十", 1)
    return (_CHINESE_DIGITS.get(tens, 1) * 10) + (_CHINESE_DIGITS.get(ones, 0) if ones else 0)


def _number_value(token: str) -> int | None:
    folded = token.casefold()
    if folded.isdigit():
        return int(folded)
    if folded in _ENGLISH_CARDINALS:
        return _ENGLISH_CARDINALS[folded]
    return _chinese_cardinal(token)


def request_signature(plan: QueryPlan) -> ChartRequestSignature:
    """Extract chart structure without deck, industry or KPI vocabulary."""

    text = " ".join(
        dict.fromkeys(
            item
            for item in (
                plan.original_question,
                plan.canonical_question,
                plan.task_summary,
                *plan.operations,
                *plan.evidence_requirements,
            )
            if item
        )
    )
    number_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|[零〇一二两三四五六七八九十]{1,3})(?![A-Za-z0-9])",
        re.I,
    )
    duration_spans: set[tuple[int, int]] = set()
    point_counts: list[int] = []
    duration_pattern = re.compile(
        number_pattern.pattern
        + r"[\s-]*(?:个?(?:月|季度|年度点|时间点|情景|场景|类别|数据点)|月度|期|"
        + r"months?|periods?|quarters?|scenarios?|categories?|data[ -]?points?)",
        re.I,
    )
    for match in duration_pattern.finditer(text):
        number_match = number_pattern.search(match.group(0))
        value = _number_value(number_match.group(0)) if number_match else None
        if value is not None and 1 < value <= 120:
            point_counts.append(value)
            duration_spans.add((match.start() + number_match.start(), match.start() + number_match.end()))

    series_counts: list[int] = []
    for match in number_pattern.finditer(text):
        if (match.start(), match.end()) in duration_spans:
            continue
        before = text[max(0, match.start() - 1) : match.start()]
        after = text[match.end() : match.end() + 2]
        if "/" in before + after or "%" in after or re.match(r"\s*(?:年|year|m\b)", text[match.end() :], re.I):
            continue
        value = _number_value(match.group(0))
        if value is not None and 1 < value <= 50:
            series_counts.append(value)

    requested_families: list[str] = []
    family_patterns = {
        "bar": r"(?:柱(?:状|形)?(?:图|系列)?|bars?|columns?)",
        "line": r"(?:折线|曲线|lines?)",
        "share": r"(?:饼(?:图)?|环形图|pies?|doughnuts?)",
    }
    for family, pattern in family_patterns.items():
        if re.search(pattern, text, re.I):
            requested_families.append(family)

    visual = plan.visual_structure if isinstance(plan.visual_structure, dict) else {}
    structured_counts = tuple(int(value) for value in visual.get("series_group_counts", []) if isinstance(value, int))
    structured_points = tuple(int(value) for value in visual.get("point_counts", []) if isinstance(value, int))
    structured_families = tuple(str(value) for value in visual.get("chart_families", []))
    return ChartRequestSignature(
        tokens=frozenset(question_tokens(text)),
        series_counts=structured_counts or tuple(series_counts),
        point_counts=structured_points or tuple(dict.fromkeys(point_counts)),
        requested_families=structured_families or tuple(requested_families),
    )


def _bbox(row: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = row.get("bbox_json") or row.get("bbox") or {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        x = float(value.get("x", value.get("left")))
        y = float(value.get("y", value.get("top")))
        width = float(value.get("w", value.get("width")))
        height = float(value.get("h", value.get("height")))
    except (TypeError, ValueError):
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


def _overlap_ratio(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    left_x, left_y, left_w, left_h = left
    right_x, right_y, right_w, right_h = right
    intersection_w = max(0.0, min(left_x + left_w, right_x + right_w) - max(left_x, right_x))
    intersection_h = max(0.0, min(left_y + left_h, right_y + right_h) - max(left_y, right_y))
    intersection = intersection_w * intersection_h
    return intersection / min(left_w * left_h, right_w * right_h)


def chart_scopes(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group overlaid native charts while keeping unrelated same-slide charts apart."""

    by_slide: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        slide_key = str(row.get("slide_id") or row.get("chart_id") or row.get("element_id") or "")
        if slide_key:
            by_slide[slide_key].append(row)

    scopes: dict[str, list[dict[str, Any]]] = {}
    for slide_key, slide_rows in by_slide.items():
        by_element: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in slide_rows:
            element_key = str(row.get("element_id") or row.get("chart_id") or "")
            by_element[element_key].append(row)
        elements = list(by_element)
        boxes = {element: _bbox(by_element[element][0]) for element in elements}
        if len(elements) <= 1 or sum(box is not None for box in boxes.values()) < 2:
            scopes[slide_key] = slide_rows
            continue

        parent = {element: element for element in elements}

        def find(element: str, _parent: dict[str, str] = parent) -> str:
            while _parent[element] != element:
                _parent[element] = _parent[_parent[element]]
                element = _parent[element]
            return element

        def union(
            left: str,
            right: str,
            _parent: dict[str, str] = parent,
            _find: Any = find,
        ) -> None:
            left_root, right_root = _find(left), _find(right)
            if left_root != right_root:
                _parent[right_root] = left_root

        for index, left in enumerate(elements):
            for right in elements[index + 1 :]:
                if boxes[left] is not None and boxes[right] is not None and _overlap_ratio(boxes[left], boxes[right]) >= 0.8:
                    union(left, right)

        components: dict[str, list[str]] = defaultdict(list)
        for element in elements:
            components[find(element)].append(element)
        for component_index, component in enumerate(components.values(), 1):
            scopes[f"{slide_key}:chart_cluster_{component_index}"] = [
                row for element in component for row in by_element[element]
            ]
    return scopes


def series_groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row.get("series_id") or f"{row.get('element_id')}:{row.get('series_name')}")
        grouped[key].append(row)
    return [
        sorted(group, key=lambda item: int(item.get("point_order") or 0))
        for group in grouped.values()
        if any(item.get("y_value") is not None for item in group)
    ]
