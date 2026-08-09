from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def normalized_numbers(text: str) -> set[tuple[str, bool]]:
    text = re.sub(r"\[\d+\]", "", text)
    values: set[tuple[str, bool]] = set()
    for match in re.finditer(r"(?<![\w])[-+]?\d+(?:\.\d+)?\s*%?", text):
        raw = match.group(0).strip()
        percent = raw.endswith("%")
        try:
            number = Decimal(raw.rstrip("%").strip()).normalize()
        except InvalidOperation:
            continue
        values.add((format(number, "f"), percent))
    return values


@dataclass(frozen=True, slots=True)
class VerificationResult:
    accepted: bool
    warnings: tuple[str, ...]
    diagnostics: dict[str, Any]


class ClaimEvidenceVerifier:
    """Deterministic final gate for citation, numeric, source-role and query-adherence failures."""

    def verify(
        self,
        answer: str,
        *,
        fallback: str,
        evidence: list[dict[str, Any]],
        query_plan: dict[str, Any] | None = None,
    ) -> VerificationResult:
        warnings: list[str] = []
        references = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
        valid_references = set(range(1, len(evidence) + 1))
        if evidence and (not references or not references.issubset(valid_references)):
            warnings.append("LLM_CITATION_VALIDATION_FAILED")

        allowed_corpus = (
            fallback
            + "\n"
            + "\n".join(f"{item.get('document_title', '')} {item.get('slide_no', '')} {item.get('quote', '')}" for item in evidence)
        )
        introduced_numbers = normalized_numbers(answer) - normalized_numbers(allowed_corpus)
        if introduced_numbers:
            warnings.append("LLM_NUMERIC_VALIDATION_FAILED")

        intent = str((query_plan or {}).get("intent") or "evidence_answer")
        roles = {str(item.get("content_role") or "unknown") for item in evidence}
        if (
            evidence
            and intent != "provenance"
            and roles
            and "unknown" not in roles
            and roles <= {"provenance", "methodology", "boilerplate"}
        ):
            warnings.append("EVIDENCE_ROLE_VALIDATION_FAILED")

        folded = answer.casefold()
        if intent != "provenance" and any(
            marker in folded
            for marker in ("[sources]", "generated visual:", "implemented with two aligned editable", "all monthly management data")
        ):
            warnings.append("LLM_QUERY_ADHERENCE_FAILED")

        diagnostics = {
            "references": sorted(references),
            "valid_references": sorted(valid_references),
            "introduced_numbers": sorted(introduced_numbers),
            "evidence_roles": sorted(roles),
        }
        return VerificationResult(not warnings, tuple(dict.fromkeys(warnings)), diagnostics)
