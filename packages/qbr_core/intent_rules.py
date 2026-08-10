from __future__ import annotations

INTENTS = {
    "term_definition",
    "risk_explanation",
    "business_evaluation",
    "negative_signal_summary",
    "summary",
    "provenance",
    "chart_analysis",
    "table_analysis",
    "evidence_answer",
}

EXECUTION_PROFILES = {"fast", "focused", "analytical", "deep"}

EVALUATION_POLARITIES = {"neutral", "positive", "negative", "balanced", "opportunity"}

INTENT_OPERATIONS = {
    "term_definition": "define_term",
    "risk_explanation": "explain_risk",
    "business_evaluation": "evaluate_business",
    "negative_signal_summary": "assess_downside",
    "summary": "synthesize_summary",
    "provenance": "verify_provenance",
    "chart_analysis": "analyze_chart",
    "table_analysis": "analyze_table",
    "evidence_answer": "answer_from_evidence",
}

PROFILE_RANK = {"fast": 0, "focused": 1, "analytical": 2, "deep": 3}

SUMMARY_MARKERS = (
    "总结",
    "概括",
    "概览",
    "整体表现",
    "总体表现",
    "核心结论",
    "主要结论",
    "业绩情况",
    "经营情况",
    "表现如何",
    "情况如何",
    "业务表现",
    "key takeaways",
    "executive summary",
    "summarize",
    "summary",
    "overview",
    "overall performance",
    "business performance",
    "how is the business doing",
)

NEGATIVE_DIRECT_MARKERS = (
    "bad news",
    "what are the risks",
    "what risks",
    "main risks",
    "key risks",
    "downside",
    "negative information",
    "negative signals",
    "what is wrong",
    "what went wrong",
    "weakness",
    "weaknesses",
    "what should management worry",
    "what should we worry",
    "potential issue",
    "potential issues",
    "problem area",
    "problem areas",
    "weak spot",
    "weak spots",
    "areas of concern",
    "warning signs",
    "red flags",
    "what could go wrong",
    "坏消息",
    "负面信息",
    "不利信息",
    "负面信号",
    "主要风险",
    "有哪些风险",
    "有什么风险",
    "哪里承压",
    "哪些指标承压",
    "有什么问题",
    "劣势",
    "不足",
    "不足之处",
    "短板",
    "潜在问题",
    "潜在风险",
    "经营问题",
    "业务问题",
    "主要问题",
    "关键问题",
    "问题点",
    "风险点",
    "隐患",
    "薄弱点",
    "薄弱环节",
    "需要警惕",
    "值得警惕",
    "值得担忧",
)

NEGATIVE_TOPIC_MARKERS = (
    "risk",
    "risks",
    "concern",
    "concerns",
    "challenge",
    "challenges",
    "pressure",
    "underperform",
    "deteriorat",
    "风险",
    "担忧",
    "挑战",
    "承压",
    "下滑",
    "恶化",
    "未达标",
)

NEGATIVE_DISCOVERY_PATTERNS = (
    r"潜在(?:的)?(?:经营|业务|公司)?(?:问题|风险|隐患)",
    r"(?:经营|业务|公司)(?:上|方面)?(?:可能)?(?:存在|面临)?(?:哪些|什么)?(?:问题|隐患|风险点)",
    r"(?:哪些|什么).{0,12}(?:需要|值得)(?:持续)?(?:警惕|担忧|关注)",
    r"(?:薄弱|脆弱)(?:的)?(?:点|环节|方面)",
    r"\b(?:potential issues?|problem areas?|weak spots?|areas? of concern|warning signs?|red flags?)\b",
)

RISK_EXPLANATION_MARKERS = (
    "是指什么",
    "指的是什么",
    "具体指什么",
    "具体是指",
    "什么意思",
    "什么含义",
    "如何理解",
    "怎么理解",
    "解释一下",
    "what does",
    "what is meant by",
    "what is the meaning of",
    "explain",
)

POSITIVE_EVALUATION_MARKERS = (
    "advantage",
    "advantages",
    "best performing",
    "competitive edge",
    "core strength",
    "core strengths",
    "outperform",
    "strength",
    "strengths",
    "what went well",
    "优势",
    "优点",
    "亮点",
    "强项",
    "竞争力",
    "领先点",
    "做得好",
    "做得比较好",
    "表现最好",
)

BALANCED_EVALUATION_MARKERS = (
    "pros and cons",
    "strengths and weaknesses",
    "优劣势",
    "优势和劣势",
    "优势与不足",
    "好坏在哪里",
)

OPPORTUNITY_EVALUATION_MARKERS = (
    "growth lever",
    "growth opportunity",
    "growth opportunities",
    "upside opportunity",
    "增长机会",
    "增长点",
    "突破点",
    "潜在机会",
)

BROAD_QUESTION_MARKERS = (
    "main",
    "key",
    "overall",
    "ppt",
    "deck",
    "presentation",
    "有哪些",
    "有什么",
    "哪些",
    "主要",
    "整体",
    "这份",
)

PROVENANCE_MARKERS = (
    "公开披露",
    "模拟数据",
    "测试数据",
    "数据来源",
    "source",
    "sources",
    "synthetic",
    "illustrative",
    "official disclosure",
)

DOMAIN_EQUIVALENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("revenue", "sales", "营收", "收入"), "revenue sales 营收 收入"),
    (("profit", "earnings", "利润", "盈利"), "profit earnings 利润 盈利"),
    (("margin", "利润率", "价值率"), "margin rate 利润率 价值率"),
    (("growth", "增长", "增速"), "growth increase 增长 增速"),
    (("risk", "风险"), "risk exposure warning 风险 暴露 预警"),
    (("capital", "资本"), "capital solvency buffer 资本 偿付能力 缓冲"),
    (("customer", "客户"), "customer retention persistency 客户 留存 继续率"),
    (("channel", "渠道"), "channel distribution productivity 渠道 分销 产能"),
    (("target", "budget", "目标", "预算"), "target budget attainment variance 目标 预算 达成 偏差"),
)
