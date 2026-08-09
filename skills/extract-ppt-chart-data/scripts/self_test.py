#!/usr/bin/env python3
"""End-to-end self-test for extract_ppt_chart_data.py."""

from __future__ import annotations

import csv
import json
import tempfile
import zipfile
from pathlib import Path

from extract_ppt_chart_data import Limits, extract, write_outputs


PRESENTATION = """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst>
</p:presentation>"""

PRESENTATION_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
</Relationships>"""

SLIDE_1 = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:cSld><p:spTree>
  <p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Quarterly metrics</a:t></a:r></a:p></p:txBody></p:sp>
  <p:graphicFrame><p:xfrm><a:off x="100" y="200"/><a:ext cx="800" cy="500"/></p:xfrm>
   <a:graphic><a:graphicData><c:chart r:id="rId1"/></a:graphicData></a:graphic>
  </p:graphicFrame>
 </p:spTree></p:cSld>
</p:sld>"""

SLIDE_2 = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:cSld><p:spTree><p:graphicFrame><p:xfrm><a:off x="1" y="2"/><a:ext cx="3" cy="4"/></p:xfrm>
  <a:graphic><a:graphicData><cx:chart r:id="rId1"/></a:graphicData></a:graphic>
 </p:graphicFrame></p:spTree></p:cSld>
</p:sld>"""

SLIDE_1_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
</Relationships>"""

SLIDE_2_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.microsoft.com/office/2014/relationships/chartEx" Target="../charts/chartEx1.xml"/>
</Relationships>"""

CLASSIC_CHART = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Revenue &amp; margin</a:t></a:r></a:p></c:rich></c:tx></c:title>
 <c:plotArea>
  <c:barChart><c:grouping val="clustered"/><c:ser><c:idx val="0"/><c:order val="0"/>
   <c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:pt idx="0"><c:v>Revenue</c:v></c:pt></c:strCache></c:strRef></c:tx>
   <c:cat><c:strRef><c:f>Sheet1!$A$2:$A$4</c:f><c:strCache><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt><c:pt idx="2"><c:v>Q3</c:v></c:pt></c:strCache></c:strRef></c:cat>
   <c:val><c:numRef><c:f>Sheet1!$B$2:$B$4</c:f><c:numCache><c:formatCode>0.0</c:formatCode><c:pt idx="0"><c:v>9</c:v></c:pt><c:pt idx="1"><c:v>19</c:v></c:pt><c:pt idx="2"><c:v>29</c:v></c:pt></c:numCache></c:numRef></c:val>
  </c:ser><c:axId val="10"/><c:axId val="20"/></c:barChart>
  <c:lineChart><c:grouping val="standard"/><c:ser><c:idx val="1"/><c:order val="1"/>
   <c:tx><c:strRef><c:f>Sheet1!$C$1</c:f><c:strCache><c:pt idx="0"><c:v>Margin</c:v></c:pt></c:strCache></c:strRef></c:tx>
   <c:cat><c:strRef><c:f>Sheet1!$A$2:$A$4</c:f><c:strCache><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt><c:pt idx="2"><c:v>Q3</c:v></c:pt></c:strCache></c:strRef></c:cat>
   <c:val><c:numRef><c:f>Sheet1!$C$2:$C$4</c:f><c:numCache><c:formatCode>0%</c:formatCode><c:pt idx="0"><c:v>0.2</c:v></c:pt><c:pt idx="1"><c:v>0.25</c:v></c:pt><c:pt idx="2"><c:v>0.3</c:v></c:pt></c:numCache></c:numRef></c:val>
  </c:ser><c:axId val="10"/><c:axId val="30"/></c:lineChart>
  <c:catAx><c:axId val="10"/><c:axPos val="b"/><c:crossAx val="20"/></c:catAx>
  <c:valAx><c:axId val="20"/><c:axPos val="l"/><c:scaling><c:min val="0"/></c:scaling><c:crossAx val="10"/></c:valAx>
  <c:valAx><c:axId val="30"/><c:axPos val="r"/><c:scaling><c:min val="0"/><c:max val="1"/></c:scaling><c:numFmt formatCode="0%"/><c:crossAx val="10"/></c:valAx>
 </c:plotArea></c:chart><c:externalData r:id="rId1"/>
</c:chartSpace>"""

CLASSIC_CHART_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="../embeddings/workbook1.xlsx"/>
</Relationships>"""

CHARTEX = """<?xml version="1.0" encoding="UTF-8"?>
<cx:chartSpace xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <cx:chartData><cx:data id="1">
  <cx:strDim type="cat"><cx:f>Sheet1!$A$2:$A$4</cx:f><cx:lvl><cx:v idx="0">Lead</cx:v><cx:v idx="1">SQL</cx:v><cx:v idx="2">Won</cx:v></cx:lvl></cx:strDim>
  <cx:numDim type="val"><cx:f>Sheet1!$B$2:$B$4</cx:f><cx:lvl><cx:v idx="0">100</cx:v><cx:v idx="1">50</cx:v><cx:v idx="2">10</cx:v></cx:lvl></cx:numDim>
 </cx:data></cx:chartData>
 <cx:chart><cx:title><cx:tx><cx:rich><a:p><a:r><a:t>Pipeline</a:t></a:r></a:p></cx:rich></cx:tx></cx:title>
 <cx:plotArea><cx:plotAreaRegion><cx:series layoutId="funnel"><cx:tx><cx:rich><a:p><a:r><a:t>Pipeline</a:t></a:r></a:p></cx:rich></cx:tx><cx:dataId val="1"/></cx:series></cx:plotAreaRegion></cx:plotArea>
 </cx:chart>
</cx:chartSpace>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
 <row r="1"><c r="A1" t="inlineStr"><is><t>Quarter</t></is></c><c r="B1" t="inlineStr"><is><t>Revenue</t></is></c><c r="C1" t="inlineStr"><is><t>Margin</t></is></c></row>
 <row r="2"><c r="A2" t="inlineStr"><is><t>Q1</t></is></c><c r="B2"><v>10</v></c><c r="C2"><v>0.2</v></c></row>
 <row r="3"><c r="A3" t="inlineStr"><is><t>Q2</t></is></c><c r="B3"><v>20</v></c><c r="C3"><v>0.25</v></c></row>
 <row r="4"><c r="A4" t="inlineStr"><is><t>Q3</t></is></c><c r="B4"><v>30</v></c><c r="C4"><v>0.3</v></c></row>
</sheetData></worksheet>"""


def make_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET)


def make_pptx(path: Path) -> None:
    with tempfile.TemporaryDirectory() as temp:
        xlsx = Path(temp) / "workbook1.xlsx"
        make_xlsx(xlsx)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ppt/presentation.xml", PRESENTATION)
            zf.writestr("ppt/_rels/presentation.xml.rels", PRESENTATION_RELS)
            zf.writestr("ppt/slides/slide1.xml", SLIDE_1)
            zf.writestr("ppt/slides/slide2.xml", SLIDE_2)
            zf.writestr("ppt/slides/_rels/slide1.xml.rels", SLIDE_1_RELS)
            zf.writestr("ppt/slides/_rels/slide2.xml.rels", SLIDE_2_RELS)
            zf.writestr("ppt/charts/chart1.xml", CLASSIC_CHART)
            zf.writestr("ppt/charts/_rels/chart1.xml.rels", CLASSIC_CHART_RELS)
            zf.writestr("ppt/charts/chartEx1.xml", CHARTEX)
            zf.write(xlsx, "ppt/embeddings/workbook1.xlsx")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ppt-chart-selftest-") as temp:
        root = Path(temp)
        pptx = root / "complex.pptx"
        out = root / "out"
        make_pptx(pptx)
        result = extract(pptx, out, Limits(), True, "soffice", 30)
        files = write_outputs(result, out, overwrite=False, pretty=True)

        assert result["summary"]["slide_count"] == 2
        assert result["summary"]["native_chart_count"] == 2
        classic, chartex = result["charts"]
        assert classic["chart_types"] == ["barChart", "lineChart"]
        assert classic["series"][0]["points"][0]["value"] == 10
        assert classic["series"][0]["points"][0]["value_cache"] == 9
        assert classic["series"][1]["points"][2]["value"] == 0.3
        assert classic["series"][0]["points"][1]["category"] == "Q2"
        assert chartex["chart_types"] == ["funnel"]
        assert chartex["series"][0]["points"][2]["value"] == 10
        assert chartex["series"][0]["points"][2]["dimensions"]["cat"] == "Won"
        assert (out / "workbooks/slide-001-chart-01.xlsx").is_file()
        with (out / "chart-points.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 9
        json.loads((out / "charts.json").read_text(encoding="utf-8"))
        print(json.dumps({"status": "ok", "charts": 2, "points": len(rows),
                          "files": [path.name for path in files]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
