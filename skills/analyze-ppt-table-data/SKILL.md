---
name: analyze-ppt-table-data
description: Analyze parser-preserved PowerPoint table rows with deterministic, auditable calculations. Use when Codex or the QBR runtime needs to answer exact lookup, difference, ratio, threshold, ranking, filtering, or aggregation questions from structured PPT or PPTX table data without calling a language model.
---

# Analyze PPT Table Data

Use structured rows extracted from native PowerPoint tables. Preserve the source object so every answer can cite its slide and element.

## Run the analysis

1. Pass the user question and retrieved table sources to `scripts/analyze_ppt_table_data.py:answer`.
2. Prefer this deterministic result before chart reasoning, free-form retrieval, or model generation.
3. Return `None` when the requested operation is unsupported or the table evidence is insufficient.
4. Never infer missing cells or fetch external data.

Each source must contain pipe-delimited table rows in `content`; keep citation metadata such as `document_version_id`, `slide_id`, `element_id`, `bbox_json`, and `confidence` alongside it.

## Validate changes

Run `pytest tests/unit/test_structured_reasoning.py tests/unit/test_skill_registry.py` after modifying the wrapper or reasoning implementation.
