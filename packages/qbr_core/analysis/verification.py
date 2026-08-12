from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<prefix>US\$|USD|RMB|CNY|CN¥|HK\$|HKD|\$|¥|€|£)?\s*"
    r"(?P<number>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<suffix>percentage\s+points?|个百分点|basis\s+points?|个基点|bps?|％|%|倍|x|"
    r"billions?|millions?|thousands?|bn|mn|m|k|十亿|百万|亿|万)?(?![A-Za-z])",
    flags=re.I,
)

_SCALE_FACTORS = {
    "k": Decimal("1000"),
    "thousand": Decimal("1000"),
    "thousands": Decimal("1000"),
    "万": Decimal("10000"),
    "m": Decimal("1000000"),
    "mn": Decimal("1000000"),
    "million": Decimal("1000000"),
    "millions": Decimal("1000000"),
    "百万": Decimal("1000000"),
    "亿": Decimal("100000000"),
    "bn": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "billions": Decimal("1000000000"),
    "十亿": Decimal("1000000000"),
}

_CURRENCY_UNITS = {
    "us$": "currency:usd",
    "usd": "currency:usd",
    "$": "currency:usd",
    "rmb": "currency:cny",
    "cny": "currency:cny",
    "cn¥": "currency:cny",
    "¥": "currency:cny",
    "hk$": "currency:hkd",
    "hkd": "currency:hkd",
    "€": "currency:eur",
    "£": "currency:gbp",
}

_ORDINAL_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-+*]\s+)?(?:#{1,6}\s*)?(?:\*{1,2}|_{1,2})?)"
    r"(?P<marker>\d{1,3}[.)、])\s+"
)

_CONTEXT_UNIT_RE = re.compile(
    r"(?P<prefix>US\$|USD|RMB|CNY|CN¥|HK\$|HKD|\$|¥|€|£)\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mn|m|k|十亿|百万|亿|万)(?![A-Za-z])",
    flags=re.I,
)

_CONTEXT_CURRENCY_RE = re.compile(
    r"(?P<prefix>US\$|USD|RMB|CNY|CN¥|HK\$|HKD|\$|¥|€|£)\s*$",
    flags=re.I,
)

_CONTEXT_SCALE_RE = re.compile(
    r"(?<![A-Za-z])(?P<scale>billions?|millions?|thousands?|bn|mn|m|k|十亿|百万|亿|万)\s*$",
    flags=re.I,
)

_DERIVATION_MARKERS = (
    "同比",
    "环比",
    "增长",
    "下降",
    "上升",
    "提升",
    "降低",
    "减少",
    "增加",
    "差异",
    "差额",
    "缓冲",
    "高于",
    "低于",
    "剔除",
    "变化",
    "change",
    "growth",
    "increase",
    "decrease",
    "decline",
    "difference",
    "buffer",
    "above",
    "below",
)


@dataclass(frozen=True, slots=True)
class NumericFact:
    """One semantic numeric fact, normalized independently of its display form."""

    value: str
    unit: str | None
    raw: str
    display_value: str

    def diagnostic(self) -> dict[str, str | None]:
        """Return diagnostic for this numeric fact."""
        return {"value": self.value, "unit": self.unit, "raw": self.raw}


def _mask_structural_numbers(text: str) -> str:
    """Remove citation labels and Markdown ordinals before business-number parsing."""

    masked = re.sub(r"\[\d+\]", "", text)
    lines: list[str] = []
    for line in masked.splitlines():
        lines.append(_ORDINAL_RE.sub(lambda match: match.group("prefix"), line))
    return "\n".join(lines)


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _numeric_context(cell: str) -> tuple[str | None, Decimal] | None:
    combined = _CONTEXT_UNIT_RE.search(cell)
    if combined:
        return (
            _CURRENCY_UNITS.get(combined.group("prefix").casefold()),
            _SCALE_FACTORS[combined.group("scale").casefold()],
        )
    currency = _CONTEXT_CURRENCY_RE.search(cell)
    if currency:
        return _CURRENCY_UNITS.get(currency.group("prefix").casefold()), Decimal("1")
    scale = _CONTEXT_SCALE_RE.search(cell)
    if scale:
        return None, _SCALE_FACTORS[scale.group("scale").casefold()]
    return None


def _standalone_numeric_context(cell: str) -> tuple[str | None, Decimal] | None:
    compact = re.sub(r"\s+", "", cell)
    context = _numeric_context(compact)
    if context is None:
        return None
    context_tokens = re.sub(
        r"(?i)(US\$|USD|RMB|CNY|CN¥|HK\$|HKD|\$|¥|€|£|billions?|millions?|thousands?|bn|mn|m|k|十亿|百万|亿|万)",
        "",
        compact,
    )
    return context if not context_tokens else None


def numeric_facts(text: str) -> tuple[NumericFact, ...]:
    """Extract locale-tolerant numeric facts without treating formatting ordinals as data."""

    facts: list[NumericFact] = []
    table_contexts: dict[int, tuple[str | None, Decimal]] = {}
    for source_line in _mask_structural_numbers(text).splitlines():
        line = re.sub(
            r"(?i)(?<!\d)\d{1,3}\s*(?:M|个?月)(?=\s*(?:继续率|续保率|留存率|persistency))",
            "",
            source_line,
        )
        cells = line.split("|") if "|" in line else [line]
        if "|" not in line:
            table_contexts = {}
        row_context: tuple[str | None, Decimal] | None = None
        for index, cell in enumerate(cells):
            context = _numeric_context(cell)
            if context is not None and "|" in line:
                table_contexts[index] = context
            row_context = _standalone_numeric_context(cell) or row_context

        for index, cell in enumerate(cells):
            contextual_unit, contextual_scale = (
                _numeric_context(cell) or table_contexts.get(index) or row_context or (None, Decimal("1"))
            )
            for match in _NUMBER_RE.finditer(cell):
                raw_number = match.group("number").replace(",", "")
                try:
                    display_value = Decimal(raw_number)
                except InvalidOperation:
                    continue
                value = display_value
                prefix = (match.group("prefix") or "").casefold()
                suffix = re.sub(r"\s+", " ", (match.group("suffix") or "").casefold())
                scale = _SCALE_FACTORS.get(suffix)
                if scale is not None:
                    value *= scale

                unit = _CURRENCY_UNITS.get(prefix)
                if suffix in {"%", "％"}:
                    unit = "percent"
                elif suffix in {"percentage point", "percentage points", "个百分点"}:
                    unit = "percentage_point"
                elif suffix in {"basis point", "basis points", "bp", "bps", "个基点"}:
                    unit = "basis_point"
                elif suffix in {"x", "倍"}:
                    unit = "multiple"
                elif not prefix and not suffix and (index in table_contexts or row_context is not None):
                    value *= contextual_scale
                    unit = contextual_unit
                facts.append(
                    NumericFact(
                        _decimal_text(value),
                        unit,
                        match.group(0).strip(),
                        _decimal_text(display_value),
                    )
                )
    return tuple(facts)


def normalized_numbers(text: str) -> set[tuple[str, bool]]:
    """Compatibility view used by existing callers and tests."""

    return {(fact.value, fact.unit == "percent") for fact in numeric_facts(text)}


def _units_compatible(answer_unit: str | None, evidence_unit: str | None) -> bool:
    # Missing units are treated as contextual, not conflicting. Explicitly
    # incompatible units (for example USD versus percent) still fail closed.
    return answer_unit is None or evidence_unit is None or answer_unit == evidence_unit


def _fact_supported(fact: NumericFact, allowed: tuple[NumericFact, ...]) -> bool:
    return any(
        (candidate.value == fact.value and _units_compatible(fact.unit, candidate.unit))
        or (fact.unit is None and candidate.display_value == fact.display_value)
        for candidate in allowed
    )


def _approximately_equal(left: Decimal, right: Decimal) -> bool:
    tolerance = max(Decimal("0.05"), abs(right) * Decimal("0.002"))
    return abs(left - right) <= tolerance


def _derived_fact_supported(fact: NumericFact, allowed: tuple[NumericFact, ...], segment: str) -> bool:
    """Accept only simple, auditable arithmetic when the prose explicitly signals a derivation."""

    folded = segment.casefold()
    if not any(marker in folded for marker in _DERIVATION_MARKERS):
        return False
    try:
        target = Decimal(fact.value)
    except InvalidOperation:
        return False
    unique_values = list(dict.fromkeys((item.value, item.unit) for item in allowed))[:160]
    candidates = [(Decimal(value), unit) for value, unit in unique_values]
    for left, left_unit in candidates:
        for right, right_unit in candidates:
            if left == right or not _units_compatible(left_unit, right_unit):
                continue
            difference = abs(left - right)
            if fact.unit == "percentage_point" and left_unit in {"percent", "percentage_point", None}:
                if _approximately_equal(difference, target):
                    return True
            elif fact.unit in {None, left_unit, right_unit} and _approximately_equal(difference, target):
                return True
            if right != 0:
                if fact.unit == "percent":
                    percentage_change = abs((left - right) / right * Decimal("100"))
                    if _approximately_equal(percentage_change, target):
                        return True
                if fact.unit == "multiple":
                    ratio = abs(left / right)
                    if _approximately_equal(ratio, target):
                        return True
    return False


def _calendar_year_supported(fact: NumericFact, allowed_corpus: str) -> bool:
    if fact.unit is not None:
        return False
    try:
        year = int(Decimal(fact.value))
    except (InvalidOperation, ValueError):
        return False
    if year < 1900 or year > 2100:
        return False
    short_year = year % 100
    return bool(
        re.search(rf"(?<!\d){year}(?!\d)", allowed_corpus)
        or re.search(rf"(?<!\d){short_year:02d}/\d{{1,2}}(?!\d)", allowed_corpus)
    )


def _fact_supported_in_context(
    fact: NumericFact,
    allowed: tuple[NumericFact, ...],
    segment: str,
    allowed_corpus: str,
) -> bool:
    return (
        _fact_supported(fact, allowed)
        or _calendar_year_supported(fact, allowed_corpus)
        or _derived_fact_supported(fact, allowed, segment)
    )


def _unsupported_facts(answer: str, allowed: tuple[NumericFact, ...], allowed_corpus: str) -> list[NumericFact]:
    unsupported: list[NumericFact] = []
    for line in answer.splitlines():
        for segment in _segments(line):
            unsupported.extend(
                fact
                for fact in numeric_facts(segment)
                if not _fact_supported_in_context(fact, allowed, segment, allowed_corpus)
            )
    return _unique_facts(unsupported)


def _unique_facts(facts: list[NumericFact]) -> list[NumericFact]:
    result: list[NumericFact] = []
    seen: set[tuple[str, str | None]] = set()
    for fact in facts:
        key = (fact.value, fact.unit)
        if key not in seen:
            seen.add(key)
            result.append(fact)
    return result


def _segments(line: str) -> list[str]:
    if not line.strip() or line.lstrip().startswith("|"):
        return [line]
    protected = line
    ordinal = _ORDINAL_RE.match(line)
    if ordinal and ordinal.group("marker").endswith("."):
        position = ordinal.end("marker") - 1
        protected = line[:position] + "\ue000" + line[position + 1 :]
    sentence_end = r"(?:[。！？!?]|\.(?!\d))(?:\s*\[\d+\])*"
    return [item.replace("\ue000", ".") for item in re.findall(rf".+?(?:{sentence_end}|$)", protected) if item]


def _remove_orphan_headings(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []

    def is_heading(line: str) -> bool:
        stripped = line.strip()
        return bool(
            re.match(r"^#{1,6}\s+", stripped)
            or re.fullmatch(r"(?:\*\*|__)[^\n]+(?:\*\*|__)", stripped)
        )

    for index, line in enumerate(lines):
        if not is_heading(line):
            kept.append(line)
            continue
        following = next((item for item in lines[index + 1 :] if item.strip()), "")
        if following and not is_heading(following):
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _repair_candidate(
    answer: str,
    *,
    allowed: tuple[NumericFact, ...],
    valid_references: set[int],
    allowed_corpus: str,
) -> tuple[str, int]:
    """Delete only unsupported claim segments while preserving grounded prose."""

    repaired_lines: list[str] = []
    removed = 0
    for line in answer.splitlines():
        kept_segments: list[str] = []
        for segment in _segments(line):
            references = {int(value) for value in re.findall(r"\[(\d+)\]", segment)}
            invalid_reference = bool(references - valid_references)
            unsupported_number = any(
                not _fact_supported_in_context(fact, allowed, segment, allowed_corpus)
                for fact in numeric_facts(segment)
            )
            if invalid_reference or unsupported_number:
                removed += 1
                continue
            kept_segments.append(segment)
        repaired_lines.append("".join(kept_segments).rstrip())
    return _remove_orphan_headings("\n".join(repaired_lines)), removed


def _substantive(text: str) -> bool:
    plain = re.sub(r"\[\d+\]|[#*_>`|\-]", "", text)
    return len(re.sub(r"\s+", "", plain)) >= 12


def _normalized_series_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)


def _mentions_series(text: str, name: str) -> bool:
    """Verify the authoritative source label without maintaining translation aliases."""

    normalized_name = _normalized_series_text(name)
    return bool(normalized_name and normalized_name in _normalized_series_text(text))


def _series_pattern(name: str) -> re.Pattern[str] | None:
    """Compile a separator-tolerant source-label pattern without translation aliases."""

    normalized = unicodedata.normalize("NFKC", name).strip()
    parts = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", normalized)
    if not parts:
        return None
    body = r"[^\w\u4e00-\u9fff]*".join(re.escape(part) for part in parts)
    if any(re.search(r"[A-Za-z0-9]", part) for part in parts):
        body = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    return re.compile(body, flags=re.I)


def _series_spans(text: str, name: str) -> list[tuple[int, int]]:
    pattern = _series_pattern(name)
    return [match.span() for match in pattern.finditer(unicodedata.normalize("NFKC", text))] if pattern else []


def _strong_excluded_series_mention(text: str, name: str, selected_names: list[str]) -> bool:
    """Detect authoritative out-of-scope labels while rejecting substring collisions.

    Latin labels require token boundaries. CJK labels must end at a CJK boundary,
    so ordinary compounds such as ``保障型`` are not treated as a series mention.
    Any occurrence nested inside a longer selected source label is also ignored.
    """

    normalized_text = unicodedata.normalize("NFKC", text)
    selected_spans = [span for selected in selected_names for span in _series_spans(normalized_text, selected)]
    contains_cjk = bool(re.search(r"[\u4e00-\u9fff]", name))
    for start, end in _series_spans(normalized_text, name):
        if any(selected_start <= start and end <= selected_end for selected_start, selected_end in selected_spans):
            continue
        if contains_cjk and end < len(normalized_text) and re.match(r"[\u4e00-\u9fff]", normalized_text[end]):
            continue
        return True
    return False


_CAUSAL_ASSERTION = re.compile(
    r"(?:导致|造成|所致|驱动|依赖于?|意味着|受.{0,40}影响|due\s+to|driven\s+by|caused\s+by|"
    r"results?\s+from|depends?\s+on|means?\s+that)",
    flags=re.I,
)
_CAUSAL_BOUNDARY = re.compile(
    r"(?:不代表|不能|无法|未(?:能)?证明|不可(?:据此)?|待验证|(?:需(?:要)?|有待).{0,60}(?:验证|查证|确认)|"
    r"需要.{0,30}(?:数据|证据)|"
    r"not\s+(?:causal|causality|proof)|cannot|unable\s+to|needs?\s+(?:validation|evidence|data)|"
    r"requires?\s+(?:validation|evidence|data))",
    flags=re.I,
)


def _unsupported_chart_inferences(answer: str, scope: dict[str, Any] | None) -> list[str]:
    """Return causal chart claims that do not state their own evidence boundary."""

    if not isinstance(scope, dict) or not scope.get("no_pairwise_mapping"):
        return []
    violations: list[str] = []
    for segment in _segments(answer):
        cleaned = segment.strip()
        if cleaned and _CAUSAL_ASSERTION.search(cleaned) and not _CAUSAL_BOUNDARY.search(cleaned):
            violations.append(cleaned)
    return violations


_ABSOLUTE_NEGATIVE = re.compile(
    r"(?:不存在|没有任何|均不存在|全部(?:正常|安全|合规)|无任何|无法识别出任何|"
    r"\bno\s+(?:specific|material|potential|company|entity|risk|issue)s?\b|"
    r"\bnone\b|\ball\s+(?:are|is|remain)\s+(?:safe|normal|compliant)\b)",
    flags=re.I,
)
_EVIDENCE_BOUNDARY = re.compile(
    r"(?:没有|未能?|无法).{0,12}(?:检索|找到|确认|判断)|证据不足|资料不足|"
    r"(?:not|no).{0,12}(?:retrieved|found)|insufficient\s+evidence|cannot\s+(?:confirm|determine)",
    flags=re.I,
)


def _claim_terms(text: str) -> set[str]:
    folded = text.casefold()
    terms = set(re.findall(r"[a-z0-9_.%-]{2,}", folded))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


def _unsupported_absolute_negatives(answer: str, evidence_corpus: str) -> list[str]:
    """Reject universal negatives inferred only from retrieval absence."""

    evidence_segments = [
        segment.strip()
        for segment in _segments(evidence_corpus)
        if _ABSOLUTE_NEGATIVE.search(segment) and not _EVIDENCE_BOUNDARY.search(segment)
    ]
    unsupported: list[str] = []
    for segment in _segments(answer):
        claim = segment.strip()
        if not claim or not _ABSOLUTE_NEGATIVE.search(claim) or _EVIDENCE_BOUNDARY.search(claim):
            continue
        claim_terms = _claim_terms(claim)
        supported = any(len(claim_terms & _claim_terms(source)) >= 2 for source in evidence_segments)
        if not supported:
            unsupported.append(claim)
    return unsupported


def _remove_claim_segments(answer: str, claims: list[str]) -> tuple[str, int]:
    """Remove exact claim segments while preserving the rest of each line."""

    rejected = set(claims)
    repaired_lines: list[str] = []
    removed = 0
    for line in answer.splitlines():
        kept: list[str] = []
        for segment in _segments(line):
            if segment.strip() in rejected:
                removed += 1
            else:
                kept.append(segment)
        repaired_line = "".join(kept).rstrip()
        if repaired_line or not line.strip():
            repaired_lines.append(repaired_line)
    return _remove_orphan_headings("\n".join(repaired_lines)), removed


def _chart_scope_diagnostics(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    scope = next(
        (
            item.get("chart_scope")
            for item in evidence
            if isinstance(item.get("chart_scope"), dict) and item["chart_scope"].get("kind") == "chart_analysis"
        ),
        None,
    )
    if not isinstance(scope, dict):
        return {"active": False, "outside_scope_series": [], "missing_family_coverage": {}}

    selected_names = [name for name in scope.get("selected_series_names", []) if isinstance(name, str) and name]
    outside = [
        name
        for name in scope.get("excluded_document_series_names", [])
        if isinstance(name, str) and name and _strong_excluded_series_mention(answer, name, selected_names)
    ]
    minimum = scope.get("minimum_family_mentions") or 0
    missing: dict[str, dict[str, Any]] = {}
    for family, names in dict(scope.get("family_series") or {}).items():
        if family not in {"bar", "line"} or not isinstance(names, list) or not names:
            continue
        mentioned = [name for name in names if isinstance(name, str) and _mentions_series(answer, name)]
        if isinstance(minimum, dict):
            required = min(max(0, int(minimum.get(family) or 0)), len(names))
        else:
            required = min(max(0, int(minimum)), len(names))
        if len(mentioned) < required:
            missing[str(family)] = {"required": required, "mentioned": mentioned, "available": names}
    return {
        "active": True,
        "outside_scope_series": sorted(set(outside)),
        "missing_family_coverage": missing,
        "selected_series_names": scope.get("selected_series_names", []),
        "no_pairwise_mapping": bool(scope.get("no_pairwise_mapping")),
    }


def markdown_format_integrity(text: str) -> bool:
    """Check paired Markdown delimiters without treating thematic breaks as emphasis."""

    emphasis_text = "\n".join(
        ""
        if re.fullmatch(r"\s{0,3}(?:(?:\*\s*){3,}|(?:_\s*){3,})", line)
        else line
        for line in text.splitlines()
    )
    return (
        emphasis_text.count("**") % 2 == 0
        and emphasis_text.count("__") % 2 == 0
        and text.count("```") % 2 == 0
    )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Report whether an answer is supported and expose salvage diagnostics."""
    accepted: bool
    warnings: tuple[str, ...]
    diagnostics: dict[str, Any]
    repaired_answer: str | None = None


class ClaimEvidenceVerifier:
    """Verify grounding and salvage supported claim segments when possible."""

    def verify(
        self,
        answer: str,
        *,
        fallback: str,
        evidence: list[dict[str, Any]],
        task_frame: dict[str, Any] | None = None,
    ) -> VerificationResult:
        """Verify answer claims against the supplied evidence."""
        references = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
        valid_references = set(range(1, len(evidence) + 1))
        citation_failed = bool(evidence and (not references or not references.issubset(valid_references)))

        allowed_corpus = fallback + "\n\n" + "\n\n".join(
            f"{item.get('document_title', '')} {item.get('slide_no', '')} {item.get('quote', '')}"
            for item in evidence
        )
        allowed_facts = numeric_facts(allowed_corpus)
        unsupported = _unsupported_facts(answer, allowed_facts, allowed_corpus)
        chart_scope = _chart_scope_diagnostics(answer, evidence)
        unsupported_inferences = _unsupported_chart_inferences(answer, chart_scope)
        unsupported_negatives = _unsupported_absolute_negatives(answer, allowed_corpus)

        warnings: list[str] = []
        if citation_failed:
            warnings.append("LLM_CITATION_VALIDATION_FAILED")
        if unsupported:
            warnings.append("LLM_NUMERIC_VALIDATION_FAILED")
        if chart_scope["outside_scope_series"]:
            warnings.append("LLM_CHART_SCOPE_VALIDATION_FAILED")
        if chart_scope["missing_family_coverage"]:
            warnings.append("LLM_CHART_COVERAGE_VALIDATION_FAILED")
        if unsupported_inferences:
            warnings.append("LLM_CHART_INFERENCE_VALIDATION_FAILED")
        if unsupported_negatives:
            warnings.append("LLM_ABSOLUTE_NEGATIVE_VALIDATION_FAILED")

        diagnostics: dict[str, Any] = {
            "references": sorted(references),
            "valid_references": sorted(valid_references),
            "unsupported_numeric_facts": [fact.diagnostic() for fact in unsupported],
            "evidence_roles": sorted({str(item.get("content_role") or "unknown") for item in evidence}),
            "original_question": str((task_frame or {}).get("original_question") or ""),
            "chart_scope": chart_scope,
            "unsupported_chart_inferences": unsupported_inferences,
            "unsupported_absolute_negatives": unsupported_negatives,
            "disposition": "accepted",
        }
        if not warnings:
            return VerificationResult(True, (), diagnostics)

        inference_repaired, inference_removed = _remove_claim_segments(
            answer,
            [*unsupported_inferences, *unsupported_negatives],
        )
        repaired, numeric_removed = _repair_candidate(
            inference_repaired,
            allowed=allowed_facts,
            valid_references=valid_references,
            allowed_corpus=allowed_corpus,
        )
        repaired_references = {int(value) for value in re.findall(r"\[(\d+)\]", repaired)}
        repaired_unsupported = _unsupported_facts(repaired, allowed_facts, allowed_corpus)
        repaired_citations_valid = not evidence or bool(repaired_references and repaired_references <= valid_references)
        repaired_chart_scope = _chart_scope_diagnostics(repaired, evidence)
        repaired_inferences = _unsupported_chart_inferences(repaired, repaired_chart_scope)
        diagnostics["removed_claim_segments"] = inference_removed + numeric_removed
        if (
            repaired != answer
            and _substantive(repaired)
            and markdown_format_integrity(repaired)
            and repaired_citations_valid
            and not repaired_unsupported
            and not repaired_chart_scope["outside_scope_series"]
            and not repaired_chart_scope["missing_family_coverage"]
            and not repaired_inferences
        ):
            diagnostics["disposition"] = "repaired"
            return VerificationResult(True, tuple(warnings), diagnostics, repaired)

        diagnostics["disposition"] = "fallback"
        return VerificationResult(False, tuple(warnings), diagnostics)
