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


def _percent(value: float | None, *, unavailable: str) -> str:
    return unavailable if value is None else f"{value:+.1f}%"


def _trend_detail(item: Any, *, chinese: bool) -> str:
    measures: list[str] = []
    if item.relative_change_percent is not None:
        measures.append(("较起点" if chinese else "vs start ") + f"{item.relative_change_percent:+.1f}%")
    if item.year_over_year_change_percent is not None:
        measures.append(("同比" if chinese else "YoY ") + f"{item.year_over_year_change_percent:+.1f}%")
    if item.recent_change_percent is not None:
        measures.append(
            ("近3期均值" if chinese else "recent 3 vs prior 3 ") + f"{item.recent_change_percent:+.1f}%"
        )
    separator = "、" if chinese else ", "
    return item.name + (f"（{separator.join(measures)}）" if measures else "")


def _direction_counts(items: list[Any]) -> tuple[list[Any], list[Any]]:
    comparable = [item for item in items if item.relative_change_percent is not None]
    return (
        [item for item in comparable if item.relative_change_percent > 0],
        [item for item in comparable if item.relative_change_percent < 0],
    )


def render_chart_fallback(question: str, summaries: list[Any], facts: list[dict[str, Any]]) -> str:
    """Render a dense, evidence-bounded analysis from generic chart statistics."""

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
    rising_bars, falling_bars = _direction_counts([item for item in bars if item.category_mode == "temporal"])

    if chinese:
        output = ["基于目标图表的原生数据，可以确认：", "", "- 图表构成：" + _memberships(summaries, chinese=True) + "。[1]"]
        if aggregate:
            shares = dict(aggregate.get("latest_shares_percent") or {})
            leaders = sorted(shares.items(), key=lambda item: item[1], reverse=True)[:2]
            recent = aggregate.get("recent_change_percent")
            relative = aggregate.get("relative_change_percent")
            yoy = aggregate.get("year_over_year_change_percent")
            aggregate_direction = "扩张" if isinstance(relative, int | float) and relative > 0 else "收缩"
            if weak:
                quality_signal = f"{len(lines)}项折线中{len(rising)}项总体上行、{len(weak)}项近期走弱，折线信号并不同步"
            elif rising:
                quality_signal = f"{len(rising)}项折线总体上行"
            else:
                quality_signal = "折线信号未形成一致方向"
            output.append(f"- 核心判断：柱状合计总体{aggregate_direction}，但{quality_signal}；因此应把规模变化与其他指标变化分层解读。[1]")
            output.append(
                f"- 规模节奏：合计从{aggregate['start_period']}的{_number(float(aggregate['start_value']))}"
                f"变为{aggregate['latest_period']}的{_number(float(aggregate['latest_value']))}，"
                f"累计变化{_percent(float(relative) if relative is not None else None, unavailable='不适用')}、"
                f"同比{_percent(float(yoy) if yoy is not None else None, unavailable='不适用')}、"
                f"最近3期均值较此前3期{_percent(float(recent) if recent is not None else None, unavailable='不适用')}。[1]"
            )
            if leaders:
                output.append(
                    "- 最新结构：占比最高的是"
                    + "、".join(f"{name}（{share:.1f}%）" for name, share in leaders)
                    + f"，前两项合计{sum(float(share) for _, share in leaders):.1f}%。[1]"
                )
        if rising_bars or falling_bars:
            strongest = sorted(
                rising_bars,
                key=lambda item: item.relative_change_percent if item.relative_change_percent is not None else -math.inf,
                reverse=True,
            )[:2]
            breadth = f"{len(rising_bars)}/{len(rising_bars) + len(falling_bars)}个柱状系列高于起点"
            leaders = "；累计增幅领先的是" + "、".join(_trend_detail(item, chinese=True) for item in strongest) if strongest else ""
            output.append(f"- 增长广度：{breadth}{leaders}。[1]")
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
            output.append("- 折线中的改善信号：" + "；".join(_trend_detail(item, chinese=True) for item in rising) + "。[1]")
        if weak:
            details = [
                _trend_detail(item, chinese=True)
                + (
                    f"，较{item.maximum_period}峰值回撤{abs(item.latest_vs_peak_percent):.1f}%"
                    if item.latest_vs_peak_percent is not None and item.latest_vs_peak_percent < 0
                    else ""
                )
                for item in weak
            ]
            prefix = "- 需要关注的分化：总产出仍增长，但" if aggregate else "- 近期走弱的折线系列："
            output.append(prefix + "；".join(details) + "。[1]")
        if aggregate and weak:
            output.append(
                "- 有边界的推断：规模扩张尚未获得所有折线指标同步确认，可视为需要进一步拆解的分化信号；"
                "仅凭同图共变无法确定原因，需补充系列口径、分组明细及驱动数据验证。[1]"
            )
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
        relative = aggregate.get("relative_change_percent")
        yoy = aggregate.get("year_over_year_change_percent")
        recent = aggregate.get("recent_change_percent")
        direction = "expanded" if isinstance(relative, int | float) and relative > 0 else "contracted"
        output.append(
            f"- Core finding: aggregate bar/column output {direction}, while {len(rising)} line series generally rose and "
            f"{len(weak)} weakened recently; scale and line signals should be read separately. [1]"
        )
        output.append(
            f"- Output moved from {_number(float(aggregate['start_value']))} in {aggregate['start_period']} to "
            f"{_number(float(aggregate['latest_value']))} in {aggregate['latest_period']} "
            f"(cumulative {_percent(float(relative) if relative is not None else None, unavailable='n/a')}, "
            f"YoY {_percent(float(yoy) if yoy is not None else None, unavailable='n/a')}, "
            f"recent {_percent(float(recent) if recent is not None else None, unavailable='n/a')}). [1]"
        )
        if leaders:
            output.append(
                "- Latest concentration: "
                + ", ".join(f"{name} ({share:.1f}%)" for name, share in leaders)
                + f"; top two combined {sum(float(share) for _, share in leaders):.1f}%. [1]"
            )
    if rising_bars or falling_bars:
        output.append(
            f"- Growth breadth: {len(rising_bars)}/{len(rising_bars) + len(falling_bars)} bar series were above "
            "their starting levels. [1]"
        )
    if rising:
        output.append("- Improving line signals: " + "; ".join(_trend_detail(item, chinese=False) for item in rising) + ". [1]")
    if weak:
        output.append(
            "- Divergent line signals: " + "; ".join(_trend_detail(item, chinese=False) for item in weak) + ". [1]"
        )
    if aggregate and weak:
        output.append(
            "- Bounded inference: scale growth is not confirmed by every line indicator. This is descriptive "
            "divergence, not causality; definitions and disaggregated driver data are needed to test explanations. [1]"
        )
    if categorical and not composition and not path:
        output.append("- Boundary: the x-axis is categorical or scenario-based, not a timeline. [1]")
    elif bars and lines:
        output.append("- Boundary: the chart does not declare one-to-one mappings or causality between bar and line series. [1]")
    else:
        output.append("- Boundary: series with different units should not be summed, and descriptive co-movement is not causality. [1]")
    return "\n".join(output)
