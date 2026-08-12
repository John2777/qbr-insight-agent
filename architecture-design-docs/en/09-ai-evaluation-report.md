# 09. AI Evaluation and Quality Loop

## 1. Evaluation method

Answer quality is decomposed into five diagnosable layers: parsing, retrieval, evidence selection, deterministic reasoning, and generation with verification. When a case fails, the team can decide whether to improve the parser, retriever, calculator, or prompt instead of attributing everything to the model.

```mermaid
flowchart LR
    Dataset["Versioned Dataset"] --> Parse["Parse Quality"]
    Parse --> Retrieve["Retrieval Hit / Recall"]
    Retrieve --> Reason["Numeric / Table / Chart Reasoning"]
    Reason --> Answer["Content Coverage / Readability"]
    Answer --> Verify["Citation / Groundedness / Safety"]
    Verify --> Artifact["Case-level Artifact"]
    Artifact --> Regression["Regression Gate"]
```

Critical numbers, citations, and abstention behavior use deterministic evaluators. Model-based judging is reserved for expression quality that cannot be reduced to stable rules. Every run stores the dataset version, question, answer, citations, dimension scores, verification disposition, latency, and token usage.

## 2. Datasets

| Dataset | Size | Capability focus |
|---|---:|---|
| QBR-50 | 50 questions, 3 synthetic documents | lookup, calculation, tables, charts, synthesis, citation, abstention |
| QBR Terms | 50 terms | QBR, finance, life-insurance value, customer and sales metrics |
| Chart Analysis | 12 dense chart tasks | multi-series, dual-axis, trend divergence, scenario analysis |
| Query Pipeline | planning and retrieval regression set | raw-question preservation, expansion, hard constraints, content roles |
| Real-model Matrix | 4 presentations, 81 questions | end-to-end behavior with a real provider |

QBR-50 gold answers include accepted phrasings, target numbers, required evidence slides, and citation ranges. Scoring does not invoke another large model, avoiding correlated answer/judge bias.

## 3. Representative validation results

### 3.1 Deterministic QBR regression milestone

On the fixed dataset checkpoint dated 2026-08-08, QBR-50 improved from 94.01 to 100.00, with all 50 cases reaching full score. Content coverage, numeric accuracy, retrieval hit rate, citation recall, and citation precision all reached 100. The result demonstrates reproducibility of deterministic calculations and evidence binding on a fixed business corpus.

At the same checkpoint, QBR Terms reached 50 / 50, showing that abbreviations and domain terminology can use an independent, low-noise knowledge path instead of competing with operating metrics.

### 3.2 Dense chart analysis

The Chart Analysis run on 2026-08-13 scored 90.52 overall, with Pass@80 of 91.67% and a median score of 100. Broad chart analysis, categorical matrices, multi-series trends, and stacked-output trends scored between 98.46 and 100.

### 3.3 Real-model matrix

The real-model matrix covered four presentations, 35 slides, 397 elements, 19 tables, 22 charts, and 1,227 data points across 81 questions:

| Metric | Result |
|---|---:|
| Completed executions | 81 / 81 |
| Basic readability | 81 / 81 |
| Model answers accepted directly | 42 |
| Delivered after verifier repair | 37 |
| Safe fallbacks | 2 |
| Citation-validation failures | 0 |

The important result is not first-pass model acceptance. The verification layer repaired 37 locally flawed candidates before delivery, and all 81 tasks ended with a usable result.

## 4. Cross-document capability

Cross-document questions can be dominated by one highly similar source. After document lanes and evidence quotas were introduced, a representative question improved from covering 1 / 3 documents with one citation to 3 / 3 documents with eight citations. The model was unchanged; the gain came from retrieval architecture.

## 5. How evaluation changes the code

| Evaluation signal | Design response |
|---|---|
| Correct value but possible series leakage | ChartDataCube + axis/series scope verifier |
| Relevant retrieval but missing sub-question | requirement coverage + adequacy check |
| One document monopolizes evidence | per-document lane + quota |
| Chart calculation hides other requested scopes | composite calculation + cross-scope evidence preservation |
| Answer reads like an evidence dump | fact-preserving polishing agent |
| Generation introduces a new number | numeric allowlist + repair/fallback |

Evaluation is therefore not a final scorecard. It is a design input to the planner, retriever, calculator, polisher, and verifier.

## 6. Release quality gate

A release requires deterministic tests to pass, critical numbers and citations to remain valid, workspace scope to be correct, agent loops to terminate within policy, and benchmarks to avoid material regression. More natural prose cannot compensate for weaker grounding or calculation accuracy.

## 7. Implementation anchors

- Evaluation entry point: `scripts/evaluate_qbr_benchmark.py`
- Real-model matrix: `scripts/run_output_ppt_e2e_matrix.py`
- Datasets: `benchmarks/`
- Results: `benchmark_results/`
- Quality dashboard: `apps/web/src/pages/AnalyticsPage.tsx`
