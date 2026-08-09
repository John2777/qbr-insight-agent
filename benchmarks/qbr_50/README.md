# QBR-50 Benchmark

`qbr-50` 是面向当前 QBR Insight Agent 的 50 题、三文档端到端评估集。它保留 QBR-30 的全部案例，并新增 20 题，重点扩大以下能力覆盖：

- 双轴图中跨系列、同月份读取与相对变化计算；
- 图表全序列极值、跨年度系列比较；
- 表格条件过滤、跨行聚合、极值和多字段联合查询；
- 下一季度事项与 Owner 的同页关系恢复；
- 不存在的市场指标精确值拒答。

每个 case 都包含人工核验的标准答案、允许表达别名、数值目标、必需证据页和允许引用范围。评分器不调用另一个大模型，避免 judge 共偏差。

运行：

```bash
.venv/bin/python scripts/evaluate_qbr_benchmark.py --retrieval-strategy hybrid --embedding-provider hashing
```

默认输出：

- `benchmark_results/qbr_50_latest.json`：逐题回答、引用、检索结果与分维度得分；
- `benchmark_results/qbr_50_latest.md`：人工可读汇总报告。

总分按适用维度归一化加权：内容覆盖 35%、数值准确 30%、引用召回 12%、引用精度 8%、检索命中 10%、证据约束 5%。拒答题要求明确说明证据不足且不得产生伪引用。
