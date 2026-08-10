from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

WarningSeverity = Literal["info", "warning", "degraded", "error"]


@dataclass(frozen=True, slots=True)
class WarningDescriptor:
    code: str
    severity: WarningSeverity
    category: str
    label: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_WARNING_CATALOG: dict[str, WarningDescriptor] = {
    "SYNTHETIC_DATA_SIGNAL": WarningDescriptor(
        code="SYNTHETIC_DATA_SIGNAL",
        severity="info",
        category="data_caveat",
        label="文档含模拟数据",
        description="回答引用的页面被文档自身标记为模拟、测试或示例数据；这不是系统故障。",
    ),
    "INSUFFICIENT_EVIDENCE": WarningDescriptor(
        code="INSUFFICIENT_EVIDENCE",
        severity="warning",
        category="evidence_quality",
        label="证据不足",
        description="当前文档中没有足够的相关业务证据支撑更明确的结论。",
    ),
    "PARTIAL_EVIDENCE_COVERAGE": WarningDescriptor(
        code="PARTIAL_EVIDENCE_COVERAGE",
        severity="warning",
        category="evidence_quality",
        label="证据覆盖不完整",
        description="回答已使用可用证据，但问题要求的部分维度没有被文档覆盖。",
    ),
    "NO_COMPARATIVE_STRENGTH_EVIDENCE": WarningDescriptor(
        code="NO_COMPARATIVE_STRENGTH_EVIDENCE",
        severity="warning",
        category="evidence_quality",
        label="缺少优势对比证据",
        description="文档缺少足以证明相对优势的目标、历史或同业对比证据。",
    ),
    "LOW_CHART_CONFIDENCE": WarningDescriptor(
        code="LOW_CHART_CONFIDENCE",
        severity="warning",
        category="evidence_quality",
        label="图表识别置信度较低",
        description="相关图表可以用于辅助判断，但其结构化识别结果需要人工复核。",
    ),
    "QUERY_PLANNER_PROVIDER_ERROR": WarningDescriptor(
        code="QUERY_PLANNER_PROVIDER_ERROR",
        severity="degraded",
        category="provider_fallback",
        label="查询规划已降级",
        description="查询规划模型调用失败，系统已使用确定性查询计划继续完成回答。",
    ),
    "QUERY_PLANNER_OUTPUT_INVALID": WarningDescriptor(
        code="QUERY_PLANNER_OUTPUT_INVALID",
        severity="degraded",
        category="provider_fallback",
        label="查询规划已降级",
        description="查询规划模型返回了无效结构，系统已使用确定性查询计划继续完成回答。",
    ),
    "LLM_PROVIDER_ERROR": WarningDescriptor(
        code="LLM_PROVIDER_ERROR",
        severity="degraded",
        category="provider_fallback",
        label="模型生成已降级",
        description="回答生成模型调用失败，系统已返回经过验证的确定性证据答案。",
    ),
    "LLM_EMPTY_OR_OVERSIZED_RESPONSE": WarningDescriptor(
        code="LLM_EMPTY_OR_OVERSIZED_RESPONSE",
        severity="degraded",
        category="answer_fallback",
        label="模型回答已回退",
        description="模型返回为空或超出安全长度，系统已改用确定性证据答案。",
    ),
    "LLM_OUTPUT_TRUNCATED": WarningDescriptor(
        code="LLM_OUTPUT_TRUNCATED",
        severity="degraded",
        category="answer_fallback",
        label="模型回答被截断",
        description="模型达到输出长度上限，系统已改用完整的确定性证据答案。",
    ),
    "LLM_CITATION_VALIDATION_FAILED": WarningDescriptor(
        code="LLM_CITATION_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="引用校验后已回退",
        description="模型回答的引用未通过校验，系统已改用确定性证据答案。",
    ),
    "LLM_NUMERIC_VALIDATION_FAILED": WarningDescriptor(
        code="LLM_NUMERIC_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="数字校验后已回退",
        description="模型回答中的数字未通过证据校验，系统已改用确定性证据答案。",
    ),
    "EVIDENCE_ROLE_VALIDATION_FAILED": WarningDescriptor(
        code="EVIDENCE_ROLE_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="证据类型校验后已回退",
        description="模型回答使用了不符合当前问题类型的证据，系统已改用确定性证据答案。",
    ),
    "LLM_QUERY_ADHERENCE_FAILED": WarningDescriptor(
        code="LLM_QUERY_ADHERENCE_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="问题相关性校验后已回退",
        description="模型回答偏离了用户问题，系统已改用确定性证据答案。",
    ),
}


def describe_warning(code: str) -> WarningDescriptor:
    normalized = str(code).strip() or "UNKNOWN_WARNING"
    return _WARNING_CATALOG.get(
        normalized,
        WarningDescriptor(
            code=normalized,
            severity="warning",
            category="uncategorized",
            label="运行提示",
            description="该运行产生了尚未分类的诊断信号；请使用代码在服务日志中检索。",
        ),
    )


def warning_details(codes: list[str]) -> list[dict[str, str]]:
    return [describe_warning(code).to_dict() for code in dict.fromkeys(codes)]


def has_degraded_warning(codes: list[str]) -> bool:
    return any(describe_warning(code).severity == "degraded" for code in codes)


def warning_codes_by_severity(severity: WarningSeverity) -> tuple[str, ...]:
    return tuple(item.code for item in _WARNING_CATALOG.values() if item.severity == severity)
