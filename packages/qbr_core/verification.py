from __future__ import annotations

import re
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
        references = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
        valid_references = set(range(1, len(evidence) + 1))
        citation_failed = bool(evidence and (not references or not references.issubset(valid_references)))

        allowed_corpus = fallback + "\n\n" + "\n\n".join(
            f"{item.get('document_title', '')} {item.get('slide_no', '')} {item.get('quote', '')}"
            for item in evidence
        )
        allowed_facts = numeric_facts(allowed_corpus)
        unsupported = _unsupported_facts(answer, allowed_facts, allowed_corpus)

        warnings: list[str] = []
        if citation_failed:
            warnings.append("LLM_CITATION_VALIDATION_FAILED")
        if unsupported:
            warnings.append("LLM_NUMERIC_VALIDATION_FAILED")

        diagnostics: dict[str, Any] = {
            "references": sorted(references),
            "valid_references": sorted(valid_references),
            "unsupported_numeric_facts": [fact.diagnostic() for fact in unsupported],
            "evidence_roles": sorted({str(item.get("content_role") or "unknown") for item in evidence}),
            "task_summary": str((task_frame or {}).get("task_summary") or ""),
            "disposition": "accepted",
        }
        if not warnings:
            return VerificationResult(True, (), diagnostics)

        repaired, removed = _repair_candidate(
            answer,
            allowed=allowed_facts,
            valid_references=valid_references,
            allowed_corpus=allowed_corpus,
        )
        repaired_references = {int(value) for value in re.findall(r"\[(\d+)\]", repaired)}
        repaired_unsupported = _unsupported_facts(repaired, allowed_facts, allowed_corpus)
        repaired_citations_valid = not evidence or bool(repaired_references and repaired_references <= valid_references)
        diagnostics["removed_claim_segments"] = removed
        if (
            repaired != answer
            and _substantive(repaired)
            and markdown_format_integrity(repaired)
            and repaired_citations_valid
            and not repaired_unsupported
        ):
            diagnostics["disposition"] = "repaired"
            return VerificationResult(True, tuple(warnings), diagnostics, repaired)

        diagnostics["disposition"] = "fallback"
        return VerificationResult(False, tuple(warnings), diagnostics)
