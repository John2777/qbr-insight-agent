from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

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


def _negative_table_signal(row: str, terms: set[str]) -> float:
    folded = row.casefold()
    score = sum(1.0 for term in terms if term in folded)
    if "↑" in row or re.search(r"\b(?:red|amber|warning|breach)\b", folded):
        score += 4.0
    if any(marker in folded for marker in ("红色", "黄色", "预警", "超限", "恶化", "承压", "未达")):
        score += 4.0
    percentages = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)%", row)]
    if percentages and max(percentages) >= 75:
        score += 1.5
    return score


def _extract_table(content: str, terms: set[str], plan: QueryPlan) -> str:
    rows = [line.strip() for line in content.splitlines() if line.strip()]
    if not rows:
        return content.strip()
    header = rows[0]
    body = rows[1:]
    if plan.intent in {"negative_signal_summary", "risk_explanation"}:
        ranked = sorted(enumerate(body), key=lambda item: (-_negative_table_signal(item[1], terms), item[0]))
        selected_indexes = sorted(index for index, row in ranked[:4] if _negative_table_signal(row, terms) > 0)
    else:
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


def infer_facet(content: str, plan: QueryPlan) -> str:
    folded = content.casefold()
    if plan.intent == "business_evaluation":
        if any(marker in folded for marker in ("cash", "capital", "fsg", "solvency", "现金", "资本", "自由盈余", "偿付")):
            return "cash_capital"
        if any(
            marker in folded
            for marker in ("persistency", "productivity", "retention", "stp", "quality", "继续率", "产能", "留存", "直通率", "质量")
        ):
            return "operating_quality"
        if any(
            marker in folded for marker in ("mix", "concentration", "diversif", "portfolio", "regional", "组合", "集中", "多元", "区域")
        ):
            return "portfolio_resilience"
        if any(
            marker in folded
            for marker in ("target", "threshold", "priority", "execution", "green", "目标", "阈值", "优先", "执行", "绿色", "达标")
        ):
            return "execution_delivery"
        if any(
            marker in folded
            for marker in ("profit", "earnings", "margin", "roe", "roev", "value", "vonb", "利润", "盈利", "回报", "价值", "价值率")
        ):
            return "profitability_value"
        if any(marker in folded for marker in ("growth", "increase", "momentum", "record", "增长", "提升", "动量", "新高", "创纪录")):
            return "growth_momentum"
        return ""
    if plan.intent not in {"negative_signal_summary", "risk_explanation"}:
        return plan.required_facets[0] if plan.required_facets else "direct_answer"
    if any(marker in folded for marker in ("threshold", "limit", "warning", "breach", "阈值", "限额", "红色", "黄色", "预警")):
        return "threshold_pressure"
    if any(marker in folded for marker in MANAGEMENT_MARKERS):
        return "management_concerns"
    if any(marker in folded for marker in ("concentration", "exposure", "集中", "暴露")):
        return "risk_concentration"
    if any(marker in folded for marker in ("decline", "drop", "below", "deteriorat", "下降", "下滑", "恶化", "低于", "未达", "承压")):
        return "deteriorating_metrics"
    if any(marker in folded for marker in RISK_MARKERS):
        return "explicit_negative_statements"
    return ""


def _is_heading_like_negative(content: str) -> bool:
    compact = re.sub(r"\s+", " ", content).strip()
    folded = compact.casefold()
    if folded.startswith("section "):
        return True
    has_predicate = any(marker in folded for marker in NEGATIVE_PREDICATE_MARKERS)
    latin_letters = re.sub(r"[^A-Za-z]", "", compact)
    if latin_letters and len(compact) <= 80 and latin_letters.upper() == latin_letters and not has_predicate:
        return True
    token_count = len(re.findall(r"[A-Za-z0-9%]+|[\u4e00-\u9fff]{2,}", compact))
    return len(compact) <= 90 and token_count <= 6 and not has_predicate


def _is_uninterpreted_negative_chart(content: str) -> bool:
    folded = content.casefold()
    has_predicate = any(marker in folded for marker in NEGATIVE_PREDICATE_MARKERS)
    has_status = bool(re.search(r"\b(?:threshold|limit|target|red|amber)\b", folded)) or any(
        marker in folded for marker in ("阈值", "限额", "目标", "红色", "黄色", "红灯", "黄灯")
    )
    return not has_predicate and not has_status


def _comparison_matches(value: float, expression: str) -> bool | None:
    match = re.search(r"(>=|<=|>|<|≥|≤)\s*(-?\d+(?:\.\d+)?)", expression.replace(",", ""))
    if match is None:
        return None
    operator, raw_threshold = match.groups()
    threshold = float(raw_threshold)
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "≥": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "≤": value <= threshold,
    }[operator]


def _all_current_values_green(content: str) -> bool:
    """Recognize native QBR traffic-light tables and suppress all-green rows as bad news."""
    rows = [[cell.strip() for cell in line.split("|")] for line in content.splitlines() if "|" in line]
    if len(rows) < 2:
        return False
    header = [cell.casefold() for cell in rows[0]]

    def column(markers: tuple[str, ...]) -> int | None:
        return next((index for index, cell in enumerate(header) if any(marker in cell for marker in markers)), None)

    green_index = column(("green", "绿"))
    current_index = column(("current", "actual", "当前", "实际"))
    if green_index is None or current_index is None:
        return False
    evaluated: list[bool] = []
    for row in rows[1:]:
        if max(green_index, current_index) >= len(row):
            continue
        current_match = re.search(r"-?\d+(?:\.\d+)?", row[current_index].replace(",", ""))
        if current_match is None:
            continue
        result = _comparison_matches(float(current_match.group(0)), row[green_index])
        if result is not None:
            evaluated.append(result)
    return bool(evaluated) and all(evaluated)


@dataclass(frozen=True, slots=True)
class EvidenceAtom:
    atom_id: str
    quote: str
    content_role: str
    facet: str
    relevance_score: float
    source: dict[str, Any]

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
            "evidence_atom_id": self.atom_id,
            "extraction": "semantic_units",
        }


@dataclass(frozen=True, slots=True)
class EvidencePack:
    atoms: tuple[EvidenceAtom, ...]
    covered_facets: tuple[str, ...]
    missing_facets: tuple[str, ...]
    answerable: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return [atom.to_evidence() for atom in self.atoms]

    def render_fallback(self, plan: QueryPlan) -> str:
        if not self.atoms:
            return (
                "当前文档范围内没有足够的相关业务证据回答这个问题。"
                if plan.answer_language == "zh"
                else "The current document does not contain enough relevant business evidence to answer this question."
            )
        if plan.intent == "negative_signal_summary":
            heading = (
                "文档中最明确的负面信号或管理层关注事项是："
                if plan.answer_language == "zh"
                else "The clearest negative signals or management concerns in the document are:"
            )
        else:
            heading = "与问题直接相关的文档证据如下：" if plan.answer_language == "zh" else "The directly relevant document evidence is:"
        lines = [f"- {atom.quote} [{index}]" for index, atom in enumerate(self.atoms, 1)]
        return heading + "\n\n" + "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom_count": len(self.atoms),
            "covered_facets": list(self.covered_facets),
            "missing_facets": list(self.missing_facets),
            "answerable": self.answerable,
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
            content = str(row.get("content") or "").strip()
            if (
                str(row.get("source_kind") or "") == "visual_model"
                and _requires_authoritative_numeric_source(plan.original_question)
                and re.search(r"\d", content)
            ):
                rejected_quality["visual_numeric"] = rejected_quality.get("visual_numeric", 0) + 1
                continue
            if plan.intent in {"negative_signal_summary", "risk_explanation"}:
                if _is_heading_like_negative(content):
                    rejected_quality["heading_like"] = rejected_quality.get("heading_like", 0) + 1
                    continue
                chunk_type = str(row.get("chunk_type") or "").casefold()
                if chunk_type.startswith("chart") and _is_uninterpreted_negative_chart(content):
                    rejected_quality["uninterpreted_chart"] = rejected_quality.get("uninterpreted_chart", 0) + 1
                    continue
                if chunk_type == "table" and _all_current_values_green(content):
                    rejected_quality["all_green_status_table"] = rejected_quality.get("all_green_status_table", 0) + 1
                    continue
            facet = infer_facet(content, plan)
            if plan.intent in {"business_evaluation", "negative_signal_summary", "risk_explanation"} and not facet:
                continue
            quote = extract_relevant_quote(content, plan, chunk_type=str(row.get("chunk_type") or "text"))
            if not quote:
                continue
            score = float(row.get("task_score") or row.get("retrieval_score") or 0.0)
            score += 2.0 if role in {"risk_signal", "management_insight"} else 1.0 if role in {"table", "chart"} else 0.0
            score += _unit_score(quote, _terms(plan))
            ranked.append((score, row, role, facet or "direct_answer", quote))
        ranked.sort(key=lambda item: (-item[0], int(item[1].get("slide_no") or 0), str(item[1].get("id") or "")))

        atoms: list[EvidenceAtom] = []
        seen_quotes: set[str] = set()
        per_slide: dict[tuple[str, str], int] = {}
        total_chars = 0
        budget_rejections = 0
        for score, row, role, facet, quote in ranked:
            normalized = re.sub(r"\s+", " ", quote).casefold()
            if normalized in seen_quotes:
                continue
            slide_key = (str(row.get("document_id") or ""), str(row.get("slide_id") or ""))
            if per_slide.get(slide_key, 0) >= 2:
                continue
            if atoms and total_chars + len(quote) > max_total_chars:
                budget_rejections += 1
                continue
            atoms.append(EvidenceAtom(f"ev_{len(atoms) + 1}", quote, role, facet, round(score, 6), row))
            total_chars += len(quote)
            seen_quotes.add(normalized)
            per_slide[slide_key] = per_slide.get(slide_key, 0) + 1
            if len(atoms) >= max_atoms:
                break

        covered = tuple(dict.fromkeys(atom.facet for atom in atoms if atom.facet))
        missing = tuple(facet for facet in plan.required_facets if facet not in covered)
        minimum_atoms = 2 if plan.execution_profile == "deep" else 1
        answerable = len(atoms) >= minimum_atoms
        return EvidencePack(
            atoms=tuple(atoms),
            covered_facets=covered,
            missing_facets=missing,
            answerable=answerable,
            diagnostics={
                "candidate_count": len(rows),
                "accepted_count": len(ranked),
                "role_counts": role_counts,
                "rejected_roles": rejected_roles,
                "rejected_quality": rejected_quality,
                "quote_chars": total_chars,
                "budget_rejections": budget_rejections,
            },
        )
