from __future__ import annotations

import re
from typing import Any

from .answering_shared import _format_chart_value, _loads


class ChartAnswerMixin:
    def _slide_takeaway(self, slide: dict[str, Any]) -> str:
        title = (slide.get("title") or "").strip()
        parts = [part.strip() for part in re.split(r"\s+·\s+", slide.get("summary") or "") if part.strip()]
        candidates = [
            part.replace("\n", " ")
            for part in parts
            if part != title and not part.startswith("QBR ") and not re.fullmatch(r"\d{1,3}", part) and len(part) >= 8 and len(part) <= 220
        ]
        if not candidates:
            return title
        candidates.sort(key=lambda part: (self._performance_score(part), len(part)), reverse=True)
        takeaway = candidates[0]
        if len(takeaway) > 180:
            takeaway = takeaway[:177].rstrip() + "…"
        return takeaway

    def _slide_summary_evidence(self, slide: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": slide["document_version_id"],
            "slide_id": slide["slide_id"],
            "element_id": None,
            "chunk_id": None,
            "quote": (slide.get("summary") or slide.get("title") or "")[:500],
            "bbox": {},
            "confidence": 1.0,
            "source_kind": "native_ooxml",
            "document_title": slide.get("document_title"),
            "slide_no": slide.get("slide_no"),
        }

    def _series_evidence(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
        first = ordered[0]
        point_labels = []
        for row in ordered:
            value = row.get("display_value") or f"{float(row['y_value']):g}"
            point_labels.append(f"{row.get('category') or row.get('point_order')}={value}")
        points = ", ".join(point_labels)
        return {
            "document_version_id": first["document_version_id"],
            "slide_id": first["slide_id"],
            "element_id": first["element_id"],
            "chunk_id": None,
            "quote": f"{first.get('chart_title') or 'Chart'} — {first.get('series_name') or 'Series'}: {points}"[:500],
            "bbox": _loads(first.get("bbox_json"), {}),
            "confidence": min(
                float(first.get("chart_confidence") or 0),
                float(first.get("series_confidence") or 0),
                *(float(row.get("confidence") or 0) for row in ordered),
            ),
            "source_kind": first["source_kind"],
            "document_title": first.get("document_title"),
            "slide_no": first.get("slide_no"),
        }

    def _rank_chart_points(self, question: str, rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
        folded = question.casefold()
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            score = 0
            axis = self._axis_metadata_from_row(row)
            for value, weight in [
                (row.get("series_name"), 3),
                (row.get("category"), 3),
                (row.get("chart_title"), 2),
                (axis.get("title"), 2),
                (row.get("document_title"), 1),
            ]:
                if value and str(value).casefold() in folded:
                    score += weight
            asks_secondary = any(term in folded for term in ("右轴", "次轴", "副轴", "secondary", "right axis"))
            asks_primary = any(term in folded for term in ("左轴", "主轴", "primary", "left axis"))
            if asks_secondary:
                score += 4 if axis.get("role") == "secondary" else -4
            if asks_primary:
                score += 4 if axis.get("role") == "primary" else -4
            if any(term in folded for term in ["多少", "数值", "value", "增长", "变化", "环比", "同比"]):
                score += 1
            ranked.append((score, row))
        return sorted(ranked, key=lambda pair: (pair[0], pair[1].get("point_order", 0)), reverse=True)

    @staticmethod
    def _axis_metadata_from_row(row: dict[str, Any]) -> dict[str, Any]:
        visual = _loads(row.get("visual_json"), {})
        if isinstance(visual, dict) and isinstance(visual.get("axis"), dict):
            return dict(visual["axis"])
        axes = _loads(row.get("axes_json"), [])
        if isinstance(axes, list):
            for axis in axes:
                if isinstance(axis, dict) and str(axis.get("axis_id")) == str(row.get("axis_id")):
                    return axis
        return {}

    def _axis_descriptor(self, row: dict[str, Any]) -> str:
        axis = self._axis_metadata_from_row(row)
        slide_context = f"{row.get('slide_title') or ''} {row.get('slide_summary') or ''}".casefold()
        chart_type = str(row.get("chart_type") or "").casefold()
        inferred_secondary = any(term in slide_context for term in ("双轴", "右轴", "secondary")) and "line" in chart_type
        parts: list[str] = []
        if axis.get("role") == "secondary" or inferred_secondary:
            parts.append("右侧次轴")
        elif axis.get("role") == "primary":
            parts.append("左侧主轴")
        if axis.get("title"):
            parts.append(str(axis["title"]))
        if axis.get("display_units"):
            parts.append(f"显示单位 {axis['display_units']}")
        if row.get("unit"):
            parts.append(f"格式/单位 {row['unit']}")
        return "；".join(parts) or "原图未提供明确单位"

    def _chart_comparison_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        folded = question.casefold()
        if not any(term in folded for term in ("哪个更高", "高多少", "低多少", "增加了多少", "增幅约")):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            series = str(row.get("series_name") or "")
            category = str(row.get("category") or "")
            if row.get("y_value") is None or not series or not category:
                continue
            if series.casefold() in folded and category.casefold() in folded:
                groups.setdefault(str(row.get("element_id")), []).append(row)
        candidates = [rows for rows in groups.values() if len({row.get("series_id") for row in rows}) >= 2]
        if not candidates:
            return None
        rows = max(candidates, key=len)
        unique: dict[str, dict[str, Any]] = {str(row.get("series_id")): row for row in rows}
        rows = list(unique.values())
        rows.sort(key=lambda row: folded.find(str(row.get("series_name") or "").casefold()))
        if len(rows) < 2:
            return None
        first, second = rows[0], rows[1]
        first_value, second_value = float(first["y_value"]), float(second["y_value"])
        first_label = f"{first.get('series_name')} {first.get('category')}"
        second_label = f"{second.get('series_name')} {second.get('category')}"
        if any(term in folded for term in ("增加了多少", "增幅约", "从")):
            delta = second_value - first_value
            rate = delta / abs(first_value) * 100 if first_value else None
            rate_text = f"，相对增幅约{rate:.1f}%" if rate is not None else "，基期为0，无法计算相对增幅"
            answer = f"{first_label}为{first_value:g}，{second_label}为{second_value:g}；增加{delta:g}{rate_text}。 [1][2]"
        else:
            higher, lower = (
                max((first, second), key=lambda row: float(row["y_value"])),
                min((first, second), key=lambda row: float(row["y_value"])),
            )
            difference = float(higher["y_value"]) - float(lower["y_value"])
            answer = (
                f"{first_label}为{first_value:g}，{second_label}为{second_value:g}；"
                f"{higher.get('series_name')}更高，高{difference:g}。 [1][2]"
            )
        return answer, [self._point_evidence(first), self._point_evidence(second)], []

    def _chart_extreme_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        operation = next((term for term in ("最高", "最大", "最低", "最小") if term in question), None)
        if operation is None or not any(term in question for term in ("图", "月度", "控制图")):
            return None
        folded = question.casefold()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            series = str(row.get("series_name") or "")
            if row.get("y_value") is not None and series and series.casefold() in folded:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        if not groups:
            return None
        rows = max(groups.values(), key=len)
        reverse = operation in {"最高", "最大"}
        selected = sorted(rows, key=lambda row: float(row["y_value"]), reverse=reverse)[0]
        value = selected.get("display_value") or f"{float(selected['y_value']):g}"
        answer = f"{selected.get('series_name')}在{selected.get('category')}达到{operation}值{value}。 [1]"
        return answer, [self._point_evidence(selected)], []

    def _composition_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        """Answer part-to-whole chart questions from a complete native series."""
        if not (any(term in question for term in ("构成", "组合")) and "占" in question):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        candidates: list[tuple[int, list[dict[str, Any]]]] = []
        for rows in groups.values():
            ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 3:
                continue
            total = sum(float(row["y_value"]) for row in ordered)
            if not 98 <= total <= 102:
                continue
            context = " ".join(
                str(ordered[0].get(key) or "") for key in ("chart_title", "series_name", "slide_title", "slide_summary", "document_title")
            )
            score = sum(3 for term in ("渠道", "组合", "触点", "代理") if term in question and term in context)
            score += sum(2 for row in ordered if str(row.get("category") or "") in question)
            candidates.append((score, ordered))
        if not candidates:
            return None
        _, rows = max(candidates, key=lambda item: (item[0], len(item[1])))
        agent = next((row for row in rows if "代理" in str(row.get("category") or "")), rows[0])
        others = [row for row in rows if row is not agent]
        agent_value = float(agent["y_value"])
        non_agent = sum(float(row["y_value"]) for row in others)
        agent_label = str(agent.get("category") or "代理触点")
        detail = "、".join(f"{row.get('category')} {_format_chart_value(row)}" for row in others)
        answer = f"{agent_label}（代理触点）占{agent_value:g}%，非代理触点合计占{non_agent:g}%；非代理触点由{detail}构成。 [1]"
        return answer, [self._series_evidence(rows)], []

    def _max_drop_answer(
        self,
        question: str,
        chart_rows: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]] | None:
        """Compute the largest adjacent decline while retaining slide context."""
        if not any(term in question for term in ("降幅最大", "最大降幅", "下降最多")):
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in chart_rows:
            if row.get("y_value") is not None:
                groups.setdefault(str(row.get("series_id")), []).append(row)
        candidates: list[tuple[int, float, dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
        for rows in groups.values():
            ordered = sorted(rows, key=lambda row: int(row.get("point_order") or 0))
            if len(ordered) < 2:
                continue
            context = " ".join(
                str(ordered[0].get(key) or "") for key in ("chart_title", "series_name", "slide_title", "slide_summary", "document_title")
            )
            context_score = sum(4 for term in ("资本", "情景", "路径", "阶段") if term in question and term in context)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                drop = float(previous["y_value"]) - float(current["y_value"])
                if drop > 0:
                    candidates.append((context_score, drop, previous, current, ordered))
        if not candidates:
            return None
        _, drop, previous, current, rows = max(candidates, key=lambda item: (item[0], item[1]))
        answer = (
            f"最大降幅发生在“{current.get('category')}”这一步：阶段值从"
            f"{float(previous['y_value']):g}降至{float(current['y_value']):g}，下降{drop:g}。 [1]"
        )
        return answer, [self._series_evidence(rows)], []

    def _chart_answer(
        self,
        question: str,
        ranked: list[tuple[int, dict[str, Any]]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        best_score = ranked[0][0]
        selected = [row for score, row in ranked if score == best_score][:4]
        warnings: list[str] = []
        selected = [row for row in selected if row.get("y_value") is not None]
        if not selected:
            return "已定位到相关图表，但缺少可用于精确回答的数值。", [], ["INSUFFICIENT_EVIDENCE"]
        growth = any(term in question.casefold() for term in ["增长", "增幅", "增加", "变化", "环比", "同比", "change", "growth"])
        if growth:
            same_series = [
                row for _, row in ranked if row.get("series_id") == selected[0].get("series_id") and row.get("y_value") is not None
            ]
            unique = {int(row["point_order"]): row for row in same_series}
            ordered = [unique[key] for key in sorted(unique)]
            mentioned = [row for row in ordered if row.get("category") and str(row["category"]).casefold() in question.casefold()]
            pair = mentioned[-2:] if len(mentioned) >= 2 else ordered[-2:]
            if len(pair) == 2:
                previous, current = pair
                prior = float(previous["y_value"])
                current_value = float(current["y_value"])
                if prior == 0:
                    answer = (
                        f"{current['series_name']} 从 {previous['category']} 的 {prior:g} 变为 "
                        f"{current['category']} 的 {current_value:g}；基期为 0，增长率不可计算。 [1][2]"
                    )
                else:
                    change = (current_value - prior) / abs(prior) * 100
                    absolute_change = current_value - prior
                    answer = (
                        f"{current['series_name']} 从 {previous['category']} 的 {prior:g} 变为 "
                        f"{current['category']} 的 {current_value:g}，绝对变化为 {absolute_change:g}；"
                        f"按 `(本期-上期)/|上期|` 计算，相对变化为 {change:.1f}%。 [1][2]"
                    )
                evidence = [self._point_evidence(previous), self._point_evidence(current)]
                return answer, evidence, warnings
        folded = question.casefold()
        explicit_series: list[dict[str, Any]] = []
        seen_series: set[str] = set()
        for row in selected:
            series_name = str(row.get("series_name") or "")
            series_id = str(row.get("series_id") or "")
            if series_name and series_name.casefold() in folded and series_id not in seen_series:
                explicit_series.append(row)
                seen_series.add(series_id)
        if len(explicit_series) >= 2:
            evidence = [self._point_evidence(row) for row in explicit_series]
            lines = ["| 指标 | 类别 | 数值 | 坐标轴/单位 | 证据 |", "|---|---|---:|---|---|"]
            for index, row in enumerate(explicit_series, 1):
                value = row.get("display_value") or f"{float(row['y_value']):g}"
                lines.append(
                    f"| {row['series_name']} | {row.get('category') or row.get('point_order')} | "
                    f"{value} | {self._axis_descriptor(row)} | [{index}] |"
                )
            return "同一图表中相关系列如下：\n\n" + "\n".join(lines), evidence, warnings
        row = selected[0]
        value = row.get("display_value") or f"{float(row['y_value']):g}"
        unit_text = f"（{self._axis_descriptor(row)}）"
        if float(row.get("chart_confidence", 0)) < 0.85:
            warnings.append("LOW_CHART_CONFIDENCE")
            prefix = "视觉/低置信识别值约为"
        else:
            prefix = "为"
        answer = f"{row['series_name']} 在 {row.get('category') or row.get('point_order')} 的值{prefix} {value} {unit_text}。 [1]"
        return answer, [self._point_evidence(row)], warnings

    def _point_evidence(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"],
            "slide_id": row["slide_id"],
            "element_id": row["element_id"],
            "chunk_id": None,
            "quote": (
                f"{row.get('chart_title') or 'Chart'} — {row['series_name']}: "
                f"{row.get('category')}={row.get('display_value') or row.get('y_value')}"
            ),
            "bbox": _loads(row.get("bbox_json"), {}),
            "confidence": min(float(row["chart_confidence"]), float(row["series_confidence"]), float(row["confidence"])),
            "source_kind": row["source_kind"],
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
        }

    def _chunk_evidence(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"],
            "slide_id": row["slide_id"],
            "element_id": row.get("element_id"),
            "chunk_id": row["id"],
            "quote": row["content"][:500],
            "bbox": _loads(row.get("bbox_json"), {}),
            "confidence": 1.0,
            "source_kind": "native_ooxml",
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
        }
