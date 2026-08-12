from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from packages.qbr_core.planning import QueryPlan

CoverageStatus = Literal["supported", "partial", "unsupported", "conflicting", "unreliable"]


_COMPARISON_MARKERS = (
    "同比",
    "环比",
    "增长",
    "增速",
    "变化",
    "变动",
    "增加",
    "提高",
    "下降",
    "降低",
    "差异",
    "相差",
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
    "drove",
    "driven",
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
    "drove",
    "driven",
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
_ACTION_REQUEST_MARKERS = ("建议", "行动", "措施", "怎么办", "recommend", "action", "mitigat")
_DEFINITION_MARKERS = ("是什么意思", "什么含义", "定义", "解释", "meaning", "definition", "define")
_VALUE_MARKERS = ("多少", "数值", "金额", "比例", "value", "amount", "how much")
_DECISION_MARKERS = (
    "是否",
    "达标",
    "阈值",
    "限额",
    "超限",
    "越线",
    "whether",
    "threshold",
    "target",
    "limit",
    "breach",
)
_ANALYSIS_MARKERS = (
    "解读",
    "分析",
    "说明了什么",
    "怎么看",
    "interpret",
    "analy",
    "what does",
    "show",
)
_PERIOD_PATTERN = re.compile(r"20\d{2}年|(?:FY|CY)?20\d{2}|Q[1-4]|H[12]|第[一二三四1-4]季度", re.I)
_VISUAL_REFERENCE_PATTERN = re.compile(
    r"(?:图(?:表)?|柱(?:状|形)?图|折线|坐标轴|系列|charts?|graphs?|plots?|axes|axis|series|bars?|columns?|lines?)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class EvidenceFacet:
    """Describe one delivery requirement and its composable evidence checks."""
    facet_id: str
    label: str
    kind: str
    subject: str = ""
    constraints: tuple[str, ...] = ()
    validators: tuple[str, ...] = ("direct_support",)
    minimum_evidence: int = 1
    minimum_distinct_contexts: int = 1
    match_terms: tuple[str, ...] = ()
    semantic_match_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this value."""
        return {
            "facet_id": self.facet_id,
            "label": self.label,
            "kind": self.kind,
            "subject": self.subject,
            "constraints": list(self.constraints),
            "validators": list(self.validators),
            "minimum_evidence": self.minimum_evidence,
            "minimum_distinct_contexts": self.minimum_distinct_contexts,
            "match_terms": list(self.match_terms),
            "semantic_match_required": self.semantic_match_required,
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
    compact = re.sub(r"^.*(?:表|图|矩阵)(?:中|的)?", "", compact).strip()
    compact = _PERIOD_PATTERN.sub("", compact)
    compact = re.sub(r"(?:从|由)\s*[^，,]*?(?:到|至)\s*[^，,]*$", "", compact).strip()
    compact = re.sub(r"(?:分别|各自|合计占|合计|占)$", "", compact).strip()
    folded = compact.casefold()
    ascii_terms = re.findall(r"[a-z][a-z0-9_-]{1,}", folded)
    if ascii_terms:
        return "_".join(ascii_terms[:4])
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", compact)
    return chinese[:24]


def _term_subject(term: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", term.casefold())) or _canonical_subject(term)


def _facet_id(validators: tuple[str, ...], subject: str, label: str) -> str:
    profile = "+".join(validators)
    if subject:
        return f"requirement:{profile}:{subject}"
    stable = hashlib.sha1(label.casefold().encode("utf-8")).hexdigest()[:10]
    return f"requirement:{profile}:{stable}"


def _validators_for(text: str) -> tuple[str, ...]:
    """Derive orthogonal evidence checks instead of assigning one intent label."""

    validators = ["direct_support"]
    if _contains_any(text, _SOURCE_MARKERS):
        validators.append("provenance")
    if _contains_any(text, _DRIVER_MARKERS):
        validators.append("attribution")
    if _contains_any(text, _ACTION_REQUEST_MARKERS) or re.search(
        r"(?:如何|怎么)优?化|(?:如何|怎么)(?:改善|提高|降低)|how\s+(?:to|should|can)\b[^?.!]*(?:improve|optimi[sz]e|reduce)",
        text,
        re.I,
    ):
        validators.append("action")
    if _contains_any(text, _DEFINITION_MARKERS):
        validators.append("definition")
    if _contains_any(text, _COMPARISON_MARKERS):
        validators.append("comparison")
    if _contains_any(text, _VALUE_MARKERS):
        validators.append("numeric")
    if _contains_any(text, _DECISION_MARKERS):
        validators.extend(("numeric", "decision_criterion"))
    if _VISUAL_REFERENCE_PATTERN.search(text) and _contains_any(text, _ANALYSIS_MARKERS):
        validators.append("visual_analysis")
    return tuple(dict.fromkeys(validators))


def _metric_subjects(clause: str) -> list[str]:
    working = re.sub(
        r"^(?:请问|请帮我|请|what is|what was|how much did|how much|compare)\s*",
        "",
        clause,
        flags=re.I,
    )
    folded = working.casefold()
    boundary = len(working)
    primary_markers = (*_VALUE_MARKERS, *_DECISION_MARKERS)
    markers = primary_markers if any(marker in folded for marker in primary_markers) else _COMPARISON_MARKERS
    for marker in markers:
        position = folded.find(marker)
        if position >= 0:
            boundary = min(boundary, position)
    prefix = working[:boundary]
    prefix = re.split(r"[,，:：]", prefix)[-1].strip()
    prefix = re.sub(r"(?:分别|各自)$", "", prefix).strip()
    parts = re.split(r"\s*(?:、|，|,|和|及|与|\band\b)\s*", prefix, flags=re.I)
    from packages.qbr_core.planning.terminology import mentioned_terms

    if len(parts) == 1:

        terms = mentioned_terms(prefix)
        if terms:
            def last_position(definition: Any) -> int:
                return max((folded.rfind(alias.casefold()) for alias in definition.search_aliases), default=-1)

            return [_term_subject(max(terms, key=last_position).term)]
        return []
    subjects = []
    for part in parts:
        terms = mentioned_terms(part)
        subject = _term_subject(max(terms, key=lambda item: len(item.term)).term) if terms else _canonical_subject(part)
        if subject:
            subjects.append(subject)
    return list(dict.fromkeys(subject for subject in subjects if subject))[:8]


def build_evidence_contract(
    task: str | QueryPlan,
    *,
    tool_evidence: Iterable[Any] = (),
) -> tuple[EvidenceFacet, ...]:
    """Build a completeness contract from requested outcomes plus reusable checks.

    The contract is deliberately not an intent taxonomy.  Each outcome remains
    natural language and receives any number of orthogonal validators such as
    numeric, comparison, attribution or provenance.  New question phrasings
    therefore compose existing checks instead of requiring a new answer class.
    """

    del tool_evidence  # Tool evidence is evaluated against the same user contract.
    question = str(getattr(task, "original_question", task)).strip()
    planned = tuple(str(item).strip() for item in getattr(task, "delivery_requirements", ()) if str(item).strip())
    requirements = planned or tuple(
        item.strip(" ,，")
        for item in re.split(r"[?？!！;；。]+", question)
        if item.strip(" ,，")
    ) or (question,)
    question_clauses = tuple(
        item.strip(" ,，")
        for item in re.split(r"[?？!！;；。]+", question)
        if item.strip(" ,，")
    ) or (question,)
    visual_task = bool(_VISUAL_REFERENCE_PATTERN.search(question) and _contains_any(question, _ANALYSIS_MARKERS))
    facets: list[EvidenceFacet] = []
    evidence_hints = tuple(str(item).strip() for item in getattr(task, "evidence_requirements", ()) if str(item).strip())
    aligned_hints = evidence_hints if len(evidence_hints) == len(requirements) else ("",) * len(requirements)
    for requirement_index, requirement in enumerate(requirements):
        matching_clause = _closest_clause(requirement, question_clauses)
        context = " ".join(dict.fromkeys((requirement, matching_clause)))
        validators = _validators_for(context)
        if visual_task and "visual_analysis" not in validators:
            validators = (*validators, "visual_analysis")
        constraints = tuple(dict.fromkeys(match.group(0) for match in _PERIOD_PATTERN.finditer(context)))
        minimum = _minimum_evidence(task, validators, len(requirements))
        matching_text = " ".join((requirement, aligned_hints[requirement_index])).strip()
        match_terms = tuple(sorted(_semantic_terms(matching_text) - _MATCH_STOPWORDS))
        semantic_match_required = len(requirements) > 1 and set(validators) == {"direct_support"}
        quantitative = any(item in validators for item in ("numeric", "decision_criterion"))
        subjects = _metric_subjects(matching_clause) if quantitative else []
        if "attribution" in validators and not subjects:
            folded = matching_clause.casefold()
            subjects = [
                _canonical_subject(marker)
                for marker in _SEGMENT_MARKERS
                if marker in folded and _canonical_subject(marker)
            ][:1]
        for subject in subjects or [""]:
            label = f"{subject.replace('_', ' ')}: {requirement}" if subject and len(subjects) > 1 else requirement
            facets.append(
                EvidenceFacet(
                    _facet_id(validators, subject, label),
                    label,
                    "delivery_requirement",
                    subject,
                    constraints,
                    validators,
                    minimum,
                    minimum,
                    match_terms,
                    semantic_match_required,
                )
            )

    deduped: dict[str, EvidenceFacet] = {}
    for facet in facets:
        deduped.setdefault(facet.facet_id, facet)
    return tuple(deduped.values())


def _semantic_terms(text: str) -> set[str]:
    folded = text.casefold()
    terms = set(re.findall(r"[a-z0-9%_.-]{2,}", folded))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        if len(phrase) <= 4:
            terms.add(phrase)
        terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


_MATCH_STOPWORDS = _semantic_terms(
    "answer evidence supported support directly relevant explain summarize identify state give use "
    "回答 证据 支持 直接 相关 说明 总结 概括 识别 给出 使用 用户 问题"
)


def _closest_clause(requirement: str, clauses: tuple[str, ...]) -> str:
    requirement_terms = _semantic_terms(requirement)
    return max(clauses, key=lambda clause: len(requirement_terms & _semantic_terms(clause)))


def _minimum_evidence(task: str | QueryPlan, validators: tuple[str, ...], requirement_count: int) -> int:
    substantive = set(validators) - {"direct_support"}
    if substantive or requirement_count > 1 or isinstance(task, str):
        return 1
    execution_profile = str(getattr(task, "execution_profile", "focused"))
    broad_bridge = any(str(getattr(query, "kind", "")) == "paraphrase_bridge" for query in getattr(task, "retrieval_queries", ()))
    return 2 if execution_profile in {"analytical", "deep"} or broad_bridge else 1


def tool_evidence_fully_answers(
    task: str | QueryPlan,
    evidence: Iterable[Any],
    *,
    tool_kind: str = "",
) -> bool:
    """Return whether one deterministic tool result may define the whole answer scope.

    A successful calculation is not automatically a complete answer.  It may
    replace the normal evidence contract only when the user's wording asks for
    a bounded quantitative result or for analysis of an explicitly referenced
    visual.  Broad synthesis questions therefore retain evidence from other
    slides and dimensions even when a useful calculation is available.
    """

    if tool_kind == "scope_reconciliation":
        return True
    rows = list(evidence)
    contract = build_evidence_contract(task)
    if not contract or any(set(item.validators) == {"direct_support"} for item in contract):
        return False
    coverage = evaluate_evidence_coverage(contract, rows)
    if not coverage.has_gaps:
        return True
    if len(contract) != 1 or not contract[0].subject:
        return False
    relaxed = replace(contract[0], subject="")
    relaxed_coverage = evaluate_evidence_coverage((relaxed,), rows)
    if relaxed_coverage.has_gaps:
        return False
    evidence_terms = _semantic_terms(
        "\n".join(_evidence_text(_evidence_view(row, index)) for index, row in enumerate(rows, 1))
    )
    task_terms = set(contract[0].match_terms) - _MATCH_STOPWORDS
    return len(task_terms & evidence_terms) >= min(2, len(task_terms))


def _subject_aliases(subject: str) -> tuple[str, ...]:
    spaced = subject.replace("_", " ")
    aliases = [spaced, *(part for part in spaced.split() if len(part) > 1)]
    from packages.qbr_core.planning.terminology import QBR_TERMS

    normalized_subject = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", spaced.casefold())
    for definition in QBR_TERMS:
        search_aliases = tuple(alias.casefold() for alias in definition.search_aliases)
        if any(
            normalized_subject == re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", alias)
            for alias in search_aliases
        ):
            aliases.extend(search_aliases)
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


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


def _evidence_text(evidence: dict[str, Any]) -> str:
    """Combine an evidence unit with its source-derived semantic context."""

    values = (
        evidence.get("quote") or evidence.get("content"),
        evidence.get("facet"),
        evidence.get("chart_title"),
        evidence.get("slide_title"),
        evidence.get("slide_summary"),
    )
    return "\n".join(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _is_related(facet: EvidenceFacet, text: str) -> bool:
    folded = text.casefold()
    aliases = _subject_aliases(facet.subject)
    if not aliases:
        if facet.semantic_match_required:
            return bool(set(facet.match_terms) & (_semantic_terms(text) - _MATCH_STOPWORDS))
        return True
    if any(alias in folded for alias in aliases):
        return True
    subject_terms = _semantic_terms(facet.subject.replace("_", " ")) - _MATCH_STOPWORDS
    evidence_terms = _semantic_terms(text) - _MATCH_STOPWORDS
    if subject_terms and len(subject_terms & evidence_terms) / len(subject_terms) >= 0.5:
        return True
    return "attribution" in facet.validators and _contains_any(folded, (*_SEGMENT_MARKERS, *_COMPARISON_MARKERS))


def _context_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("document_id") or ""), str(row.get("slide_id") or row.get("element_id") or "")


def _constraint_satisfied(constraint: str, text: str) -> bool:
    year = re.search(r"20\d{2}", constraint)
    if year:
        return year.group(0) in text
    quarter = re.search(r"Q([1-4])", constraint, re.I)
    if quarter:
        return bool(re.search(rf"Q{quarter.group(1)}\b|第{'一二三四'[int(quarter.group(1)) - 1]}季度", text, re.I))
    chinese_quarter = re.search(r"第([一二三四1-4])季度", constraint)
    if chinese_quarter:
        value = {"一": "1", "二": "2", "三": "3", "四": "4"}.get(chinese_quarter.group(1), chinese_quarter.group(1))
        return bool(re.search(rf"Q{value}\b|第{chinese_quarter.group(1)}季度", text, re.I))
    return constraint.casefold() in text.casefold()


def _missing_validators(facet: EvidenceFacet, rows: list[dict[str, Any]]) -> tuple[str, ...]:
    text = "\n".join(_evidence_text(row) for row in rows)
    folded = text.casefold()
    roles = {str(row.get("content_role") or "") for row in rows}
    extractions = {str(row.get("extraction") or "") for row in rows}
    missing: list[str] = []
    for validator in facet.validators:
        satisfied = True
        if validator == "direct_support":
            satisfied = bool(text.strip())
        elif validator == "numeric":
            satisfied = bool(re.search(r"\d", text))
        elif validator == "comparison":
            satisfied = bool(
                {"native_chart_calculation", "native_table_calculation", "native_chart_analysis_bundle"} & extractions
                or _PERIOD_PATTERN.search(text)
                or re.search(r"\d{2}/\d{1,2}|同比|环比|[+-]\d|higher|lower|increase|decrease", text, re.I)
            )
        elif validator == "attribution":
            satisfied = _contains_any(folded, _ATTRIBUTION_EVIDENCE_MARKERS)
        elif validator == "provenance":
            satisfied = bool(roles & {"provenance", "methodology"}) or _contains_any(folded, _SOURCE_MARKERS)
        elif validator == "action":
            satisfied = "management_insight" in roles or _contains_any(folded, _ACTION_MARKERS)
        elif validator == "definition":
            satisfied = bool(text.strip()) and not bool(re.fullmatch(r"[\s\W]*\d[\d\s.,%]*", text))
        elif validator == "decision_criterion":
            satisfied = _contains_any(folded, _DECISION_MARKERS) and bool(
                re.search(r"(?:above|below|within|exceed|高于|低于|超过|范围内|突破)", folded)
            )
        elif validator == "visual_analysis":
            satisfied = "native_chart_analysis_bundle" in extractions
        if not satisfied:
            missing.append(validator)
    if facet.constraints and not all(_constraint_satisfied(constraint, text) for constraint in facet.constraints):
        missing.append("explicit_constraints")
    evidence_ids = {
        str(row.get("evidence_atom_id") or row.get("id") or index)
        for index, row in enumerate(rows, 1)
    }
    contexts = {_context_key(row) for row in rows}
    if len(evidence_ids) < facet.minimum_evidence:
        missing.append("evidence_breadth")
    if len(contexts) < facet.minimum_distinct_contexts:
        missing.append("context_breadth")
    return tuple(dict.fromkeys(missing))


def evaluate_evidence_coverage(
    contract: Iterable[EvidenceFacet],
    evidence: Iterable[Any],
) -> CoverageResult:
    rows = [_evidence_view(item, index) for index, item in enumerate(evidence, 1)]
    results: list[FacetCoverage] = []
    for facet in contract:
        related = [row for row in rows if _is_related(facet, _evidence_text(row))]
        reliable = [
            row
            for row in related
            if float(row.get("confidence") or 1.0) >= 0.65
            and not (row.get("source_kind") == "visual_model" and "numeric" in facet.validators)
        ]
        missing = _missing_validators(facet, reliable)
        evidence_ids = tuple(
            str(row.get("evidence_atom_id") or row.get("id") or f"evidence_{index}")
            for index, row in enumerate(reliable or related, 1)
        )
        if reliable and not missing:
            status: CoverageStatus = "supported"
            reason = "Reliable evidence satisfies every validator in the delivery requirement."
        elif related and not reliable:
            status = "unreliable"
            reason = "Relevant evidence exists, but its source or confidence is not sufficient."
        elif related:
            status = "partial"
            reason = "Related evidence is missing: " + ", ".join(missing) + "."
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
