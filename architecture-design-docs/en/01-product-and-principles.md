# 01. Product Positioning and Design Principles

## 1. One-line summary

QBR Insight Agent turns quarterly business-review presentations from page-bound files into a queryable, calculable knowledge base whose conclusions can be traced to the source slide.

This is not generic document chat. The value of a QBR is concentrated in charts, tables, periods, units, and reporting bases. A fluent answer is useless if it binds a number to the wrong market or month. The product objective is therefore precise:

> Deliver a management conclusion in conversation while preserving the complete path from conclusion to calculation, evidence element, and original slide.

## 2. Users and jobs

| User | Typical job | System output |
|---|---|---|
| Business leader | Understand growth, mix, and operating signals | Concise findings with numbers and sources |
| Analyst | Compare periods, markets, channels, and products | Replayable deltas, growth, shares, and attribution |
| Reviewer | Inspect chart extraction and answer support | Source slide, bounding box, provenance, and revision history |
| Administrator | Operate documents, jobs, and quality | Lifecycle, queues, audit, and analytics |

The product loop is more than upload followed by chat:

```mermaid
flowchart LR
    A["Upload QBR"] --> B["Parse text, tables, and charts"]
    B --> C["Build retrievable evidence"]
    C --> D["Ask, compare, and calculate"]
    D --> E["Open source citation"]
    E --> F["Review and revise"]
    F --> C

    classDef primary fill:#16324F,color:#fff,stroke:#16324F;
    classDef secondary fill:#E9F3FA,color:#16324F,stroke:#4A90B8;
    class A,D,E primary;
    class B,C,F secondary;
```

## 3. Four architectural judgements

### 3.1 Native data owns numbers; models add semantics

Editable charts are read from embedded workbooks, chart caches, and OOXML. Vision supplies page semantics and content that has no native representation. Source precedence is explicit, so the model never re-guesses a value already present in the file.

### 3.2 The raw question is the sole answer contract

Planning may add synonyms, bilingual vocabulary, and retrieval hypotheses, but it cannot redefine the requested outcome. Generation, completeness checks, and final verification always return to the user's wording. This prevents compound questions from drifting across multiple model calls.

### 3.3 Calculation and communication have separate authority

Period change, share, extrema, threshold decisions, and cross-source reconciliation are deterministic. The model explains the verified result. Every calculation retains its inputs, unit, scope, evidence, and replayable formula.

### 3.4 Citation is a product feature

A citation resolves through `document_version → slide → element → bbox`. Clicking an answer reference opens the slide and highlights its evidence. The interaction replaces “trust the model” with “verify in seconds.”

## 4. Product differentiation

| Common approach | QBR Insight Agent |
|---|---|
| Flatten a deck and run vector search | Preserve pages, elements, tables, chart series, and points |
| Ask a multimodal model to answer from pixels | Prefer native structure; use vision as a supplemental channel |
| Return one generated paragraph | Return answer, citations, calculation facts, events, and source navigation |
| Route through one intent label | Compose evidence checks from raw question clauses |
| Provider failure stops the product | Local retrieval, table reasoning, and chart calculation remain available |

## 5. Scope

The current release concentrates on ingesting, analysing, questioning, and reviewing PPTX business material. It does not edit PowerPoint or issue audit opinions. That boundary keeps engineering effort on the differentiators: complex chart structure, evidence retrieval, deterministic reasoning, citations, and quality control.

## 6. Implementation anchors

- Document entry: `apps/api/routes/documents.py`
- QA entry: `apps/api/routes/conversations.py`
- Structured reasoning: `packages/qbr_core/analysis/`
- Evidence contract: `packages/qbr_core/retrieval/coverage.py`
- Citation navigation: `packages/qbr_core/application/results.py`
