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
    """Deterministic final gate for citation and numeric grounding failures."""

    def verify(
        self,
        answer: str,
        *,
        fallback: str,
        evidence: list[dict[str, Any]],
        task_frame: dict[str, Any] | None = None,
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

        roles = {str(item.get("content_role") or "unknown") for item in evidence}

        diagnostics = {
            "references": sorted(references),
            "valid_references": sorted(valid_references),
            "introduced_numbers": sorted(introduced_numbers),
            "evidence_roles": sorted(roles),
            "task_summary": str((task_frame or {}).get("task_summary") or ""),
        }
        return VerificationResult(not warnings, tuple(dict.fromkeys(warnings)), diagnostics)
