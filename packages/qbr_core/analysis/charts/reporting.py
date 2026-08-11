from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any


def _number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _weakened(item: Any) -> bool:
    return bool(
        item.category_mode == "temporal"
        and (
            (item.recent_change_percent is not None and item.recent_change_percent < 0)
            or (
                item.latest_vs_peak_percent is not None
                and item.latest_vs_peak_percent <= -3
                and item.periods_since_maximum <= 5
                and item.trailing_declines >= 2
            )
        )
    )


def _memberships(summaries: list[Any], *, chinese: bool) -> str:
    by_family: dict[str, list[str]] = defaultdict(list)
    for summary in summaries:
        by_family[summary.family].append(summary.name)
    labels = (
        {"bar": "柱状系列为", "line": "折线系列为", "share": "占比系列为", "other": "其他系列为"}
        if chinese
        else {"bar": "bars/columns are ", "line": "lines are ", "share": "share series are ", "other": "other series are "}
    )
    separator = "、" if chinese else ", "
    joiner = "；" if chinese else "; "
    return joiner.join(labels[family] + separator.join(by_family[family]) for family in labels if by_family[family])


def _fact(facts: list[dict[str, Any]], operation: str) -> dict[str, Any] | None:
    return next((item for item in facts if item.get("operation") == operation), None)


def render_chart_fallback(question: str, summaries: list[Any], facts: list[dict[str, Any]]) -> str:
    """Render computed chart facts without adding business causality or directionality."""

    chinese = bool(re.search(r"[\u4e00-\u9fff]", question))
    bars = [item for item in summaries if item.family == "bar"]
    lines = [item for item in summaries if item.family == "line"]
    temporal = [item for item in summaries if item.category_mode == "temporal"]
    categorical = [item for item in summaries if item.category_mode == "categorical"]
    aggregate = _fact(facts, "aggregate_series")
    composition = _fact(facts, "share_composition")
    path = _fact(facts, "sequential_path")
    weak = [item for item in lines if _weakened(item)]
    rising = [
        item
        for item in lines
        if item not in weak and item.category_mode == "temporal" and (item.year_over_year_change_percent or 0) >= 0
    ]

    if chinese:
        output = ["基于目标图表的原生数据，可以确认：", "", "- 图表构成：" + _memberships(summaries, chinese=True) + "。[1]"]
        if aggregate:
            shares = dict(aggregate.get("latest_shares_percent") or {})
            leaders = sorted(shares.items(), key=lambda item: item[1], reverse=True)[:2]
            recent = aggregate.get("recent_change_percent")
            output.append(
                f"- 产出结构：合计从{aggregate['start_period']}的{_number(float(aggregate['start_value']))}"
                f"变为{aggregate['latest_period']}的{_number(float(aggregate['latest_value']))}，"
                f"最近3期均值较此前3期变化{'不适用' if recent is None else f'{float(recent):.1f}%'}。"
                + ("最新占比最高的是" + "、".join(f"{name}（{share:.1f}%）" for name, share in leaders) + "。" if leaders else "")
                + "[1]"
            )
        if composition:
            segments = dict(composition.get("segments") or {})
            largest = dict(composition.get("largest_segment") or {})
            output.append(
                "- 占比结构："
                + "、".join(f"{name}={_number(float(value))}" for name, value in segments.items())
                + f"；最大部分为{largest.get('category')}（{_number(float(largest.get('value') or 0))}）。[1]"
            )
        if path:
            decline = dict(path.get("largest_decline") or {})
            output.append(
                f"- 路径归因：从“{path.get('start_category')}”的{_number(float(path.get('start_value') or 0))}"
                f"到“{path.get('end_category')}”的{_number(float(path.get('end_value') or 0))}，"
                f"净变化{_number(float(path.get('net_change') or 0))}；最大单步下降为"
                f"{decline.get('from')}→{decline.get('to')}（{_number(float(decline.get('delta') or 0))}）。[1]"
            )
        if rising:
            output.append("- 总体上行的折线系列：" + "、".join(item.name for item in rising) + "。[1]")
        if weak:
            details = [
                f"{item.name}最近3期均值较此前3期{item.recent_change_percent:.1f}%"
                if item.recent_change_percent is not None and item.recent_change_percent < 0
                else f"{item.name}较{item.maximum_period}峰值回撤{abs(item.latest_vs_peak_percent or 0):.1f}%"
                for item in weak
            ]
            prefix = "- 需要关注的近期方向背离：总产出仍增长，但" if aggregate else "- 近期走弱的折线系列："
            output.append(prefix + "；".join(details) + "。这是描述性信号，不代表因果关系。[1]")
        if temporal and not aggregate and not lines:
            output.append(
                "- 时间变化："
                + "；".join(
                    f"{item.name}从{item.start_period}的{_number(item.start_value)}变为"
                    f"{item.latest_period}的{_number(item.latest_value)}（{item.relative_change_percent:.1f}%）"
                    for item in temporal
                    if item.relative_change_percent is not None
                )
                + "。[1]"
            )
        if categorical and not composition and not path:
            output.append(
                "- 分类/情景比较："
                + "；".join(
                    f"{item.name}从“{item.start_period}”的{_number(item.start_value)}变为"
                    f"“{item.latest_period}”的{_number(item.latest_value)}（{item.relative_change_percent:.1f}%）"
                    for item in categorical
                    if item.relative_change_percent is not None
                )
                + "。[1]"
            )
            output.append("- 解释边界：横轴是分类或情景而非时间，不能表述为近期趋势。[1]")
        elif bars and lines:
            output.append("- 解释边界：柱状系列与折线系列没有文档声明的一一对应关系，不能直接配对或推断因果。[1]")
        else:
            output.append("- 解释边界：不同系列若量纲不同，不应直接求和或据此推断因果。[1]")
        return "\n".join(output)

    output = ["Based on the selected chart's native data:", "", "- Chart membership: " + _memberships(summaries, chinese=False) + ". [1]"]
    if aggregate:
        shares = dict(aggregate.get("latest_shares_percent") or {})
        leaders = sorted(shares.items(), key=lambda item: item[1], reverse=True)[:2]
        output.append(
            f"- Output moved from {_number(float(aggregate['start_value']))} in {aggregate['start_period']} to "
            f"{_number(float(aggregate['latest_value']))} in {aggregate['latest_period']}. Latest leaders were "
            + ", ".join(f"{name} ({share:.1f}%)" for name, share in leaders)
            + ". [1]"
        )
    if rising:
        output.append("- Generally rising line series: " + ", ".join(item.name for item in rising) + ". [1]")
    if weak:
        output.append(
            "- Recently weaker line series: " + ", ".join(item.name for item in weak) + ". This is descriptive, not causal. [1]"
        )
    if categorical and not composition and not path:
        output.append("- Boundary: the x-axis is categorical or scenario-based, not a timeline. [1]")
    elif bars and lines:
        output.append("- Boundary: the chart does not declare one-to-one mappings or causality between bar and line series. [1]")
    else:
        output.append("- Boundary: series with different units should not be summed, and descriptive co-movement is not causality. [1]")
    return "\n".join(output)
