from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

CoverageStatus = Literal["supported", "partial", "unsupported", "conflicting", "unreliable"]


_COMPARISON_MARKERS = (
    "同比",
    "环比",
    "增长",
    "增速",
    "变化",
    "变动",
    "下降",
    "比较",
    "compare",
    "growth",
    "grow",
    "change",
    "increase",
    "decrease",
)
_DRIVER_MARKERS = (
    "主要来自",
    "来自哪些",
    "增长来源",
    "贡献增长",
    "增长贡献",
    "驱动",
    "归因",
    "为什么",
    "原因",
    "contribut",
    "driv",
    "mainly from",
    "why",
    "reason",
)
_ATTRIBUTION_EVIDENCE_MARKERS = (
    "主要来自",
    "增长来源",
    "贡献",
    "驱动",
    "推动",
    "带动",
    "归因",
    "contribut",
    "driv",
    "mainly from",
    "attribut",
)
_SEGMENT_MARKERS = (
    "业务板块",
    "板块",
    "市场",
    "地区",
    "区域",
    "产品线",
    "渠道",
    "segment",
    "market",
    "region",
    "product line",
    "channel",
)
_SOURCE_MARKERS = ("来源", "出处", "数据源", "公开披露", "source", "provenance", "disclosure")
_ACTION_MARKERS = ("建议", "行动", "措施", "改善", "优化", "怎么办", "recommend", "action", "improve", "mitigat")
_EVALUATION_MARKERS = (
    "优势",
    "风险",
    "问题",
    "机会",
    "表现",
    "业绩",
    "strength",
    "risk",
    "issue",
    "opportunit",
    "performance",
)
_DEFINITION_MARKERS = ("是什么意思", "什么含义", "定义", "解释", "meaning", "definition", "define")
_VALUE_MARKERS = ("多少", "数值", "金额", "比例", "value", "amount", "how much")
_PERIOD_PATTERN = re.compile(r"(?:FY|CY)?20\d{2}|20\d{2}年|Q[1-4]|H[12]|第[一二三四1-4]季度", re.I)


@dataclass(frozen=True, slots=True)
class EvidenceFacet:
    """Describe one independently verifiable requirement in an answer plan."""
    facet_id: str
    label: str
    kind: str
    subject: str = ""
    constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "facet_id": self.facet_id,
            "label": self.label,
            "kind": self.kind,
            "subject": self.subject,
            "constraints": list(self.constraints),
        }


@dataclass(frozen=True, slots=True)
class FacetCoverage:
    """Record how strongly retrieved evidence supports one required facet."""
    facet: EvidenceFacet
    status: CoverageStatus
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            **self.facet.to_dict(),
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Aggregate facet-level support and expose remaining evidence gaps."""
    facets: tuple[FacetCoverage, ...]

    @property
    def total(self) -> int:
        """Return the total for this coverage result."""
        return len(self.facets)

    @property
    def supported_count(self) -> int:
        """Return the supported count for this coverage result."""
        return sum(item.status == "supported" for item in self.facets)

    @property
    def partial_count(self) -> int:
        """Return the partial count for this coverage result."""
        return sum(item.status == "partial" for item in self.facets)

    @property
    def has_gaps(self) -> bool:
        """Return whether this coverage result has gaps."""
        return any(item.status != "supported" for item in self.facets)

    @property
    def covered_labels(self) -> tuple[str, ...]:
        """Return the covered labels for this coverage result."""
        return tuple(item.facet.label for item in self.facets if item.status == "supported")

    @property
    def gap_labels(self) -> tuple[str, ...]:
        """Return the gap labels for this coverage result."""
        return tuple(item.facet.label for item in self.facets if item.status != "supported")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "total": self.total,
            "supported": self.supported_count,
            "partial": self.partial_count,
            "coverage_ratio": round(self.supported_count / self.total, 3) if self.total else 0.0,
            "has_gaps": self.has_gaps,
            "gap_labels": list(self.gap_labels),
            "facets": [item.to_dict() for item in self.facets],
        }


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in markers)


def _canonical_subject(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip(" ,，、:：?？")
    compact = _PERIOD_PATTERN.sub("", compact)
    compact = re.sub(r"(?:从|由)\s*[^，,]*?(?:到|至)\s*[^，,]*$", "", compact).strip()
    folded = compact.casefold()
    if "vonb margin" in folded or "新业务价值率" in compact:
        return "vonb_margin"
    if re.search(r"\bvonb\b", folded) or compact == "新业务价值":
        return "vonb"
    if re.search(r"\bopat\b", folded) or "税后营运溢利" in compact:
        return "opat"
    ascii_terms = re.findall(r"[a-z][a-z0-9_-]{1,}", folded)
    if ascii_terms:
        return "_".join(ascii_terms[:4])
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", compact)
    return chinese[:24]


def _facet_id(kind: str, subject: str, label: str) -> str:
    if subject:
        return f"{kind}:{subject}"
    stable = hashlib.sha1(label.casefold().encode("utf-8")).hexdigest()[:10]
    return f"{kind}:{stable}"


def _kind_for_clause(clause: str) -> str:
    if _contains_any(clause, _SOURCE_MARKERS):
        return "source"
    if _contains_any(clause, _DRIVER_MARKERS):
        return "driver_attribution"
    if _contains_any(clause, _ACTION_MARKERS):
        return "action"
    if _contains_any(clause, _DEFINITION_MARKERS):
        return "definition"
    if _contains_any(clause, _COMPARISON_MARKERS):
        return "metric_comparison"
    if _contains_any(clause, _VALUE_MARKERS):
        return "metric_value"
    if _contains_any(clause, _EVALUATION_MARKERS):
        return "evaluation"
    return "direct_answer"


def _metric_subjects(clause: str, kind: str) -> list[str]:
    folded = clause.casefold()
    boundary = len(clause)
    markers = _COMPARISON_MARKERS if kind == "metric_comparison" else _VALUE_MARKERS
    for marker in markers:
        position = folded.find(marker)
        if position >= 0:
            boundary = min(boundary, position)
    prefix = clause[:boundary]
    prefix = re.sub(
        r"^(?:请问|请帮我|请|what is|what was|how much did|how much|compare)\s*",
        "",
        prefix,
        flags=re.I,
    )
    prefix = re.sub(r"(?:分别|各自)$", "", prefix).strip()
    parts = re.split(r"\s*(?:、|，|,|和|及|与|\band\b)\s*", prefix, flags=re.I)
    subjects = [_canonical_subject(part) for part in parts]
    return list(dict.fromkeys(subject for subject in subjects if subject))[:8]


def build_evidence_contract(
    question: str,
    *,
    tool_evidence: Iterable[Any] = (),
) -> tuple[EvidenceFacet, ...]:
    """Create stable, typed answer requirements from the user's wording.

    LLM-generated free-form requirements remain useful for retrieval, but they
    never become coverage keys. This keeps coverage repeatable across planner
    runs that phrase the same requirement differently.
    """

    clauses = [item.strip(" ,，") for item in re.split(r"[?？!！;；。]+", question) if item.strip(" ,，")]
    clause_kinds = {_kind_for_clause(clause) for clause in clauses}
    non_chart_requirements = {"driver_attribution", "action", "definition", "source"}
    tool_rows = [_evidence_view(item, index) for index, item in enumerate(tool_evidence, 1)]
    if not (clause_kinds & non_chart_requirements) and any(
        str(row.get("extraction") or "") == "native_chart_analysis_bundle" for row in tool_rows
    ):
        return (EvidenceFacet(_facet_id("chart_analysis", "", question), question, "chart_analysis"),)
    non_calculation_requirements = non_chart_requirements | {"evaluation"}
    if not (clause_kinds & non_calculation_requirements) and any(
        str(row.get("extraction") or "") == "native_chart_calculation" for row in tool_rows
    ):
        return (
            EvidenceFacet(_facet_id("chart_calculation", "", question), question, "chart_calculation"),
        )
    if not (clause_kinds & non_calculation_requirements) and any(
        str(row.get("extraction") or "") == "native_table_calculation" for row in tool_rows
    ):
        return (
            EvidenceFacet(_facet_id("table_calculation", "", question), question, "table_calculation"),
        )

    facets: list[EvidenceFacet] = []
    for clause in clauses or [question.strip()]:
        kind = _kind_for_clause(clause)
        constraints = tuple(dict.fromkeys(match.group(0) for match in _PERIOD_PATTERN.finditer(clause)))
        if kind in {"metric_comparison", "metric_value"}:
            subjects = _metric_subjects(clause, kind)
            for subject in subjects or [""]:
                label = f"{subject} comparison" if kind == "metric_comparison" and subject else clause
                if subject == "vonb_margin":
                    label = "VONB margin / 新业务价值率同比变化"
                elif subject == "vonb":
                    label = "VONB同比变化" if kind == "metric_comparison" else "VONB数值"
                elif subject == "opat":
                    label = "OPAT同比变化" if kind == "metric_comparison" else "OPAT数值"
                facets.append(EvidenceFacet(_facet_id(kind, subject, label), label, kind, subject, constraints))
            continue
        subject = "business_segment" if kind == "driver_attribution" and _contains_any(clause, _SEGMENT_MARKERS) else ""
        facets.append(EvidenceFacet(_facet_id(kind, subject, clause), clause, kind, subject, constraints))

    deduped: dict[str, EvidenceFacet] = {}
    for facet in facets:
        deduped.setdefault(facet.facet_id, facet)
    return tuple(deduped.values()) or (EvidenceFacet("direct_answer:default", question, "direct_answer"),)


def _subject_aliases(subject: str) -> tuple[str, ...]:
    aliases = {
        "vonb": ("vonb", "新业务价值"),
        "opat": ("opat", "税后营运溢利"),
        "vonb_margin": ("vonb margin", "新业务价值率", "margin"),
        "business_segment": _SEGMENT_MARKERS,
    }
    return aliases.get(subject, tuple(part for part in subject.replace("_", " ").split() if len(part) > 1))


def _evidence_view(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    source = getattr(item, "source", {}) or {}
    return {
        **source,
        "quote": getattr(item, "quote", ""),
        "content_role": getattr(item, "content_role", ""),
        "confidence": source.get("confidence", 1.0),
        "evidence_atom_id": getattr(item, "atom_id", f"evidence_{index}"),
    }


def _is_related(facet: EvidenceFacet, text: str) -> bool:
    folded = text.casefold()
    aliases = _subject_aliases(facet.subject)
    if aliases and not any(alias in folded for alias in aliases):
        return False
    if facet.kind == "driver_attribution":
        return _contains_any(folded, (*_SEGMENT_MARKERS, *_COMPARISON_MARKERS, "vonb", "opat"))
    if facet.kind == "evaluation":
        return _contains_any(folded, _EVALUATION_MARKERS) or bool(re.search(r"\d", folded))
    return True


def _fully_supports(facet: EvidenceFacet, evidence: dict[str, Any]) -> bool:
    text = str(evidence.get("quote") or evidence.get("content") or "")
    folded = text.casefold()
    if not _is_related(facet, folded):
        return False
    if facet.constraints and not all(constraint.casefold() in folded for constraint in facet.constraints):
        return False
    role = str(evidence.get("content_role") or "")
    if facet.kind == "metric_comparison":
        period_signal = bool(_PERIOD_PATTERN.search(text) or re.search(r"\d{2}/\d{1,2}|同比|环比|[+-]\d", text, re.I))
        return bool(re.search(r"\d", text)) and period_signal
    if facet.kind == "metric_value":
        return bool(re.search(r"\d", text))
    if facet.kind == "driver_attribution":
        return _contains_any(folded, _ATTRIBUTION_EVIDENCE_MARKERS)
    if facet.kind == "source":
        return role in {"provenance", "methodology"} or _contains_any(folded, _SOURCE_MARKERS)
    if facet.kind == "action":
        return role == "management_insight" or _contains_any(folded, _ACTION_MARKERS)
    if facet.kind == "definition":
        return not bool(re.fullmatch(r"[\s\W]*\d[\d\s.,%]*", text))
    if facet.kind == "evaluation":
        return role in {"risk_signal", "management_insight", "table", "chart", "business_fact"}
    if facet.kind == "chart_analysis":
        return role == "chart" and evidence.get("extraction") == "native_chart_analysis_bundle"
    if facet.kind == "chart_calculation":
        return role == "chart" and evidence.get("extraction") == "native_chart_calculation"
    if facet.kind == "table_calculation":
        return role == "table" and evidence.get("extraction") == "native_table_calculation"
    return bool(text.strip())


def evaluate_evidence_coverage(
    contract: Iterable[EvidenceFacet],
    evidence: Iterable[Any],
) -> CoverageResult:
    rows = [_evidence_view(item, index) for index, item in enumerate(evidence, 1)]
    results: list[FacetCoverage] = []
    for facet in contract:
        related = [row for row in rows if _is_related(facet, str(row.get("quote") or row.get("content") or ""))]
        supporting = [row for row in related if _fully_supports(facet, row)]
        reliable = [
            row
            for row in supporting
            if float(row.get("confidence") or 1.0) >= 0.65
            and not (row.get("source_kind") == "visual_model" and facet.kind.startswith("metric_"))
        ]
        evidence_ids = tuple(
            str(row.get("evidence_atom_id") or row.get("id") or f"evidence_{index}")
            for index, row in enumerate(reliable or supporting or related, 1)
        )
        if reliable:
            status: CoverageStatus = "supported"
            reason = "At least one reliable evidence item satisfies the typed requirement."
        elif supporting:
            status = "unreliable"
            reason = "Relevant evidence exists, but its source or confidence is not sufficient."
        elif related:
            status = "partial"
            reason = "Related evidence exists, but it lacks a required comparison, attribution, period, or value."
        else:
            status = "unsupported"
            reason = "No selected evidence item addresses this requirement."
        results.append(FacetCoverage(facet, status, evidence_ids, reason))
    return CoverageResult(tuple(results))


def facet_ids_for_evidence(coverage: CoverageResult) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for item in coverage.facets:
        for evidence_id in item.evidence_ids:
            result.setdefault(evidence_id, []).append(item.facet.facet_id)
    return {key: tuple(dict.fromkeys(values)) for key, values in result.items()}
