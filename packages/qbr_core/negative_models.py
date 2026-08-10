from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .query_planning import QueryPlan

SignalCategory = Literal["explicit", "deterioration", "threshold", "watchpoint"]


def _risk_concept(question: str, language: str) -> str:
    if language == "zh":
        matches = re.findall(r"[A-Za-z0-9·\u4e00-\u9fff]{1,18}风险", question)
        if matches:
            concept = matches[-1]
            for prefix in ("所谓的", "这里的", "文档中的", "什么是", "请解释", "解释一下"):
                concept = concept.removeprefix(prefix)
            return concept or "该风险概念"
        return "该风险概念"
    folded = question.casefold()
    matches = re.findall(r"(?:[a-z][a-z-]*\s+){0,3}risk\b", folded)
    if matches:
        concept = matches[-1].strip()
        for prefix in ("what is the ", "what is ", "the ", "explain "):
            concept = concept.removeprefix(prefix)
        return concept or "this risk concept"
    return "this risk concept"


@dataclass(frozen=True, slots=True)
class NegativeSignal:
    category: SignalCategory
    claim_zh: str
    claim_en: str
    evidence: dict[str, Any]
    score: float
    synthetic: bool = False

    def claim(self, language: str) -> str:
        return self.claim_zh if language == "zh" else self.claim_en


@dataclass(frozen=True, slots=True)
class NegativeAssessment:
    signals: tuple[NegativeSignal, ...]
    status_evidence: tuple[dict[str, Any], ...]
    explicit_red_flags: bool
    analyzed_sources: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def render_explanation(self, plan: QueryPlan) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Explain a risk concept from evidence without falling back to a raw evidence dump."""
        language = plan.answer_language
        concept = _risk_concept(plan.original_question, language)
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

        selected_signals = list(self.signals[:5])
        selected_status = list(self.status_evidence[:2])
        if not selected_signals and not selected_status:
            answer = (
                "当前文档没有提供足以界定这一风险概念的业务指标、风险闸门或管理行动。"
                if language == "zh"
                else (
                    "The document does not provide business indicators, risk gates, or management actions "
                    "sufficient to define this risk concept."
                )
            )
            return answer, [], ["INSUFFICIENT_EVIDENCE"]

        basis = [signal.evidence for signal in selected_signals[:2]] or selected_status[:2]
        basis_refs = "".join(f"[{citation(item)}]" for item in basis)
        if language == "zh":
            definition = (
                "既定经营目标和优先事项在落地过程中偏离计划，或触发管理阈值的可能性"
                if any(
                    marker in concept.casefold() for marker in ("执行", "落地", "交付", "实施", "execution", "delivery", "implementation")
                )
                else "相关业务因素偏离预期、触发管理阈值并影响目标达成的可能性"
            )
            sections = [
                "## 直接解释\n\n"
                f"文档没有给出“{concept}”的正式词典定义。结合其中的风险闸门、关键指标和管理行动，"
                f"这里可将它理解为：**{definition}**。"
                f"这是基于文档证据的归纳，而不是原文定义。{basis_refs}",
            ]
            if selected_signals:
                labels = {
                    "explicit": "已经出现的明确问题",
                    "deterioration": "指标恶化或交付偏差",
                    "threshold": "风险阈值被触发",
                    "watchpoint": "需要落实的管理动作",
                }
                lines = [
                    f"- **{labels[signal.category]}**：{signal.claim('zh')} [{citation(signal.evidence)}]" for signal in selected_signals
                ]
                sections.append("## 在这份 PPT 中的具体表现\n\n" + "\n".join(lines))
            if selected_status:
                refs = "".join(f"[{citation(item)}]" for item in selected_status)
                sections.append(
                    "## 如何判断是否升级为实际问题\n\n"
                    "文档中的绿/黄/红阈值和当前值是量化判断工具：进入黄区或红区、持续恶化，"
                    f"才支持把潜在执行风险升级为明确问题；处于绿色也不等于未来没有风险。{refs}"
                )
            sections.append(
                "## 结论边界\n\n"
                f"“{concept}”描述的是目标落地的不确定性，不应与已经发生的经营损失画等号。"
                "回答只把文档中可验证的指标变化、阈值和管理动作列为具体落点。"
            )
        else:
            definition = (
                "the possibility that agreed business goals or priorities deviate from plan during delivery, "
                "or trigger a management threshold"
                if any(marker in concept for marker in ("execution", "delivery", "implementation"))
                else (
                    "the possibility that the relevant business condition deviates from expectation, "
                    "triggers a management threshold, and affects delivery of the plan"
                )
            )
            sections = [
                "## Direct explanation\n\n"
                f"The document does not provide a formal glossary definition of {concept}. "
                "From its risk gates, KPIs, and management actions, "
                f"the term can be understood as **{definition}**. This is an evidence-based interpretation, "
                f"not a quoted definition. {basis_refs}",
            ]
            if selected_signals:
                labels = {
                    "explicit": "Explicit issue",
                    "deterioration": "Metric deterioration or delivery variance",
                    "threshold": "Triggered risk threshold",
                    "watchpoint": "Management action requiring delivery",
                }
                lines = [
                    f"- **{labels[signal.category]}**: {signal.claim('en')} [{citation(signal.evidence)}]" for signal in selected_signals
                ]
                sections.append("## What it means in this presentation\n\n" + "\n".join(lines))
            if selected_status:
                refs = "".join(f"[{citation(item)}]" for item in selected_status)
                sections.append(
                    "## How it becomes an actual issue\n\n"
                    "The current values and green/amber/red gates are the quantitative test: "
                    "an amber/red result or sustained deterioration "
                    f"supports escalation, while a green status does not prove that future execution risk is absent. {refs}"
                )
            sections.append(
                "## Boundary\n\n"
                f"{concept.capitalize()} is uncertainty around delivery; it is not the same as a loss "
                "that has already occurred. Only verifiable metric changes, thresholds, and management "
                "actions from the document are treated as concrete manifestations."
            )
        warnings = ["SYNTHETIC_DATA_SIGNAL"] if any(signal.synthetic for signal in selected_signals) else []
        return "\n\n".join(sections), evidence, warnings

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
            if not self.status_evidence and self.analyzed_sources == 0:
                answer = (
                    "当前文档范围内没有足够的相关业务证据回答这个问题。"
                    if language == "zh"
                    else "The current document does not contain enough relevant business evidence to answer this question."
                )
                return answer, [], ["INSUFFICIENT_EVIDENCE"]
            if not self.status_evidence:
                answer = (
                    "已分析的业务内容中没有发现明确的负面陈述、实质性恶化趋势或阈值突破。"
                    "这不等于证明没有下行风险；只能说明当前文档没有提供足以支持具体“坏消息”结论的证据。"
                    if language == "zh"
                    else "No explicit negative statement, material deterioration, or threshold breach was found in the "
                    "business content analyzed. This does not prove that no downside exists; it means the presentation "
                    "does not support a specific bad-news claim."
                )
                return answer, [], []
            references = [citation(item) for item in self.status_evidence[:2]]
            suffix = "" if not references else " " + "".join(f"[{index}]" for index in references)
            answer = (
                "文档中没有发现明确的负面结果或突破阈值的红灯事项；现有指标主要处于绿色/达标状态。因此不应为了回答“坏消息”而制造负面结论。"
                if language == "zh"
                else "No explicit negative result or breached threshold was found in the presentation. "
                "The available indicators are predominantly green or on target, so it would be misleading to manufacture bad news."
            )
            return answer + suffix, evidence, []

        if self.explicit_red_flags:
            intro = "文档中最明确的负面信号是：" if language == "zh" else "The clearest negative signals in the presentation are:"
        else:
            intro = (
                "文档中没有发现突破阈值的红灯事项，但存在以下基于数据和管理动作的关注点："
                if language == "zh"
                else "No red/amber threshold breach was found, but the evidence supports the following watchpoints:"
            )
        labels = {
            "explicit": ("明确负面信息", "Explicit negative evidence"),
            "deterioration": ("恶化趋势", "Deteriorating metrics"),
            "threshold": ("阈值事项", "Threshold issues"),
            "watchpoint": ("管理关注点", "Management watchpoints"),
        }
        sections: list[str] = [intro]
        for category in ("explicit", "deterioration", "threshold", "watchpoint"):
            selected = [signal for signal in self.signals if signal.category == category]
            if not selected:
                continue
            label = labels[category][0 if language == "zh" else 1]
            lines = [f"- {signal.claim(language)} [{citation(signal.evidence)}]" for signal in selected]
            sections.append(f"**{label}**\n\n" + "\n".join(lines))
        warnings = ["SYNTHETIC_DATA_SIGNAL"] if any(signal.synthetic for signal in self.signals) else []
        return "\n\n".join(sections), evidence, warnings
