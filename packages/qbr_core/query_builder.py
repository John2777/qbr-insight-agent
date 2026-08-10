from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from .intent_rules import (
    BALANCED_EVALUATION_MARKERS,
    BROAD_QUESTION_MARKERS,
    DOMAIN_EQUIVALENTS,
    INTENT_OPERATIONS,
    NEGATIVE_DIRECT_MARKERS,
    NEGATIVE_DISCOVERY_PATTERNS,
    NEGATIVE_TOPIC_MARKERS,
    OPPORTUNITY_EVALUATION_MARKERS,
    POSITIVE_EVALUATION_MARKERS,
    PROFILE_RANK,
    PROVENANCE_MARKERS,
    RISK_EXPLANATION_MARKERS,
    SUMMARY_MARKERS,
)
from .query_models import QueryPlan, RetrievalQuery
from .terminology import find_term


def _answer_language(question: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", question) else "en"


def _hard_constraints(question: str) -> tuple[str, ...]:
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


def _is_negative_summary(question: str) -> bool:
    folded = question.casefold()
    if any(marker in folded for marker in NEGATIVE_DIRECT_MARKERS):
        return True
    if any(re.search(pattern, folded, flags=re.I) for pattern in NEGATIVE_DISCOVERY_PATTERNS):
        return True
    has_topic = any(marker in folded for marker in NEGATIVE_TOPIC_MARKERS)
    has_broad_scope = any(marker in folded for marker in BROAD_QUESTION_MARKERS)
    return has_topic and has_broad_scope


def _is_risk_explanation(question: str) -> bool:
    """Recognize questions asking what a document risk concept means, not which risks exist."""
    folded = question.casefold()
    has_risk = any(marker in folded for marker in ("risk", "风险"))
    if not has_risk:
        return False
    if any(marker in folded for marker in RISK_EXPLANATION_MARKERS):
        return True
    return bool(
        re.search(r"(?:什么是|何谓|所谓的|这里的|文档中的).{0,24}风险", question)
        or re.search(r"\bwhat\s+is\s+(?:the\s+)?[a-z][a-z -]{1,30}\s+risk\b", folded)
    )


def _evaluation_polarity(question: str) -> str:
    folded = question.casefold()
    if any(marker in folded for marker in BALANCED_EVALUATION_MARKERS):
        return "balanced"
    if any(marker in folded for marker in POSITIVE_EVALUATION_MARKERS):
        return "positive"
    if any(marker in folded for marker in OPPORTUNITY_EVALUATION_MARKERS):
        return "opportunity"
    return "neutral"


def _intent_candidates(question: str) -> tuple[str, ...]:
    candidates: list[str] = []
    if find_term(question) is not None:
        candidates.append("term_definition")
    folded = question.casefold()
    if _is_risk_explanation(question):
        candidates.append("risk_explanation")
    if _evaluation_polarity(question) != "neutral":
        candidates.append("business_evaluation")
    if _is_negative_summary(question):
        candidates.append("negative_signal_summary")
    if any(marker in folded for marker in PROVENANCE_MARKERS) and any(
        marker in folded for marker in ("来源", "边界", "哪些", "which", "what", "source", "official", "synthetic")
    ):
        candidates.append("provenance")
    if any(marker in folded for marker in SUMMARY_MARKERS):
        candidates.append("summary")
    if any(marker in folded for marker in ("图", "趋势", "走势", "控制图", "折线", "柱状", "chart", "trend")):
        candidates.append("chart_analysis")
    if any(marker in folded for marker in ("表", "矩阵", "阈值", "排序", "合计", "table", "matrix", "threshold")):
        candidates.append("table_analysis")
    return tuple(dict.fromkeys(candidates)) or ("evidence_answer",)


def _intent(question: str) -> str:
    return _intent_candidates(question)[0]


def _profile(intent: str) -> str:
    if intent == "term_definition":
        return "fast"
    if intent in {"risk_explanation", "business_evaluation", "negative_signal_summary", "summary"}:
        return "deep"
    if intent in {"chart_analysis", "table_analysis", "provenance"}:
        return "analytical"
    return "focused"


def _profile_for_intents(intents: Iterable[str]) -> str:
    profiles = (_profile(intent) for intent in intents)
    return max(profiles, key=lambda profile: PROFILE_RANK[profile], default="focused")


def _facets(intent: str) -> tuple[str, ...]:
    if intent == "risk_explanation":
        return (
            "explicit_negative_statements",
            "deteriorating_metrics",
            "threshold_pressure",
            "risk_concentration",
            "management_concerns",
        )
    if intent == "business_evaluation":
        return (
            "growth_momentum",
            "profitability_value",
            "cash_capital",
            "operating_quality",
            "portfolio_resilience",
            "execution_delivery",
        )
    if intent == "negative_signal_summary":
        return (
            "explicit_negative_statements",
            "deteriorating_metrics",
            "threshold_pressure",
            "risk_concentration",
            "management_concerns",
        )
    if intent == "summary":
        return ("overall_judgement", "key_metrics", "business_drivers", "risks_and_actions")
    if intent == "provenance":
        return ("official_sources", "synthetic_boundary")
    return ("direct_answer",)


def _facets_for_intents(intents: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(facet for intent in intents for facet in _facets(intent)))


def _content_policy(intent: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if intent == "provenance":
        return (
            ("provenance", "methodology", "business_fact", "table", "chart"),
            ("boilerplate",),
        )
    return (
        ("business_fact", "management_insight", "risk_signal", "table", "chart"),
        ("boilerplate", "methodology", "provenance"),
    )


def _content_policy_for_intents(intents: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    active = tuple(dict.fromkeys(intents))
    if "provenance" in active and len(active) > 1:
        return (
            ("business_fact", "management_insight", "risk_signal", "table", "chart", "provenance", "methodology"),
            ("boilerplate",),
        )
    return _content_policy(active[0] if active else "evidence_answer")


def _operations(question: str, intents: Iterable[str]) -> tuple[str, ...]:
    folded = question.casefold()
    operations = [INTENT_OPERATIONS[intent] for intent in intents]
    if any(marker in folded for marker in ("为什么", "原因", "驱动", "归因", "why", "reason", "driver", "cause")):
        operations.append("explain_drivers")
    if any(marker in folded for marker in ("比较", "对比", "相比", "compare", "versus", " vs ")):
        operations.append("compare")
    if any(marker in folded for marker in ("建议", "怎么办", "行动", "recommend", "what should", "action")):
        operations.append("recommend_actions")
    return tuple(dict.fromkeys(operations))


def _intent_confidence(intents: tuple[str, ...]) -> float:
    if len(intents) > 1:
        return 0.78
    if intents[0] == "evidence_answer":
        return 0.55
    if intents[0] in {"term_definition", "risk_explanation"}:
        return 0.97
    return 0.88


def _dedupe_queries(items: Iterable[RetrievalQuery], *, limit: int = 8) -> tuple[RetrievalQuery, ...]:
    result: list[RetrievalQuery] = []
    seen: set[str] = set()
    for item in items:
        text = re.sub(r"\s+", " ", item.text).strip()
        key = text.casefold()
        if not text or key in seen or len(text) > 300:
            continue
        seen.add(key)
        result.append(replace(item, query_id=f"q{len(result) + 1}", text=text))
        if len(result) >= limit:
            break
    return tuple(result)


def _deterministic_queries(question: str, intent: str) -> tuple[RetrievalQuery, ...]:
    queries = [RetrievalQuery("q1", question, "literal", 1.35)]
    if intent == "risk_explanation":
        execution_specific = any(
            marker in question.casefold() for marker in ("execution", "implementation", "delivery", "执行", "落地", "交付", "实施")
        )
        queries.extend(
            (
                RetrievalQuery(
                    "",
                    (
                        "execution risk implementation delivery slippage priority owner dependency mitigation"
                        if execution_specific
                        else "risk definition driver exposure indicator threshold impact mitigation"
                    ),
                    "concept_semantic",
                    1.2,
                ),
                RetrievalQuery(
                    "",
                    (
                        "执行风险 落地风险 交付风险 优先事项 责任人 依赖项 缓解措施"
                        if execution_specific
                        else "风险 定义 驱动因素 暴露 指标 阈值 影响 缓解措施"
                    ),
                    "concept_cross_language",
                    1.2,
                ),
                RetrievalQuery(
                    "",
                    "risk gate threshold target variance concentration limit warning red amber green",
                    "measurement_semantic",
                    1.1,
                ),
                RetrievalQuery(
                    "",
                    "风险闸门 阈值 目标 偏差 集中度 限额 预警 红灯 黄灯 绿灯",
                    "measurement_cross_language",
                    1.1,
                ),
                RetrievalQuery(
                    "",
                    "management action next quarter agenda control reduce repair optimize",
                    "management_response",
                    1.0,
                ),
            )
        )
    elif intent == "business_evaluation":
        queries.extend(
            (
                RetrievalQuery(
                    "",
                    "strength advantage competitive edge outperformance leading record high momentum",
                    "positive_semantic",
                    1.15,
                ),
                RetrievalQuery("", "优势 亮点 强劲 领先 创纪录 新高 增长 动量 延续", "positive_cross_language", 1.15),
                RetrievalQuery(
                    "",
                    "growth value profit earnings margin ROE ROEV cash generation capital buffer",
                    "financial_strength",
                    1.05,
                ),
                RetrievalQuery(
                    "",
                    "增长 价值 盈利 利润率 现金 自由盈余 资本缓冲 回报率",
                    "financial_cross_language",
                    1.05,
                ),
                RetrievalQuery(
                    "",
                    "persistency productivity digital straight through quality customer retention target above green",
                    "operating_strength",
                    0.95,
                ),
                RetrievalQuery(
                    "",
                    "继续率 产能 数字直通 运营质量 客户留存 超过目标 绿色 达标",
                    "operating_cross_language",
                    0.95,
                ),
                RetrievalQuery(
                    "",
                    "diversified mix concentration below limit regional balance market portfolio",
                    "portfolio_strength",
                    0.9,
                ),
            )
        )
    elif intent == "negative_signal_summary":
        queries.extend(
            (
                RetrievalQuery(
                    "",
                    "risk concentration deteriorating trend decline slowdown compression underperformance below target adverse variance",
                    "business_semantic",
                    1.15,
                ),
                RetrievalQuery("", "warning threshold breach red amber risk limit utilization", "threshold", 1.05),
                RetrievalQuery("", "风险 承压 下滑 下降 回落 放缓 恶化 未达目标 偏差 集中度 波动 挑战 预警", "cross_language", 1.15),
                RetrievalQuery("", "management concern mitigation action priority needs improvement", "management_signal", 0.95),
                RetrievalQuery("", "修复 降低 控制 优化 集中度 资本强度 渠道组合 产品节奏", "management_cross_language", 1.05),
                RetrievalQuery("", "year over year quarter over quarter change negative variance 同比 环比 变化", "trend_discovery", 0.9),
                RetrievalQuery(
                    "",
                    "potential issues weak spots warning signs 潜在问题 经营隐患 薄弱环节 风险点 需要警惕 管理关注点",
                    "latent_issue_discovery",
                    1.0,
                ),
            )
        )
    elif intent == "summary":
        queries.extend(
            (
                RetrievalQuery("", "executive summary key takeaways performance growth profit cash capital risk", "business_semantic", 1.1),
                RetrievalQuery("", "执行摘要 核心结论 业绩 增长 盈利 现金 资本 风险 行动", "cross_language", 1.05),
            )
        )
    elif intent == "provenance":
        queries.extend(
            (
                RetrievalQuery("", "official sources disclosure synthetic illustrative test data", "provenance", 1.2),
                RetrievalQuery("", "公开披露 数据来源 模拟数据 测试数据 口径 边界", "cross_language", 1.1),
            )
        )
    elif intent == "term_definition":
        definition = find_term(question)
        if definition is not None:
            queries.append(RetrievalQuery("", " ".join(definition.search_aliases), "terminology", 1.2))
    else:
        folded = question.casefold()
        expanded = [text for aliases, text in DOMAIN_EQUIVALENTS if any(alias in folded for alias in aliases)]
        if expanded:
            queries.append(RetrievalQuery("", " ".join(expanded), "domain_alias", 1.0))
        focused = " ".join(
            token
            for token in re.findall(r"[A-Za-z0-9%_-]{2,}|[\u4e00-\u9fff]{2,}", question)
            if token.casefold() not in {"what", "which", "this", "that", "the", "and", "with", "from", "ppt"}
        )
        if focused:
            queries.append(RetrievalQuery("", focused, "focused", 1.1))
    return _dedupe_queries(queries)


def _queries_for_intents(question: str, intents: Iterable[str]) -> tuple[RetrievalQuery, ...]:
    active = tuple(dict.fromkeys(intents))
    queries: list[RetrievalQuery] = [RetrievalQuery("q1", question, "literal", 1.35)]
    for intent in active:
        queries.extend(_deterministic_queries(question, intent)[1:])
    return _dedupe_queries(queries, limit=12 if len(active) > 1 else 8)


def deterministic_plan(question: str, document_ids: Iterable[str] = ()) -> QueryPlan:
    question = re.sub(r"\s+", " ", question).strip()
    intents = _intent_candidates(question)
    intent = intents[0]
    allowed, excluded = _content_policy_for_intents(intents)
    polarity = _evaluation_polarity(question)
    if "business_evaluation" in intents and "negative_signal_summary" in intents and polarity != "balanced":
        polarity = "balanced"
    elif intent == "negative_signal_summary":
        polarity = "negative"
    return QueryPlan(
        original_question=question,
        canonical_question=question,
        intent=intent,
        answer_language=_answer_language(question),
        execution_profile=_profile_for_intents(intents),
        document_ids=tuple(document_ids),
        retrieval_queries=_queries_for_intents(question, intents),
        hard_constraints=_hard_constraints(question),
        required_facets=_facets_for_intents(intents),
        allowed_content_roles=allowed,
        excluded_content_roles=excluded,
        evaluation_polarity=polarity,
        secondary_intents=intents[1:],
        operations=_operations(question, intents),
        intent_confidence=_intent_confidence(intents),
    )
