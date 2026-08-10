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
        label="Document contains synthetic data",
        description="The cited pages are marked by the document as synthetic, test, or sample data. This is not a system failure.",
    ),
    "INSUFFICIENT_EVIDENCE": WarningDescriptor(
        code="INSUFFICIENT_EVIDENCE",
        severity="warning",
        category="evidence_quality",
        label="Insufficient evidence",
        description="The current documents do not contain enough relevant business evidence to support a more specific conclusion.",
    ),
    "PARTIAL_EVIDENCE_COVERAGE": WarningDescriptor(
        code="PARTIAL_EVIDENCE_COVERAGE",
        severity="warning",
        category="evidence_quality",
        label="Some details lack source support",
        description=(
            "Other parts of the answer remain supported; the current documents do not yet provide the information "
            "needed for some requested details."
        ),
    ),
    "NO_COMPARATIVE_STRENGTH_EVIDENCE": WarningDescriptor(
        code="NO_COMPARATIVE_STRENGTH_EVIDENCE",
        severity="warning",
        category="evidence_quality",
        label="Comparative strength evidence missing",
        description="The documents lack target, historical, or peer comparisons sufficient to demonstrate a relative strength.",
    ),
    "LOW_CHART_CONFIDENCE": WarningDescriptor(
        code="LOW_CHART_CONFIDENCE",
        severity="warning",
        category="evidence_quality",
        label="Low chart recognition confidence",
        description="The related charts can support the analysis, but their structured extraction needs human review.",
    ),
    "QUERY_PLANNER_PROVIDER_ERROR": WarningDescriptor(
        code="QUERY_PLANNER_PROVIDER_ERROR",
        severity="degraded",
        category="provider_fallback",
        label="Query planning degraded",
        description="The query-planning model call failed, so the system used a deterministic retrieval plan to complete the answer.",
    ),
    "QUERY_PLANNER_OUTPUT_INVALID": WarningDescriptor(
        code="QUERY_PLANNER_OUTPUT_INVALID",
        severity="degraded",
        category="provider_fallback",
        label="Query planning degraded",
        description=(
            "The query-planning model returned an invalid structure, so the system used a deterministic retrieval plan "
            "to complete the answer."
        ),
    ),
    "LLM_PROVIDER_ERROR": WarningDescriptor(
        code="LLM_PROVIDER_ERROR",
        severity="degraded",
        category="provider_fallback",
        label="Model generation degraded",
        description="The answer-generation model call failed, so the system returned a verified deterministic evidence answer.",
    ),
    "LLM_EMPTY_OR_OVERSIZED_RESPONSE": WarningDescriptor(
        code="LLM_EMPTY_OR_OVERSIZED_RESPONSE",
        severity="degraded",
        category="answer_fallback",
        label="Model answer replaced",
        description="The model response was empty or exceeded the safe length, so the system used a deterministic evidence answer.",
    ),
    "LLM_OUTPUT_TRUNCATED": WarningDescriptor(
        code="LLM_OUTPUT_TRUNCATED",
        severity="degraded",
        category="answer_fallback",
        label="Model answer truncated",
        description="The model reached its output limit, so the system used a complete deterministic evidence answer.",
    ),
    "LLM_CITATION_VALIDATION_FAILED": WarningDescriptor(
        code="LLM_CITATION_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="Citation validation handled",
        description="Some citations failed validation. The system removed unsupported content or used a safe evidence answer.",
    ),
    "LLM_NUMERIC_VALIDATION_FAILED": WarningDescriptor(
        code="LLM_NUMERIC_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="Numeric validation handled",
        description=(
            "Some numeric claims failed evidence validation. The system removed unsupported content or used a safe "
            "evidence answer."
        ),
    ),
    "EVIDENCE_ROLE_VALIDATION_FAILED": WarningDescriptor(
        code="EVIDENCE_ROLE_VALIDATION_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="Evidence role validation fallback",
        description="The model used evidence that did not match the question type, so the system used a deterministic evidence answer.",
    ),
    "LLM_QUERY_ADHERENCE_FAILED": WarningDescriptor(
        code="LLM_QUERY_ADHERENCE_FAILED",
        severity="degraded",
        category="answer_fallback",
        label="Query relevance validation fallback",
        description="The model answer drifted from the user's question, so the system used a deterministic evidence answer.",
    ),
}

_LOCALIZED_WARNING_COPY: dict[tuple[str, str], dict[str, str]] = {
    ("PARTIAL_EVIDENCE_COVERAGE", "zh"): {
        "label": "部分内容暂无资料支持",
        "description": "回答中的其余内容仍有资料支持；当前文档暂未提供问题中部分内容所需的信息。",
    },
}


def describe_warning(code: str) -> WarningDescriptor:
    normalized = str(code).strip() or "UNKNOWN_WARNING"
    return _WARNING_CATALOG.get(
        normalized,
        WarningDescriptor(
            code=normalized,
            severity="warning",
            category="uncategorized",
            label="Run notice",
            description="This run produced an unclassified diagnostic signal. Use the code to search the service logs.",
        ),
    )


def warning_details(codes: list[str], *, language: str = "en") -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    normalized_language = "zh" if language.casefold().startswith("zh") else "en"
    for code in dict.fromkeys(codes):
        detail = describe_warning(code).to_dict()
        detail.update(_LOCALIZED_WARNING_COPY.get((detail["code"], normalized_language), {}))
        details.append(detail)
    return details


def has_degraded_warning(codes: list[str]) -> bool:
    return any(describe_warning(code).severity == "degraded" for code in codes)


def warning_codes_by_severity(severity: WarningSeverity) -> tuple[str, ...]:
    return tuple(item.code for item in _WARNING_CATALOG.values() if item.severity == severity)
