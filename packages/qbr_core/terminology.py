from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TermDefinition:
    term: str
    full_name: str
    chinese_name: str
    definition: str
    aliases: tuple[str, ...] = ()
    distinction: str = ""

    @property
    def search_aliases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.term, self.full_name, self.chinese_name, *self.aliases)))

    def answer(self) -> str:
        names = "，".join(part for part in (self.full_name, self.chinese_name) if part)
        heading = f"{self.term}（{names}）" if names else self.term
        text = f"**{heading}**：{self.definition}"
        if self.distinction:
            text += f"\n\n简单区分：{self.distinction}"
        return text


# This is approved product knowledge, not model-generated company information.
# Definitions intentionally stay at management-QBR level; company-specific formulas
# and accounting policies must still come from the uploaded document.
QBR_TERMS: tuple[TermDefinition, ...] = (
    TermDefinition(
        "QBR",
        "Quarterly Business Review",
        "季度业务回顾",
        "按季度复盘业务结果、关键差异、风险和下一步行动的管理机制。",
        distinction="它不是单纯汇报数据，还应解释偏差、明确责任人和后续动作。",
    ),
    TermDefinition(
        "KPI",
        "Key Performance Indicator",
        "关键绩效指标",
        "用于衡量业务目标达成情况的核心量化指标。",
        distinction="KPI 衡量结果或过程表现；目标本身通常由战略、预算或 OKR 给出。",
    ),
    TermDefinition(
        "OKR",
        "Objectives and Key Results",
        "目标与关键结果",
        "用定性目标和可衡量关键结果连接战略方向与执行结果的方法。",
        distinction="Objective 说明要实现什么，Key Results 说明如何判断是否实现。",
    ),
    TermDefinition(
        "YoY", "Year over Year", "同比", "将当前期间与上年同一期间比较，用于减少季节性造成的误判。", aliases=("year-on-year", "同比增长")
    ),
    TermDefinition(
        "QoQ",
        "Quarter over Quarter",
        "季度环比",
        "将当前季度与紧邻的上一季度比较，用于观察短期变化。",
        aliases=("quarter-on-quarter", "环比"),
        distinction="同比对比上年同期；QoQ 对比上一季度。",
    ),
    TermDefinition("MoM", "Month over Month", "月度环比", "将当前月份与紧邻的上一个月份比较。", aliases=("month-on-month", "月环比")),
    TermDefinition(
        "YTD", "Year to Date", "年初至今", "从本财年或自然年年初累计到当前报告日的结果。", aliases=("year-to-date", "年初至今累计")
    ),
    TermDefinition("MTD", "Month to Date", "月初至今", "从本月月初累计到当前报告日的结果。", aliases=("month-to-date",)),
    TermDefinition(
        "FY", "Fiscal Year", "财年", "企业用于财务报告和预算管理的年度期间，不一定与自然年一致。", aliases=("fiscal year", "财政年度")
    ),
    TermDefinition("CY", "Calendar Year", "自然年", "从 1 月 1 日到 12 月 31 日的年度期间。", aliases=("calendar year",)),
    TermDefinition(
        "Actual",
        "Actual Result",
        "实际值",
        "报告期内已经发生并记录的业务或财务结果。",
        aliases=("actuals", "实际"),
        distinction="Actual 是已实现结果；Budget 和 Forecast 分别是预算目标与最新预测。",
    ),
    TermDefinition("Budget", "Budget", "预算", "在计划周期开始前批准的资源配置和业绩目标基线。", aliases=("预算值",)),
    TermDefinition(
        "Forecast",
        "Forecast",
        "预测",
        "基于最新实际结果和业务假设，对未来结果作出的滚动估计。",
        aliases=("latest estimate", "预测值"),
        distinction="Forecast 会随新信息更新，Budget 通常是固定的年度批准基线。",
    ),
    TermDefinition(
        "Variance", "Variance", "差异", "实际值、预算值或预测值之间的差额，通常同时分析金额差异和百分比差异。", aliases=("偏差", "差异分析")
    ),
    TermDefinition(
        "Run Rate",
        "Run Rate",
        "运行年化水平",
        "把当前较短期间的业务速度外推为更长期间的参考水平。",
        aliases=("年化运行率", "年化水平"),
        distinction="Run Rate 是简化外推，不等同于经过完整假设建模的 Forecast。",
    ),
    TermDefinition(
        "CAGR", "Compound Annual Growth Rate", "复合年增长率", "表示起点到终点之间按复利计算的年均增长速度。", aliases=("复合增长率",)
    ),
    TermDefinition(
        "Revenue",
        "Revenue",
        "营业收入",
        "企业通过销售商品或提供服务取得的收入，在扣除各类成本费用之前体现业务规模。",
        aliases=("营收", "收入"),
    ),
    TermDefinition(
        "Gross Margin", "Gross Margin", "毛利率", "毛利占营业收入的比例，用于观察产品或服务在直接成本之后的盈利空间。", aliases=("毛利率",)
    ),
    TermDefinition(
        "Operating Margin",
        "Operating Margin",
        "营业利润率",
        "营业利润占营业收入的比例，用于衡量核心经营活动在营业费用后的盈利能力。",
        aliases=("营运利润率", "营业利润率"),
    ),
    TermDefinition(
        "EBITDA",
        "Earnings Before Interest, Taxes, Depreciation and Amortization",
        "息税折旧摊销前利润",
        "在利息、所得税、折旧和摊销之前的利润指标，常用于比较经营获利能力。",
        distinction="它不是现金流，也不等同于净利润。",
    ),
    TermDefinition(
        "EBIT",
        "Earnings Before Interest and Taxes",
        "息税前利润",
        "在利息和所得税之前、但通常已经计入折旧与摊销后的利润指标。",
        distinction="EBITDA 在 EBIT 基础上再加回折旧和摊销。",
    ),
    TermDefinition(
        "OPEX",
        "Operating Expenditure",
        "运营费用",
        "维持日常经营发生的费用，例如人员、租赁、营销和信息技术费用。",
        aliases=("operating expense", "运营支出"),
    ),
    TermDefinition(
        "CAPEX",
        "Capital Expenditure",
        "资本性支出",
        "用于取得或改善长期资产的投入，通常在多个期间内折旧或摊销。",
        aliases=("capital expenditure", "资本开支"),
    ),
    TermDefinition(
        "Free Cash Flow",
        "Free Cash Flow",
        "自由现金流",
        "企业在维持经营和必要资本投入后可供分配、偿债或再投资的现金。",
        aliases=("FCF", "自由现金流"),
    ),
    TermDefinition(
        "Working Capital",
        "Working Capital",
        "营运资金",
        "通常指流动资产减流动负债，用于观察日常经营的短期资金占用和偿付能力。",
        aliases=("营运资本", "营运资金"),
    ),
    TermDefinition(
        "ROE",
        "Return on Equity",
        "净资产收益率",
        "利润相对于股东权益的回报率，用于衡量股东资本的使用效率。",
        aliases=("return on equity", "净资产回报率"),
    ),
    TermDefinition(
        "Operating ROEV",
        "Operating Return on Embedded Value",
        "营运内含价值回报率",
        "保险公司衡量经营活动相对于内含价值所创造回报的指标。",
        aliases=("ROEV", "营运ROEV", "operating return on ev"),
        distinction="具体分子、分母和期初期末口径应以公司的披露方法为准。",
    ),
    TermDefinition(
        "EV Equity",
        "Embedded Value Equity",
        "内含价值权益",
        "寿险业务现有净资产与有效保单未来可分配利润现值的综合价值指标。",
        aliases=("embedded value", "内含价值"),
        distinction="它是精算价值口径，不等同于会计报表中的股东权益或市值。",
    ),
    TermDefinition(
        "ANP",
        "Annualised New Premium",
        "年化新保费",
        "将报告期内新业务保费按年化口径折算的规模指标。",
        aliases=("annualized new premium", "年化新业务保费"),
        distinction="ANP 反映新业务规模；VONB 反映新业务预计创造的价值。",
    ),
    TermDefinition(
        "APE",
        "Annual Premium Equivalent",
        "年化保费等值",
        "常将期缴新单年化保费与一定比例的一次性保费合并，用于比较新业务销售规模。",
        aliases=("annual premium equivalent", "年化保费"),
        distinction="具体一次性保费折算比例应以公司的披露口径为准。",
    ),
    TermDefinition(
        "VONB",
        "Value of New Business",
        "新业务价值",
        "衡量某一期间新签业务预计能够为股东创造的未来价值，是寿险新业务价值创造的核心指标。",
        aliases=("new business value", "新业务价值"),
        distinction="它不是保费收入，也不是当期会计利润；具体精算假设和计算口径以公司披露为准。",
    ),
    TermDefinition(
        "VONB Margin",
        "Value of New Business Margin",
        "新业务价值率",
        "新业务价值相对于相应新业务保费规模的比率，用于观察新业务的价值转化效率。",
        aliases=("新业务价值利润率", "新业务价值率"),
        distinction="不同公司可能采用 ANP 或 APE 等分母，应以披露口径为准。",
    ),
    TermDefinition(
        "OPAT",
        "Operating Profit After Tax",
        "税后营运利润",
        "扣除所得税后的营运利润，用于观察核心经营活动产生的盈利。",
        aliases=("税后营运溢利", "operating profit after tax"),
        distinction="它通常会剔除部分非营运或一次性项目，具体调整项以公司定义为准。",
    ),
    TermDefinition(
        "UFSG",
        "Underlying Free Surplus Generation",
        "基础自由盈余产生",
        "保险业务在基础经营口径下产生自由盈余的能力指标。",
        aliases=("underlying free surplus generation", "基础自由盈余"),
    ),
    TermDefinition(
        "Net FSG",
        "Net Free Surplus Generation",
        "净自由盈余产生",
        "在基础自由盈余产生的基础上考虑资本投入、再保险或其他相关项目后的净自由盈余变化。",
        aliases=("net free surplus generation", "净自由盈余"),
        distinction="具体调整项目应以公司披露口径为准。",
    ),
    TermDefinition(
        "CER",
        "Constant Exchange Rates",
        "固定汇率口径",
        "使用固定汇率换算不同期间数据，以减少汇率波动对增长比较的影响。",
        aliases=("constant exchange rate", "固定汇率"),
        distinction="CER 展示经营层面的可比变化，不代表实际报表汇率下的金额。",
    ),
    TermDefinition(
        "13M Persistency",
        "13-Month Persistency",
        "13个月继续率",
        "衡量保单生效约 13 个月后仍保持有效或保费继续缴纳的比例，用于观察业务质量和客户留存。",
        aliases=("13M继续率", "persistency 13m", "十三个月继续率"),
    ),
    TermDefinition(
        "Digital STP",
        "Digital Straight-Through Processing",
        "数字直通率",
        "业务从提交到承保或处理完成过程中无需人工干预、由数字化流程直通完成的比例。",
        aliases=("STP", "straight-through processing", "数字直通率"),
        distinction="具体起止环节和例外规则应以业务流程定义为准。",
    ),
    TermDefinition(
        "NPS",
        "Net Promoter Score",
        "净推荐值",
        "根据客户推荐意愿计算的体验指标，通常用推荐者占比减去贬损者占比。",
        aliases=("net promoter score", "净推荐值"),
    ),
    TermDefinition(
        "Churn",
        "Customer Churn",
        "客户流失",
        "客户在一定期间内停止续约、取消服务或不再活跃的现象及其比例。",
        aliases=("流失率", "客户流失率"),
        distinction="计算时必须明确客户口径、观察期和分母。",
    ),
    TermDefinition(
        "Retention",
        "Customer Retention",
        "客户留存",
        "客户在一定期间后仍继续使用、续约或保持有效关系的比例。",
        aliases=("留存率", "客户留存率"),
        distinction="Retention 与 Churn 相关，但不一定在所有口径下严格相加等于 100%。",
    ),
    TermDefinition(
        "CAC",
        "Customer Acquisition Cost",
        "客户获取成本",
        "为获得新增客户所投入的销售和营销成本相对于新增客户数量的指标。",
        aliases=("获客成本", "customer acquisition cost"),
    ),
    TermDefinition(
        "LTV",
        "Customer Lifetime Value",
        "客户生命周期价值",
        "一个客户在整个关系周期内预计能够贡献的经济价值。",
        aliases=("CLV", "lifetime value", "客户终身价值"),
        distinction="LTV/CAC 常用于判断获客投入是否具有经济性。",
    ),
    TermDefinition(
        "ARPU",
        "Average Revenue Per User",
        "每用户平均收入",
        "一定期间内收入除以同期平均用户数，用于观察单个用户的收入贡献。",
        aliases=("average revenue per user", "每用户收入"),
    ),
    TermDefinition(
        "ARR",
        "Annual Recurring Revenue",
        "年度经常性收入",
        "将订阅或合同中的经常性收入按年度口径表示的指标。",
        aliases=("annual recurring revenue", "年度重复性收入"),
        distinction="ARR 通常不包含一次性实施费或非经常性收入。",
    ),
    TermDefinition(
        "MRR",
        "Monthly Recurring Revenue",
        "月度经常性收入",
        "订阅或合同在一个月内产生的经常性收入。",
        aliases=("monthly recurring revenue", "月度重复性收入"),
        distinction="在稳定口径下 ARR 通常可由 MRR 年化，但仍需处理季节性和合同变化。",
    ),
    TermDefinition(
        "Sales Pipeline",
        "Sales Pipeline",
        "销售管道",
        "从线索、商机到成交的分阶段销售机会集合，用于管理未来收入和销售动作。",
        aliases=("pipeline", "销售漏斗", "销售管线"),
    ),
    TermDefinition(
        "Win Rate",
        "Win Rate",
        "赢单率",
        "进入统计范围的销售机会中最终成功成交的比例。",
        aliases=("赢率", "成交率"),
        distinction="必须明确分母是全部机会、已关闭机会还是某一阶段机会。",
    ),
    TermDefinition(
        "Conversion Rate",
        "Conversion Rate",
        "转化率",
        "对象从流程一个阶段成功进入目标阶段的比例。",
        aliases=("转化率",),
        distinction="QBR 中应同时说明起点、终点、观察期和样本口径。",
    ),
    TermDefinition(
        "Attainment",
        "Target Attainment",
        "目标达成率",
        "实际结果相对于目标值的完成比例。",
        aliases=("目标完成率", "达成率"),
        distinction="超过 100% 表示超额完成，但仍要核对目标是否中途调整。",
    ),
)


DEFINITION_MARKERS = (
    "是什么",
    "什么意思",
    "什么含义",
    "含义",
    "全称",
    "解释",
    "怎么理解",
    "如何理解",
    "指什么",
    "区别",
    "what is",
    "what does",
    "meaning",
    "define",
    "explain",
)


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _alias_match(question: str, alias: str) -> re.Match[str] | None:
    folded = _fold(question)
    candidate = _fold(alias).strip()
    if not candidate:
        return None
    if re.fullmatch(r"[a-z0-9][a-z0-9 .&/-]*", candidate):
        return re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", folded)
    return re.search(re.escape(candidate), folded)


def find_term(question: str) -> TermDefinition | None:
    folded = _fold(question).strip()
    if not any(marker in folded for marker in DEFINITION_MARKERS):
        return None
    matches: list[tuple[int, TermDefinition]] = []
    for definition in QBR_TERMS:
        alias_scores: list[int] = []
        for alias in definition.search_aliases:
            match = _alias_match(question, alias)
            if match is None:
                continue
            score = len(alias)
            if match.start() <= 2:
                score += 50
            preceding = folded[: match.start()]
            if any(re.search(rf"{re.escape(marker)}[一下这个该\s]*$", preceding) for marker in DEFINITION_MARKERS):
                score += 30
            following = folded[match.end() :]
            if any(re.match(rf"^[的在是指为叫做\s]*{re.escape(marker)}", following) for marker in DEFINITION_MARKERS):
                score += 30
            if definition.term == "QBR" and re.match(r"^[里中内的\s]", following):
                score -= 60
            alias_scores.append(score)
        if alias_scores:
            matches.append((max(alias_scores), definition))
    if not matches:
        return None
    score, definition = max(matches, key=lambda item: item[0])
    return definition if score >= 30 else None


def glossary_by_term() -> dict[str, TermDefinition]:
    return {definition.term.casefold(): definition for definition in QBR_TERMS}
