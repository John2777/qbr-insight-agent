from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .coverage import CoverageResult, build_evidence_contract, evaluate_evidence_coverage, facet_ids_for_evidence
from .query_planning import QueryPlan

BOILERPLATE_MARKERS = (
    "synthetic deployment validation data",
    "target file size",
    "confidential |",
    "all rights reserved",
)

PROVENANCE_MARKERS = (
    "[sources]",
    "source:",
    "sources:",
    "official results",
    "public baseline",
    "synthetic test data",
    "synthetic validation data",
    "illustrative synthetic",
    "generated visual",
    "imagegen",
    "http://",
    "https://",
)

METHODOLOGY_MARKERS = (
    "deterministic synthetic test",
    "implemented with",
    "editable native powerpoint",
    "dual-axis effect",
    "for testing",
)

RISK_MARKERS = (
    "risk",
    "concern",
    "challenge",
    "pressure",
    "underperform",
    "deteriorat",
    "decline",
    "drop",
    "below target",
    "warning",
    "breach",
    "volatile",
    "volatility",
    "风险",
    "担忧",
    "挑战",
    "承压",
    "恶化",
    "下降",
    "下滑",
    "未达",
    "低于目标",
    "波动",
    "集中",
    "预警",
    "红色",
    "黄色",
)

MANAGEMENT_MARKERS = (
    "priority",
    "action",
    "control",
    "mitigation",
    "optimize",
    "reduce",
    "remediate",
    "needs improvement",
    "requires",
    "需要",
    "行动",
    "优先事项",
    "缓释",
    "改善",
    "校准",
    "修复",
    "控制",
    "降低",
    "优化",
)

NEGATIVE_PREDICATE_MARKERS = (
    "above limit",
    "below target",
    "breach",
    "concern",
    "declin",
    "deteriorat",
    "fell",
    "increased",
    "pressure",
    "underperform",
    "volatile",
    "volatility",
    "warning",
    "承压",
    "控制",
    "超过",
    "低于",
    "高于",
    "集中在",
    "集中度",
    "下滑",
    "下降",
    "恶化",
    "未达",
    "波动",
    "担忧",
    "挑战",
    "预警",
)

STOPWORDS = {
    "what",
    "which",
    "where",
    "when",
    "this",
    "that",
    "with",
    "from",
    "have",
    "in",
    "is",
    "of",
    "are",
    "to",
    "does",
    "the",
    "and",
    "ppt",
    "deck",
    "presentation",
    "什么",
    "哪些",
    "这个",
    "这份",
    "一下",
}


def classify_content_role(row: dict[str, Any]) -> str:
    metadata = _loads(str(row.get("metadata_json") or ""), {})
    persisted_role = str(metadata.get("content_role") or "") if isinstance(metadata, dict) else ""
    if persisted_role:
        return persisted_role
    chunk_type = str(row.get("chunk_type") or "text").casefold()
    content = str(row.get("content") or "").strip()
    folded = content.casefold()
    if not content or re.fullmatch(r"\d{1,3}", content):
        return "boilerplate"
    if any(marker in folded for marker in BOILERPLATE_MARKERS):
        return "boilerplate"
    if chunk_type == "notes" or any(marker in folded for marker in PROVENANCE_MARKERS):
        return "methodology" if any(marker in folded for marker in METHODOLOGY_MARKERS) else "provenance"
    if chunk_type == "table":
        return "table"
    if chunk_type.startswith("chart"):
        return "chart"
    if any(marker in folded for marker in RISK_MARKERS):
        return "risk_signal"
    if any(marker in folded for marker in MANAGEMENT_MARKERS):
        return "management_insight"
    return "business_fact"


def _requires_authoritative_numeric_source(question: str) -> bool:
    folded = question.casefold()
    markers = (
        "多少",
        "数值",
        "金额",
        "百分比",
        "准确",
        "exact",
        "value",
        "amount",
        "percentage",
        "percent",
    )
    return any(marker in folded for marker in markers)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _terms(plan: QueryPlan) -> set[str]:
    corpus = " ".join([plan.original_question, plan.canonical_question, *(item.text for item in plan.retrieval_queries)])
    values = re.findall(r"[a-z0-9%_-]{2,}|[\u4e00-\u9fff]{2,}", corpus.casefold())
    return {value for value in values if value not in STOPWORDS}


def _unit_score(unit: str, terms: set[str]) -> float:
    folded = unit.casefold()
    overlap = sum(1.0 for term in terms if term in folded)
    risk = sum(0.8 for marker in RISK_MARKERS if marker in folded)
    numeric = 0.5 if re.search(r"\d", unit) else 0.0
    return overlap + risk + numeric


def _sentence_units(content: str) -> list[str]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) > 1:
        return lines
    units = [item.strip() for item in re.split(r"(?<=[。！？!?;；.])\s+", content) if item.strip()]
    expanded: list[str] = []
    for unit in units:
        if len(unit) <= 800:
            expanded.append(unit)
            continue
        clauses = [item.strip() for item in re.split(r"(?<=[,，:：])\s*", unit) if item.strip()]
        expanded.extend(clauses if len(clauses) > 1 else [unit])
    return expanded


def _extract_table(content: str, terms: set[str], plan: QueryPlan) -> str:
    rows = [line.strip() for line in content.splitlines() if line.strip()]
    if not rows:
        return content.strip()
    header = rows[0]
    body = rows[1:]
    ranked = sorted(enumerate(body), key=lambda item: (-_unit_score(item[1], terms), item[0]))
    selected_indexes = sorted(index for index, row in ranked[:4] if _unit_score(row, terms) > 0)
    if not selected_indexes:
        selected_indexes = list(range(min(3, len(body))))
    selected = [header, *(body[index] for index in selected_indexes)]
    return "\n".join(dict.fromkeys(selected))


def extract_relevant_quote(content: str, plan: QueryPlan, *, chunk_type: str = "text") -> str:
    """Extract complete semantic units; never cut evidence at an arbitrary character offset."""
    content = content.strip()
    if not content:
        return ""
    terms = _terms(plan)
    if chunk_type == "table" or ("|" in content and "\n" in content):
        return _extract_table(content, terms, plan)
    units = _sentence_units(content)
    if not units:
        return content
    ranked = sorted(enumerate(units), key=lambda item: (-_unit_score(item[1], terms), item[0]))
    selected_indexes = [index for index, unit in ranked[:3] if _unit_score(unit, terms) > 0]
    if not selected_indexes:
        selected_indexes = list(range(min(2, len(units))))
    expanded: set[int] = set(selected_indexes)
    for index in selected_indexes:
        if index > 0 and len(units[index - 1]) <= 240:
            expanded.add(index - 1)
    return "\n".join(units[index] for index in sorted(expanded))


def _document_tags(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]", "", text.casefold())
    return set(re.findall(r"qbr0?\d|9mb", normalized))


def infer_facet(content: str, plan: QueryPlan, *, document_title: str = "") -> str:
    def semantic_tokens(text: str) -> set[str]:
        folded = text.casefold()
        tokens = set(re.findall(r"[a-z0-9%_-]{2,}", folded))
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
            if len(phrase) <= 4:
                tokens.add(phrase)
            tokens.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
        return tokens

    content_terms = semantic_tokens(content)
    source_tags = _document_tags(document_title)
    best_requirement = ""
    best_score = 0
    for requirement in plan.evidence_requirements:
        requirement_tags = _document_tags(requirement)
        if requirement_tags and source_tags and requirement_tags.isdisjoint(source_tags):
            continue
        requirement_terms = semantic_tokens(requirement)
        score = len(content_terms & requirement_terms)
        if score > best_score:
            best_requirement = requirement
            best_score = score
    return best_requirement or "directly relevant evidence"


def _is_heading_like(content: str) -> bool:
    compact = re.sub(r"\s+", " ", content).strip()
    folded = compact.casefold()
    if folded.startswith("section "):
        return True
    if compact.endswith(("。", ".", "！", "!", "？", "?", "；", ";")):
        return False
    has_predicate = any(marker in folded for marker in NEGATIVE_PREDICATE_MARKERS)
    latin_letters = re.sub(r"[^A-Za-z]", "", compact)
    if latin_letters and len(compact) <= 80 and latin_letters.upper() == latin_letters and not has_predicate:
        return True
    token_count = len(re.findall(r"[A-Za-z0-9%]+|[\u4e00-\u9fff]{2,}", compact))
    return len(compact) <= 90 and token_count <= 6 and not has_predicate


def _task_requests_context_role(plan: QueryPlan, role: str) -> bool:
    corpus = " ".join(
        [
            plan.original_question,
            plan.canonical_question,
            plan.task_summary,
            plan.answer_brief,
            *plan.operations,
            *plan.evidence_requirements,
            *(item.text for item in plan.retrieval_queries),
        ]
    ).casefold()
    if role == "provenance":
        markers = ("来源", "出处", "公开披露", "数据源", "source", "provenance", "official disclosure", "public baseline")
    else:
        markers = ("方法", "口径", "生成方式", "模拟数据", "method", "methodology", "synthetic", "calculation basis")
    return any(marker in corpus for marker in markers)


def _dedupe_terms(text: str) -> set[str]:
    folded = text.casefold()
    terms = set(re.findall(r"[a-z0-9%_.-]{2,}", folded))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", folded):
        terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


def _near_duplicate(left: str, right: str) -> bool:
    left_terms, right_terms = _dedupe_terms(left), _dedupe_terms(right)
    if not left_terms or not right_terms:
        return False
    return len(left_terms & right_terms) / max(len(left_terms), len(right_terms)) >= 0.88


@dataclass(frozen=True, slots=True)
class EvidenceAtom:
    atom_id: str
    quote: str
    content_role: str
    facet: str
    relevance_score: float
    source: dict[str, Any]
    facet_ids: tuple[str, ...] = ()

    def to_evidence(self) -> dict[str, Any]:
        row = self.source
        return {
            "document_version_id": row["document_version_id"],
            "slide_id": row["slide_id"],
            "element_id": row.get("element_id"),
            "chunk_id": row.get("id"),
            "quote": self.quote,
            "bbox": _loads(row.get("bbox_json"), {}),
            "confidence": float(row.get("confidence") or 1.0),
            "source_kind": row.get("source_kind") or "native_ooxml",
            "document_title": row.get("document_title"),
            "slide_no": row.get("slide_no"),
            "slide_title": row.get("slide_title"),
            "content_role": self.content_role,
            "facet": self.facet,
            "facet_ids": list(self.facet_ids),
            "evidence_atom_id": self.atom_id,
            "extraction": "semantic_units",
        }


@dataclass(frozen=True, slots=True)
class EvidencePack:
    atoms: tuple[EvidenceAtom, ...]
    covered_facets: tuple[str, ...]
    missing_facets: tuple[str, ...]
    answerable: bool
    coverage: CoverageResult
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return [atom.to_evidence() for atom in self.atoms]

    def render_grounding_context(self, plan: QueryPlan) -> str:
        if not self.atoms:
            return (
                "没有检索到可用于回答的文档证据。"
                if plan.answer_language == "zh"
                else "No document evidence was retrieved for this answer."
            )
        heading = "已检索的编号证据：" if plan.answer_language == "zh" else "Retrieved numbered evidence:"
        lines = [f"- {atom.quote} [{index}]" for index, atom in enumerate(self.atoms, 1)]
        return heading + "\n\n" + "\n".join(lines)

    # Kept as a narrow compatibility alias for callers that expect a model-free
    # evidence replay. It contains no inferred business conclusion.
    def render_fallback(self, plan: QueryPlan) -> str:
        return self.render_grounding_context(plan)

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom_count": len(self.atoms),
            "covered_facets": list(self.covered_facets),
            "missing_facets": list(self.missing_facets),
            "answerable": self.answerable,
            "coverage": self.coverage.to_dict(),
            "diagnostics": self.diagnostics,
        }


class EvidencePackBuilder:
    """Turn ranked chunks into small, self-contained, provenance-preserving evidence atoms."""

    def build(
        self,
        plan: QueryPlan,
        candidates: Iterable[dict[str, Any]],
        *,
        max_atoms: int = 8,
        max_total_chars: int = 8_000,
    ) -> EvidencePack:
        rows = list(candidates)
        ranked: list[tuple[float, dict[str, Any], str, str, str]] = []
        role_counts: dict[str, int] = {}
        rejected_roles: dict[str, int] = {}
        rejected_quality: dict[str, int] = {}
        for row in rows:
            role = str(row.get("content_role") or classify_content_role(row))
            role_counts[role] = role_counts.get(role, 0) + 1
            if role in plan.excluded_content_roles or (plan.allowed_content_roles and role not in plan.allowed_content_roles):
                rejected_roles[role] = rejected_roles.get(role, 0) + 1
                continue
            if role in {"provenance", "methodology"} and not _task_requests_context_role(plan, role):
                rejected_roles[role] = rejected_roles.get(role, 0) + 1
                continue
            content = str(row.get("content") or "").strip()
            if (
                str(row.get("source_kind") or "") == "visual_model"
                and _requires_authoritative_numeric_source(plan.original_question)
                and re.search(r"\d", content)
            ):
                rejected_quality["visual_numeric"] = rejected_quality.get("visual_numeric", 0) + 1
                continue
            facet = infer_facet(content, plan, document_title=str(row.get("document_title") or ""))
            quote = extract_relevant_quote(content, plan, chunk_type=str(row.get("chunk_type") or "text"))
            if not quote:
                continue
            semantic_score = _unit_score(quote, _terms(plan))
            if role in {"provenance", "methodology"} and semantic_score <= 0:
                rejected_quality["off_task_context"] = rejected_quality.get("off_task_context", 0) + 1
                continue
            if role in {"business_fact", "risk_signal", "management_insight"} and _is_heading_like(content):
                rejected_quality["heading_like"] = rejected_quality.get("heading_like", 0) + 1
                continue
            score = float(row.get("task_score") or row.get("retrieval_score") or 0.0)
            score += 1.0 if role in {"risk_signal", "management_insight", "table", "chart"} else 0.0
            score += semantic_score
            ranked.append((score, row, role, facet or "direct_answer", quote))
        ranked.sort(key=lambda item: (-item[0], int(item[1].get("slide_no") or 0), str(item[1].get("id") or "")))

        atoms: list[EvidenceAtom] = []
        seen_quotes: set[str] = set()
        per_slide: dict[tuple[str, str], int] = {}
        total_chars = 0
        budget_rejections = 0

        def add_atom(score: float, row: dict[str, Any], role: str, facet: str, quote: str) -> bool:
            nonlocal total_chars, budget_rejections
            normalized = re.sub(r"\s+", " ", quote).casefold()
            if normalized in seen_quotes or any(_near_duplicate(quote, atom.quote) for atom in atoms):
                return False
            slide_key = (str(row.get("document_id") or ""), str(row.get("slide_id") or ""))
            if per_slide.get(slide_key, 0) >= 2:
                return False
            if atoms and total_chars + len(quote) > max_total_chars:
                budget_rejections += 1
                return False
            atoms.append(EvidenceAtom(f"ev_{len(atoms) + 1}", quote, role, facet, round(score, 6), row))
            total_chars += len(quote)
            seen_quotes.add(normalized)
            per_slide[slide_key] = per_slide.get(slide_key, 0) + 1
            return True

        if len(plan.document_ids) > 1:
            for document_id in plan.document_ids:
                for item in ranked:
                    if str(item[1].get("document_id") or "") != document_id:
                        continue
                    if add_atom(*item):
                        break
                if len(atoms) >= max_atoms:
                    break

        for item in ranked:
            if len(atoms) >= max_atoms:
                break
            add_atom(*item)

        covered = tuple(dict.fromkeys(atom.facet for atom in atoms if atom.facet))
        missing = tuple(requirement for requirement in plan.evidence_requirements if requirement not in covered)
        covered_documents = tuple(
            dict.fromkeys(str(atom.source.get("document_id") or "") for atom in atoms if atom.source.get("document_id"))
        )
        missing_documents = tuple(document_id for document_id in plan.document_ids if document_id not in covered_documents)
        minimum_atoms = 2 if plan.execution_profile == "deep" else 1
        answerable = len(atoms) >= minimum_atoms
        contract = build_evidence_contract(plan.original_question)
        coverage = evaluate_evidence_coverage(contract, atoms)
        facet_ids = facet_ids_for_evidence(coverage)
        atoms = [replace(atom, facet_ids=facet_ids.get(atom.atom_id, ())) for atom in atoms]
        return EvidencePack(
            atoms=tuple(atoms),
            covered_facets=covered,
            missing_facets=missing,
            answerable=answerable,
            coverage=coverage,
            diagnostics={
                "candidate_count": len(rows),
                "accepted_count": len(ranked),
                "role_counts": role_counts,
                "rejected_roles": rejected_roles,
                "rejected_quality": rejected_quality,
                "quote_chars": total_chars,
                "budget_rejections": budget_rejections,
                "covered_document_ids": list(covered_documents),
                "missing_document_ids": list(missing_documents),
            },
        )

    @staticmethod
    def with_additional_evidence(
        plan: QueryPlan,
        pack: EvidencePack,
        evidence: Iterable[dict[str, Any]],
    ) -> EvidencePack:
        """Re-evaluate the same contract when a deterministic tool adds evidence."""

        coverage = evaluate_evidence_coverage(
            build_evidence_contract(plan.original_question),
            [*pack.atoms, *evidence],
        )
        facet_ids = facet_ids_for_evidence(coverage)
        atoms = tuple(replace(atom, facet_ids=facet_ids.get(atom.atom_id, ())) for atom in pack.atoms)
        return replace(pack, atoms=atoms, coverage=coverage)
