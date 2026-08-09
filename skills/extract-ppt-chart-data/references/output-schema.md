# Output schema

## File set

| File | Purpose |
|---|---|
| `charts.json` | Canonical hierarchy, provenance, formulas, caches, axes, warnings |
| `charts.csv` | One row per native chart |
| `chart-points.csv` | One row per native data point |
| `workbooks/*.xlsx` | Optional byte-for-byte copies of embedded workbook packages |

## Canonical JSON

Top-level fields:

- `schema_version`: Output contract version.
- `input`: Absolute source path, SHA-256, size, detected container kind, and conversion flag.
- `parser`: Skill and engine versions.
- `summary`: Slide/chart/candidate counts and `complete` or `partial` status.
- `slides`: Slide titles, chart IDs, and image candidates requiring visual review.
- `charts`: Complete decoded native charts.
- `warnings`: Package- or slide-level warnings.

Each chart contains:

- Identity: `chart_id`, `slide_number`, `chart_index`, `chart_part`, `bbox_emu`.
- Structure: `schema`, `title`, `chart_types`, `axes`, `series`.
- Provenance: `data_source`, `workbook`, `confidence`, `warnings`.

Each series contains:

- `series_index`, `series_order`, `name`, `name_formula`.
- `chart_type`, `grouping`, `axis_ids`.
- `formulas`, `number_format`, `visual`.
- `points`.

Classic chart points use:

- `point_index`
- `category` and optional `category_levels`
- `value`, or `x` and `y`, plus optional `size`
- `source`
- optional `*_cache` fields when workbook values supersede saved chart caches

ChartEx points map common `cat`/`val`/`x`/`y`/`size` dimensions into canonical point fields and preserve all original dimension names under `dimensions`. The normalized CSV places that audit map and non-classic dimensions in `extra_dimensions` as JSON.

## Null and number rules

- Use JSON `null` for absent values.
- Preserve numeric source values as JSON numbers when valid and finite.
- Preserve non-numeric error/string values as strings.
- Do not apply display formats to raw values.
- Preserve category order and `point_index`; do not sort points by value.

## Provenance rules

- `embedded_workbook`: Resolved from the embedded XLSX cell range referenced by a chart formula.
- `chart_cache_or_literal`: Read from DrawingML cache/literal nodes.
- `chartex_cache_or_literal`: Read from the ChartEx dimensional cache/literal nodes.
- `visual_reconstruction`: Must be written separately by a visual-review step.
