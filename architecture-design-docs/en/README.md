# QBR Insight Agent: Architecture Design

This system turns facts scattered across PowerPoint text, tables, charts, and notes into answers that can be traced, calculated, and reviewed in a business-review workflow.

It is not a chat interface wrapped around generic RAG. The design objective is to preserve numbers, units, periods, and reporting bases through parsing, retrieval, calculation, and generation—and still navigate back to the original slide for verification.

> Source PPT → structured facts → hybrid retrieval → deterministic calculation → controlled generation → factual verification → source-slide navigation

## Reading guide

| Chapter | Focus |
|---|---|
| [01 Product and principles](./01-product-and-principles.md) | Business problem, user value, and product decisions |
| [02 System architecture](./02-system-architecture.md) | Boundaries, technology architecture, and end-to-end flows |
| [03 Ingestion and knowledge modeling](./03-ingestion-and-knowledge.md) | Dual-path PPT parsing, fact precedence, and the skill mechanism |
| [04 AI agent workflow](./04-agent-workflow.md) | Planning, tool use, verification, repair, and termination |
| [05 Context and RAG](./05-context-and-rag.md) | Reproducible context, hybrid recall, and EvidencePack |
| [06 Modules, APIs, and data](./06-engineering-api-data.md) | Application boundaries, frontend contract, domain model, and ER diagram |
| [07 Security, operations, and evolution](./07-security-operations-evolution.md) | Trust boundaries, authorization, lifecycle, deployment, and growth path |
| [08 System test report](./08-system-test-report.md) | Automated and real-browser validation of the current baseline |
| [09 AI evaluation report](./09-ai-evaluation-report.md) | Datasets, metrics, representative results, and quality loop |

## Design summary

The strongest part of the project is not the choice of model. It is the set of engineering decisions around it:

1. **The raw question is an immutable contract.** A planner may expand retrieval terms, but it cannot redefine the answer objective; completeness checks return to the user's words.
2. **Structured facts take precedence over visual inference.** Charts first use embedded workbooks, caches, and native objects; vision fills genuine gaps.
3. **The model does not own numeric truth.** Exact values come from relational facts and replayable tools; the LLM interprets intent and communicates results.
4. **A citation is an executable data relationship.** Evidence binds document, slide, element, and bounding box, allowing the UI to navigate to the source.
5. **Reliability comes from workflow design.** Frozen context, retrieval quotas, composable validators, repair, and safe fallback are enforced in code.

For an interview, begin with the main path in [02 System Architecture](./02-system-architecture.md), then expand into agent design, data modeling, security, or evaluation according to the role.

Code baseline: `8739c13`; reviewed: 2026-08-13. Chinese edition: [中文版](../zh/README.md).
