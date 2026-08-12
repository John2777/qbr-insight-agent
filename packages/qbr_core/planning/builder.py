from __future__ import annotations

import re
from collections.abc import Iterable

from packages.qbr_core.planning.language import DOMAIN_EQUIVALENTS, LANGUAGE_BRIDGES
from packages.qbr_core.planning.models import QueryPlan, RetrievalQuery
from packages.qbr_core.planning.terminology import find_term


def _answer_language(question: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", question) else "en"


def _hard_constraints(question: str) -> tuple[str, ...]:
    """Preserve explicit scope; this is a language guard, not intent inference."""
    patterns = (
        r"\b(?:FY|CY)?20\d{2}\b",
        r"\bQ[1-4]\b",
        r"\bH[12]\b",
        r"20\d{2}年",
        r"第[一二三四1-4]季度",
        r"\b\d+(?:\.\d+)?%",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(match.group(0) for match in re.finditer(pattern, question, flags=re.I))
    return tuple(dict.fromkeys(values))


def _focused_terms(question: str) -> str:
    stopwords = {
        "what",
        "which",
        "this",
        "that",
        "the",
        "and",
        "with",
        "from",
        "please",
        "ppt",
        "请问",
        "请帮我",
        "一下",
        "当前",
        "这个",
        "这份",
    }
    tokens = re.findall(r"[A-Za-z0-9%_-]{2,}|[\u4e00-\u9fff]{2,}", question)
    return " ".join(token for token in tokens if token.casefold() not in stopwords)


def _references_visual(question: str) -> bool:
    """Minimal model-free fallback based on visual grammar, not domain vocabulary."""

    return bool(
        re.search(
            r"(?:图(?:表)?|柱(?:状|形)?图|折线|坐标轴|系列|charts?|graphs?|plots?|axes|axis|series|bars?|columns?|lines?)",
            question,
            flags=re.I,
        )
    )


def _delivery_requirements(question: str) -> tuple[str, ...]:
    """Preserve independently answerable user outcomes without naming an intent."""

    clauses = [
        item.strip(" ,，")
        for item in re.split(r"[?？!！;；。]+", question)
        if item.strip(" ,，")
    ]
    return tuple(dict.fromkeys(clauses)) or (question,)


def _linguistic_expansions(question: str) -> list[RetrievalQuery]:
    """Add vocabulary bridges without deciding what kind of question this is."""
    folded = question.casefold()
    expansions: list[RetrievalQuery] = []
    focused = _focused_terms(question)
    if focused and focused.casefold() != folded:
        expansions.append(RetrievalQuery("", focused, "language_focus", 1.1))
    bridge_groups: list[list[RetrievalQuery]] = []
    for aliases, text in DOMAIN_EQUIVALENTS:
        if any(alias in folded for alias in aliases):
            bridge_groups.append(_bridge_token_queries(question, text, "vocabulary_bridge", 1.0))
    for aliases, text in LANGUAGE_BRIDGES:
        if any(alias in folded for alias in aliases):
            bridge_groups.append(_bridge_token_queries(question, text, "paraphrase_bridge", 1.05))
    for index in range(max((len(group) for group in bridge_groups), default=0)):
        expansions.extend(group[index] for group in bridge_groups if index < len(group))
    definition = find_term(question)
    if definition is not None:
        expansions.append(RetrievalQuery("", " ".join(definition.search_aliases), "document_terminology", 1.1))
    return expansions


def _bridge_token_queries(
    question: str,
    bridge: str,
    kind: str,
    weight: float,
    *,
    limit: int = 10,
) -> list[RetrievalQuery]:
    """Turn a vocabulary bridge into independent recall lanes.

    Lexical search may require every token in one query to occur in the same
    chunk.  Keeping synonyms as separate lanes avoids accidentally making a
    broad bridge more restrictive than the user's literal wording.
    """

    tokens = list(dict.fromkeys(re.findall(r"[a-z0-9%_.-]{2,}|[\u4e00-\u9fff]{2,}", bridge.casefold())))
    preferred_cjk = _answer_language(question) == "zh"
    primary = [token for token in tokens if bool(re.search(r"[\u4e00-\u9fff]", token)) == preferred_cjk]
    secondary = [token for token in tokens if token not in primary]

    def sample(values: list[str], count: int) -> list[str]:
        if len(values) <= count:
            return values
        if count <= 1:
            return values[:count]
        indexes = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
        return [values[index] for index in dict.fromkeys(indexes)]

    selected = [*sample(primary, 7), *sample(secondary, 3)][:limit]
    return [RetrievalQuery("", token, kind, weight) for token in selected]


def _dedupe_queries(items: Iterable[RetrievalQuery], *, limit: int = 8) -> tuple[RetrievalQuery, ...]:
    result: list[RetrievalQuery] = []
    seen: set[str] = set()
    for item in items:
        text = re.sub(r"\s+", " ", item.text).strip()
        key = text.casefold()
        if not text or key in seen or len(text) > 300:
            continue
        seen.add(key)
        result.append(RetrievalQuery(f"q{len(result) + 1}", text, item.kind, item.weight))
        if len(result) >= limit:
            break
    return tuple(result)


def linguistic_plan(question: str, document_ids: Iterable[str] = ()) -> QueryPlan:
    """Safe model-free task frame used only when semantic planning is unavailable."""
    question = re.sub(r"\s+", " ", question).strip()
    language = _answer_language(question)
    constraints = _hard_constraints(question)
    requirements = tuple(f"Preserve the explicit constraint: {value}" for value in constraints) or (
        "Use evidence that directly resolves the user's wording",
    )
    brief = (
        "直接完成用户提出的任务；只使用可核验证据，并让结构服从问题本身。"
        if language == "zh"
        else "Complete the user's task directly using verifiable evidence and a structure dictated by the question."
    )
    queries = _dedupe_queries(
        (RetrievalQuery("", question, "literal", 1.35), *_linguistic_expansions(question)),
        limit=14,
    )
    return QueryPlan(
        original_question=question,
        canonical_question=question,
        task_summary=question,
        answer_brief=brief,
        answer_language=language,
        execution_profile="focused",
        document_ids=tuple(document_ids),
        retrieval_queries=queries,
        hard_constraints=constraints,
        delivery_requirements=_delivery_requirements(question),
        evidence_requirements=requirements,
        needs_visuals=_references_visual(question),
        planner_confidence=0.35,
    )


# Compatibility name for callers outside the package.  It no longer performs
# deterministic intent classification; it creates the linguistic fallback frame.
deterministic_plan = linguistic_plan


__all__ = [
    "_answer_language",
    "_dedupe_queries",
    "_hard_constraints",
    "deterministic_plan",
    "linguistic_plan",
]
