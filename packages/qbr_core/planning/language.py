from __future__ import annotations

# Language-level vocabulary bridges used to improve retrieval recall. They do
# not classify the user's request or select an answer template.
DOMAIN_EQUIVALENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("revenue", "sales", "营收", "收入"), "revenue sales 营收 收入"),
    (("profit", "earnings", "利润", "盈利"), "profit earnings 利润 盈利"),
    (("margin", "利润率", "价值率"), "margin rate 利润率 价值率"),
    (("growth", "增长", "增速"), "growth increase 增长 增速"),
    (
        ("risk", "风险"),
        "risk exposure warning threshold limit current trend 风险 暴露 预警 阈值 限额 当前 趋势",
    ),
    (("capital", "资本"), "capital solvency buffer 资本 偿付能力 缓冲"),
    (("customer", "客户"), "customer retention persistency 客户 留存 继续率"),
    (("channel", "渠道"), "channel distribution productivity 渠道 分销 产能"),
    (("target", "budget", "目标", "预算"), "target budget attainment variance 目标 预算 达成 偏差"),
)

# Broad paraphrase bridges for the model-free degraded path. Matching one only
# adds retrieval vocabulary; it never selects an answer module or response form.
LANGUAGE_BRIDGES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("总结", "概括", "概览", "summarize", "summary", "overview", "key takeaways"),
        "executive summary key facts metrics drivers risks actions 执行摘要 核心事实 指标 驱动 风险 行动",
    ),
    (
        (
            "表现如何",
            "情况如何",
            "经营情况",
            "经营状况",
            "经营表现",
            "业务情况",
            "业绩",
            "performance",
            "how is",
            "how did",
            "how is the business",
            "how is the company doing",
        ),
        "performance results revenue margin growth profitability cash capital operations 业绩 营收 利润率 增长 盈利 现金 资本 运营",
    ),
    (
        ("bad news", "potential issue", "weak spot", "warning sign", "潜在问题", "隐患", "短板", "警惕", "主要风险"),
        "risk concern decline pressure below target threshold concentration 风险 担忧 下滑 承压 未达标 阈值 集中度",
    ),
    (
        ("优势", "亮点", "强项", "strength", "stand out", "competitive edge"),
        "strength growth outperformance above target resilience 优势 增长 领先 超目标 韧性",
    ),
    (
        ("来源", "公开披露", "模拟数据", "source", "official", "synthetic"),
        "source provenance official disclosure synthetic boundary 数据来源 公开披露 模拟数据 口径边界",
    ),
    (
        ("什么意思", "什么含义", "是指什么", "what does", "meaning", "define"),
        "definition meaning terminology 定义 含义 术语",
    ),
    (
        ("趋势", "变化", "比较", "对比", "trend", "change", "compare"),
        "trend change comparison period start latest 趋势 变化 比较 起始期 最新期",
    ),
    (
        ("集中", "集中度", "concentration", "concentrated", "diversification"),
        "concentration diversification mix share top current threshold 集中度 多元化 组合 占比 当前 阈值",
    ),
)


__all__ = ["DOMAIN_EQUIVALENTS", "LANGUAGE_BRIDGES"]
