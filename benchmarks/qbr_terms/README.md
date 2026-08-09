# QBR Terms Benchmark

`qbr-terms@1.0.0` 是独立于具体演示数值的专业术语解释回归集，包含 50 个案例，覆盖：

- QBR 管理与期间口径；
- 财务与经营效率指标；
- 寿险价值、资本和业务质量指标；
- 客户、订阅和销售漏斗指标。

运行完整端到端回归：

```bash
.venv/bin/python scripts/evaluate_qbr_benchmark.py \
  --cases benchmarks/qbr_terms/cases.json \
  --output-json benchmark_results/qbr_terms_latest.json \
  --output-md benchmark_results/qbr_terms_latest.md \
  --min-score 95
```

该数据集重点检查：术语全称与中文含义、核心解释覆盖、`term_definition` 回答模式、无关内容抑制和引用数量。术语定义来自代码内受控词库；具体公司计算公式和披露口径仍应以上传文档为准。
