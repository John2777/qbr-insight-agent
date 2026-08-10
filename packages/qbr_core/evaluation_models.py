from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .negative_models import NegativeSignal
from .query_models import QueryPlan

EvaluationFacet = Literal[
    "growth_momentum",
    "profitability_value",
    "cash_capital",
    "operating_quality",
    "portfolio_resilience",
    "execution_delivery",
]

FACET_ORDER: tuple[EvaluationFacet, ...] = (
    "growth_momentum",
    "profitability_value",
    "cash_capital",
    "operating_quality",
    "portfolio_resilience",
    "execution_delivery",
)

FACET_LABELS: dict[EvaluationFacet, tuple[str, str]] = {
    "growth_momentum": ("增长与动量", "Growth and momentum"),
    "profitability_value": ("盈利与价值创造", "Profitability and value creation"),
    "cash_capital": ("现金与资本", "Cash and capital"),
    "operating_quality": ("运营质量", "Operating quality"),
    "portfolio_resilience": ("组合与韧性", "Portfolio resilience"),
    "execution_delivery": ("执行与目标达成", "Execution and target delivery"),
}


@dataclass(frozen=True, slots=True)
class EvaluationSignal:
    facet: EvaluationFacet
    claim_zh: str
    claim_en: str
    evidence: dict[str, Any]
    score: float
    synthetic: bool = False

    def claim(self, language: str) -> str:
        return self.claim_zh if language == "zh" else self.claim_en


@dataclass(frozen=True, slots=True)
class EvaluationAssessment:
    signals: tuple[EvaluationSignal, ...]
    caveats: tuple[NegativeSignal, ...]
    analyzed_sources: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def render(self, plan: QueryPlan) -> tuple[str, list[dict[str, Any]], list[str]]:
        language = plan.answer_language
        evidence: list[dict[str, Any]] = []
        evidence_index: dict[tuple[str, str, str], int] = {}

        def citation(item: dict[str, Any]) -> int:
            key = (
                str(item.get("slide_id") or ""),
                str(item.get("chunk_id") or item.get("element_id") or ""),
                str(item.get("quote") or ""),
            )
            if key not in evidence_index:
                evidence.append(item)
                evidence_index[key] = len(evidence)
            return evidence_index[key]

        if not self.signals:
            return self._render_empty(plan, citation, evidence)

        sections = [self._render_intro(plan)]
        for facet in FACET_ORDER:
            selected = [signal for signal in self.signals if signal.facet == facet]
            if not selected:
                continue
            label = FACET_LABELS[facet][0 if language == "zh" else 1]
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in selected]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))

        if self.caveats:
            label = "需要平衡看待" if language == "zh" else "Important qualifications"
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in self.caveats]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))

        synthetic = any(signal.synthetic for signal in self.signals) or any(signal.synthetic for signal in self.caveats)
        warnings = ["SYNTHETIC_DATA_SIGNAL"] if synthetic else []
        return "\n\n".join(sections), evidence, warnings

    def _render_empty(
        self,
        plan: QueryPlan,
        citation: Any,
        evidence: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        language = plan.answer_language
        if self.caveats and plan.evaluation_polarity == "balanced":
            intro = (
                "文档中没有找到具备比较依据的优势证据，但发现以下需要关注的事项："
                if language == "zh"
                else "No strength was supported by comparative evidence, but the following concerns were identified:"
            )
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in self.caveats]
            warnings = ["SYNTHETIC_DATA_SIGNAL"] if any(signal.synthetic for signal in self.caveats) else []
            return intro + "\n\n" + "\n".join(lines), evidence, warnings
        if self.analyzed_sources == 0:
            answer = (
                "当前文档范围内没有可用于评价该问题的业务内容。"
                if language == "zh"
                else "The current document scope contains no business content from which this question can be evaluated."
            )
            return answer, [], ["INSUFFICIENT_EVIDENCE"]
        if plan.evaluation_polarity == "opportunity":
            answer = (
                "文档中存在业务内容，但没有找到由管理动作、未达目标或明确增长抓手支持的机会证据。因此不能仅凭一般业务描述推断下一阶段机会。"
                if language == "zh"
                else "The document contains business content, but no opportunity is supported by a management action, "
                "target gap, or explicit growth lever. General business descriptions do not justify an opportunity claim."
            )
            return answer, [], ["NO_COMPARATIVE_OPPORTUNITY_EVIDENCE"]
        answer = (
            "文档中存在业务内容，但没有找到具备趋势、目标、阈值或相对比较依据的优势证据。现有内容可以视为业务描述，不能据此断言为公司优势。"
            if language == "zh"
            else "The document contains business content, but no strength is supported by a trend, target, threshold, "
            "or relative comparison. The available material is descriptive and does not justify a company-strength claim."
        )
        return answer, [], ["NO_COMPARATIVE_STRENGTH_EVIDENCE"]

    @staticmethod
    def _render_intro(plan: QueryPlan) -> str:
        language = plan.answer_language
        if plan.evaluation_polarity == "opportunity":
            return (
                "根据文档中的管理动作，当前可执行的改进或增长机会主要包括："
                if language == "zh"
                else "Based on management actions in the presentation, the main actionable improvement or growth opportunities are:"
            )
        if plan.evaluation_polarity == "balanced":
            return (
                "根据文档中具备比较依据的指标，公司的优势及需要平衡看待的方面如下："
                if language == "zh"
                else "Based on indicators with a documented comparison basis, the company's strengths and important qualifications are:"
            )
        return (
            "根据文档中具备比较依据的指标，公司的优势主要体现在以下方面："
            if language == "zh"
            else "Based on indicators with a documented comparison basis, the company's main strengths are:"
        )
