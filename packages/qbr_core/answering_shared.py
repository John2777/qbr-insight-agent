from __future__ import annotations

import json
from typing import Any


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _format_chart_value(row: dict[str, Any]) -> str:
    display = str(row.get("display_value") or "").strip()
    if display:
        return display if "%" in display else f"{display}%"
    return f"{float(row['y_value']):g}%"


PERFORMANCE_TERMS = (
    "executive",
    "snapshot",
    "summary",
    "业绩",
    "经营",
    "表现",
    "增长",
    "收入",
    "营收",
    "利润",
    "盈利",
    "现金",
    "价值",
    "财务",
    "指标",
    "kpi",
    "revenue",
    "profit",
    "margin",
    "growth",
    "sales",
    "actual",
    "target",
    "同比",
    "环比",
    "达成",
    "完成",
    "趋势",
    "亮点",
)
