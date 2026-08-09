---
name: extract-ppt-chart-data
description: Extract complex native chart data from PowerPoint .ppt, .pptx, .pptm, .pps, and .ppsx files into auditable JSON and CSV files. Use when Codex needs to recover exact series, categories, XY or bubble values, multi-level category labels, combo-chart plot types, primary or secondary axes, formulas, embedded Excel data, ChartEx data, or chart provenance; inventory image-based charts for visual fallback; or convert legacy PPT before chart extraction.
---

# Extract PPT Chart Data

Extract chart source data before attempting OCR. Preserve formulas, cached values, embedded-workbook values, chart types, axes, and provenance so downstream analysis remains auditable.

## Run the extraction

1. Resolve the input presentation and output directory. If the user does not specify an output directory, create `<presentation-stem>-chart-data` beside the input when writable, otherwise use the current working directory.
2. Run the bundled standard-library parser:

```bash
python3 <skill-dir>/scripts/extract_ppt_chart_data.py \
  "/absolute/path/to/input.pptx" \
  --output-dir "/absolute/path/to/output" \
  --extract-workbooks
```

3. Add `--overwrite` only when the user authorized replacing prior extraction files. Never delete the output directory.
4. For legacy `.ppt` or `.pps`, allow the script to invoke LibreOffice. Pass `--soffice /path/to/soffice` only when auto-discovery fails.
5. Report the absolute output paths and the counts/status emitted by the command.

The script has no Python package dependencies. It reads OOXML directly and uses LibreOffice only for legacy binary conversion.

## Interpret the files

- Treat `charts.json` as the canonical, loss-minimizing output.
- Use `charts.csv` as the chart inventory.
- Use `chart-points.csv` as the normalized long table for Excel, SQL, pandas, or BI ingestion.
- Use `workbooks/*.xlsx` as preserved evidence when `--extract-workbooks` is enabled.
- Read [output-schema.md](references/output-schema.md) before transforming the canonical JSON or mapping it into a database.

Prefer values in this order:

1. `embedded_workbook`
2. `chart_cache_or_literal`
3. visual reconstruction

When workbook values replace a chart cache, retain fields such as `value_cache`, `category_cache`, and `name_cache`; do not remove the discrepancy evidence.

## Handle complex charts

- Keep every plot in a combo chart. Use each series' `chart_type`, `grouping`, and `axis_ids`; never assign one chart-wide type to all series.
- Keep `x`, `y`, and `size` distinct for scatter and bubble charts.
- Keep `category_levels` for hierarchical category axes.
- Keep raw numeric values separate from `number_format`; for example, retain `0.25` with `0%` rather than rewriting it as `25`.
- Keep ChartEx dimension keys in `extra_dimensions` when using CSV. Examples include funnel, histogram, waterfall, treemap, sunburst, box-and-whisker, and map-related dimensions.
- Never fetch an external workbook. Use the saved cache and surface `external_source_unavailable`.

## Route visual fallbacks

Inspect `slides[].visual_fallback_candidates` and chart warnings after native extraction.

- Treat `no_native_series`, a low chart confidence, or a chart-looking picture as a visual-review request.
- Render the affected slide at high resolution and inspect only the candidate bounding box when visual tooling is available.
- Extract visible labels first, then infer axis geometry. Set unreadable values to `null`; never invent precise values from a trend line.
- Store any visually reconstructed table in a separate file such as `visual-chart-points.csv` and include `source=visual_reconstruction`, confidence, slide number, and candidate bounding box. Do not silently merge it into native values.
- Mark the result partial when exact values cannot be recovered.

Read [coverage-and-validation.md](references/coverage-and-validation.md) for format coverage, validation checks, and known limits before making completeness claims.

## Validate the result

Check all of the following:

- Compare `summary.native_chart_count` with the visible chart count.
- Confirm each expected series has a stable name, plot type, and point count.
- Confirm secondary-axis series use the expected axis IDs.
- Review every warning and every external link.
- Compare workbook-backed points with their `*_cache` fields when both exist.
- Open `chart-points.csv` with UTF-8 BOM support for Chinese text.
- Run the bundled self-test after modifying the parser:

```bash
python3 <skill-dir>/scripts/self_test.py
```

Return a concise quality note with source priority, unresolved visual candidates, and any partial-extraction reason.
