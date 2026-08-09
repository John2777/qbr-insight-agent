# QBR Query Pipeline Benchmark

`qbr-query-pipeline@1.0.0` 用于回归新的端到端问答架构，重点不是核对某一份 PPT 的固定数值，而是验证进入召回前的决策是否稳定：

- 中英文问题能否识别为术语解释、正向/平衡/机会评价、风险综述、文档总结、来源边界、图表分析、表格分析或事实问答；
- 原问题是否始终保留为第一条检索通道；
- 查询扩写是否覆盖业务同义词和中英文交叉表达；
- 年份、季度和百分比等硬约束是否原样保留；
- 普通业务问答是否排除来源注释、生成说明和测试方法内容。

运行回归：

```bash
.venv/bin/pytest -q tests/unit/test_query_pipeline.py
```

数据集字段 `required_query_terms` 表示扩写后的查询集合必须覆盖的词组；`expected_evaluation_polarity` 用于评价型问题的正向、负向、平衡或机会极性；`excluded_roles` 表示召回阶段必须过滤的内容角色。新增坏案例时，先在 `cases.json` 中固化预期，再补充最小的端到端证据测试。
