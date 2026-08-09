# Coverage and validation

## Contents

1. Native extraction model
2. Supported structures
3. Validation checklist
4. Known limits
5. Primary references

## Native extraction model

A PPTX is an OPC ZIP package. Slides reference chart parts through relationship files. Classic DrawingML charts keep series formulas and usually a saved cache; PowerPoint chart parts can also reference an embedded spreadsheet package. Newer ChartEx charts use a dimensional `chartData` model whose series reference data IDs.

Use source priority `embedded workbook > chart cache/literal > visual reconstruction`. This is a reliability rule, not a claim that workbook and cache always agree. Preserve both when they differ.

## Supported structures

The bundled parser covers:

- Standard category charts: column/bar, line, area, pie/doughnut, radar, stock, and surface families.
- Scatter and bubble roles: `xVal`, `yVal`, and `bubbleSize`.
- Combo charts with multiple plot elements and series-specific chart types.
- Primary/secondary axis IDs, axis positions, crossing IDs, min/max, log base, number format, and display units.
- Multi-level string category caches.
- Series name/category/value formulas and cached or literal values.
- Embedded XLSX worksheets, shared strings, inline strings, booleans, cached formula results, and A1 ranges.
- ChartEx series, data IDs, numeric/string dimensions, hierarchy levels, layout IDs, and axis IDs.
- Legacy binary PPT/PPS after LibreOffice headless conversion.
- Read-only PPTM parsing; VBA is never executed.

## Validation checklist

1. Count visible charts and native chart relationships per slide.
2. Compare every series name and point count against the embedded workbook.
3. Confirm categories retain source order and hierarchical levels.
4. Confirm scatter/bubble data did not collapse into category/value columns.
5. Confirm combo-series plot type and axis binding individually.
6. Compare workbook values against retained `*_cache` values.
7. Confirm percentages, currencies, dates, and display units retain raw values plus formats.
8. Review external workbook warnings and never use network access to resolve them.
9. Inspect picture candidates only when they visually resemble charts.
10. Mark visual estimates as a separate provenance class with confidence.

## Known limits

- Binary PPT conversion can change unsupported Office-only objects; verify slides containing unusual OLE or proprietary add-in charts.
- Password-protected or damaged packages cannot be parsed.
- External workbooks are not fetched. Only cached values saved in the presentation remain available.
- Complex Excel formulas are not calculated. The reader uses the cached worksheet value stored in the file.
- Date serials remain numeric unless the chart cache already contains display labels; retain number formats for downstream conversion.
- Image charts, shape-drawn charts, SmartArt, and pasted EMF/WMF graphics require visual reconstruction.
- Vendor-specific chart extensions outside classic DrawingML and Microsoft ChartEx can yield `no_native_series`.
- Theme-derived colors may remain scheme names instead of resolved RGB values.
- A picture candidate is an inventory item, not proof that the picture is a chart.

## Primary references

- [Microsoft: Structure of a PresentationML document](https://learn.microsoft.com/en-us/office/open-xml/presentation/structure-of-a-presentationml-document)
- [Microsoft Open Specifications: ChartEx CT_ChartSpace](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-odrawxml/416e664f-c6f6-4ed9-914b-4eaaa20724dd)
- [Microsoft Open Specifications: ChartEx CT_ChartData](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-odrawxml/8a963284-245d-4c2a-935d-cc6aef448b09)
- [Microsoft Open Specifications: ChartEx CT_Series](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-odrawxml/86df19ea-8e94-49fc-9160-377c1b40b985)
- [Microsoft Open Specifications: CT_NumericDimension](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-odrawxml/37fbbd78-c3e6-4f33-9d0b-3fe428ad1c09)
- [python-pptx: Working with charts](https://python-pptx.readthedocs.io/en/latest/user/charts.html)
- [python-pptx: Chart embedded worksheet analysis](https://python-pptx.readthedocs.io/en/latest/dev/analysis/cht-access-xlsx.html)
- [python-pptx: Multi-plot chart analysis](https://python-pptx.readthedocs.io/en/stable/dev/analysis/cht-plots.html)
