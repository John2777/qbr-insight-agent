#!/usr/bin/env python3
"""Extract native chart data from PPT/PPTX into auditable JSON and CSV files.

The parser intentionally uses only the Python standard library. It reads OOXML
directly, follows package relationships, reads chart caches, and resolves ranges
in embedded XLSX workbooks. Legacy binary PPT files are converted with
LibreOffice before parsing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "cx": "http://schemas.microsoft.com/office/drawing/2014/chartex",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
RID = "{%s}id" % NS["r"]

CHART_REL_SUFFIXES = ("/chart", "/chartEx")
EMBEDDED_REL_SUFFIXES = ("/package", "/oleObject")
CLASSIC_SERIES_ROLES = ("cat", "val", "xVal", "yVal", "bubbleSize")


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    max_entries: int = 20_000
    max_entry_bytes: int = 250 * 1024 * 1024
    max_total_bytes: int = 1_000 * 1024 * 1024
    max_ratio: float = 500.0


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def text_of(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None


def attr_val(node: ET.Element | None, name: str = "val") -> str | None:
    return node.get(name) if node is not None else None


def parse_number(raw: str | None) -> int | float | str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return ""
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        number = float(value)
        return number if math.isfinite(number) else value
    except ValueError:
        return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_member_name(name: str) -> bool:
    p = PurePosixPath(name)
    return not (p.is_absolute() or ".." in p.parts or "\x00" in name)


def validate_zip(archive: zipfile.ZipFile, limits: Limits) -> None:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise ExtractionError(f"ZIP has {len(infos)} entries; limit is {limits.max_entries}")
    total = 0
    for info in infos:
        if not safe_member_name(info.filename):
            raise ExtractionError(f"Unsafe ZIP member path: {info.filename!r}")
        if info.file_size > limits.max_entry_bytes:
            raise ExtractionError(f"ZIP member too large: {info.filename}")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise ExtractionError("Expanded ZIP size exceeds configured limit")
        if info.compress_size == 0:
            ratio = float("inf") if info.file_size else 1.0
        else:
            ratio = info.file_size / info.compress_size
        if ratio > limits.max_ratio:
            raise ExtractionError(f"Suspicious compression ratio for {info.filename}")


def parse_xml(data: bytes, part: str) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ExtractionError(f"DTD/entity declarations are not allowed in {part}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ExtractionError(f"Invalid XML in {part}: {exc}") from exc


def rels_part(part: str) -> str:
    directory, basename = posixpath.split(part)
    return posixpath.join(directory, "_rels", basename + ".rels")


def resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        resolved = target.lstrip("/")
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if not safe_member_name(resolved):
        raise ExtractionError(f"Unsafe relationship target from {source_part}: {target}")
    return resolved


def relationships(archive: zipfile.ZipFile, source_part: str) -> dict[str, dict[str, str]]:
    part = rels_part(source_part)
    try:
        root = parse_xml(archive.read(part), part)
    except KeyError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for rel in root.findall("pr:Relationship", NS):
        rid = rel.get("Id")
        if not rid:
            continue
        target_mode = rel.get("TargetMode", "Internal")
        target = rel.get("Target", "")
        result[rid] = {
            "type": rel.get("Type", ""),
            "target": target if target_mode == "External" else resolve_target(source_part, target),
            "target_mode": target_mode,
        }
    return result


def presentation_slides(archive: zipfile.ZipFile) -> list[tuple[int, str]]:
    try:
        root = parse_xml(archive.read("ppt/presentation.xml"), "ppt/presentation.xml")
    except KeyError as exc:
        raise ExtractionError("Not a valid PPTX: ppt/presentation.xml is missing") from exc
    rels = relationships(archive, "ppt/presentation.xml")
    slides: list[tuple[int, str]] = []
    for index, slide_id in enumerate(root.findall(".//p:sldId", NS), start=1):
        rid = slide_id.get(RID)
        rel = rels.get(rid or "")
        if rel and rel["target_mode"] == "Internal":
            slides.append((index, rel["target"]))
    return slides


def slide_title(root: ET.Element) -> str | None:
    for shape in root.findall(".//p:sp", NS):
        ph = shape.find("./p:nvSpPr/p:nvPr/p:ph", NS)
        if ph is not None and ph.get("type") in {"title", "ctrTitle"}:
            parts = [t.text or "" for t in shape.findall(".//a:t", NS)]
            title = "".join(parts).strip()
            if title:
                return title
    return None


def xfrm_bbox(frame: ET.Element) -> dict[str, int] | None:
    xfrm = frame.find("./p:xfrm", NS) or frame.find(".//a:xfrm", NS)
    if xfrm is None:
        return None
    off = xfrm.find("./a:off", NS)
    ext = xfrm.find("./a:ext", NS)
    if off is None or ext is None:
        return None
    try:
        return {"x": int(off.get("x", "0")), "y": int(off.get("y", "0")),
                "w": int(ext.get("cx", "0")), "h": int(ext.get("cy", "0"))}
    except ValueError:
        return None


def chart_refs_on_slide(root: ET.Element) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for frame in root.findall(".//p:graphicFrame", NS):
        for node in frame.iter():
            rid = node.get(RID)
            if rid and local_name(node.tag) == "chart" and rid not in seen:
                refs.append({"rid": rid, "bbox_emu": xfrm_bbox(frame)})
                seen.add(rid)
    return refs


def picture_candidates(root: ET.Element) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for idx, pic in enumerate(root.findall(".//p:pic", NS), start=1):
        c_nv_pr = pic.find("./p:nvPicPr/p:cNvPr", NS)
        xfrm = pic.find("./p:spPr/a:xfrm", NS)
        bbox = None
        if xfrm is not None:
            off, ext = xfrm.find("./a:off", NS), xfrm.find("./a:ext", NS)
            if off is not None and ext is not None:
                try:
                    bbox = {"x": int(off.get("x", "0")), "y": int(off.get("y", "0")),
                            "w": int(ext.get("cx", "0")), "h": int(ext.get("cy", "0"))}
                except ValueError:
                    pass
        candidates.append({
            "picture_index": idx,
            "name": c_nv_pr.get("name") if c_nv_pr is not None else None,
            "description": c_nv_pr.get("descr") if c_nv_pr is not None else None,
            "bbox_emu": bbox,
            "reason": "picture_may_contain_chart; visual inspection required",
        })
    return candidates


def all_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    runs = [x.text or "" for x in node.findall(".//a:t", NS)]
    if runs:
        return "".join(runs).strip() or None
    values = [x.text or "" for x in node.iter() if local_name(x.tag) in {"v", "pt"} and x.text]
    return " ".join(values).strip() or None


def indexed_points(cache: ET.Element | None, numeric: bool = False) -> dict[int, Any]:
    if cache is None:
        return {}
    points: dict[int, Any] = {}
    for pt in cache.findall("./c:pt", NS):
        try:
            idx = int(pt.get("idx", "0"))
        except ValueError:
            continue
        raw = text_of(pt.find("./c:v", NS))
        points[idx] = parse_number(raw) if numeric else raw
    return points


def classic_data_node(parent: ET.Element, role: str) -> dict[str, Any]:
    node = parent.find(f"./c:{role}", NS)
    if node is None:
        return {"formula": None, "format_code": None, "levels": [], "values": {}}
    formula = text_of(node.find(".//c:f", NS))
    format_code = text_of(node.find(".//c:formatCode", NS))
    multi = node.find(".//c:multiLvlStrCache", NS)
    if multi is not None:
        levels = [indexed_points(level, numeric=False) for level in multi.findall("./c:lvl", NS)]
        count = max((max(level.keys(), default=-1) for level in levels), default=-1) + 1
        combined = {i: [level.get(i) for level in levels] for i in range(count)}
        return {"formula": formula, "format_code": format_code, "levels": levels, "values": combined}
    cache = None
    numeric = role in {"val", "xVal", "yVal", "bubbleSize"}
    for candidate in node.iter():
        if local_name(candidate.tag) in {"numCache", "strCache", "numLit", "strLit"}:
            cache = candidate
            numeric = local_name(candidate.tag).startswith("num")
            break
    return {"formula": formula, "format_code": format_code, "levels": [],
            "values": indexed_points(cache, numeric=numeric)}


def series_name_classic(series: ET.Element) -> str | None:
    tx = series.find("./c:tx", NS)
    if tx is None:
        return None
    cached = text_of(tx.find(".//c:v", NS))
    return cached or all_text(tx)


def series_name_formula_classic(series: ET.Element) -> str | None:
    return text_of(series.find("./c:tx/c:strRef/c:f", NS))


def series_visual(series: ET.Element) -> dict[str, Any]:
    sp_pr = series.find("./c:spPr", NS)
    if sp_pr is None:
        return {}
    visual: dict[str, Any] = {}
    srgb = sp_pr.find(".//a:srgbClr", NS)
    scheme = sp_pr.find(".//a:schemeClr", NS)
    if srgb is not None:
        visual["color"] = "#" + srgb.get("val", "")
    elif scheme is not None:
        visual["scheme_color"] = scheme.get("val")
    line = sp_pr.find("./a:ln", NS)
    if line is not None:
        visual["line_width_emu"] = parse_number(line.get("w"))
        dash = line.find("./a:prstDash", NS)
        if dash is not None:
            visual["line_dash"] = dash.get("val")
    return visual


def chart_title_classic(root: ET.Element) -> str | None:
    title = root.find("./c:chart/c:title", NS)
    return all_text(title)


def parse_classic_axes(root: ET.Element) -> list[dict[str, Any]]:
    axes: list[dict[str, Any]] = []
    for axis_type in ("catAx", "valAx", "dateAx", "serAx"):
        for axis in root.findall(f"./c:chart/c:plotArea/c:{axis_type}", NS):
            scaling = axis.find("./c:scaling", NS)
            display_units = axis.find("./c:dispUnits/c:builtInUnit", NS)
            num_fmt = axis.find("./c:numFmt", NS)
            axes.append({
                "axis_type": axis_type,
                "axis_id": attr_val(axis.find("./c:axId", NS)),
                "cross_axis_id": attr_val(axis.find("./c:crossAx", NS)),
                "position": attr_val(axis.find("./c:axPos", NS)),
                "title": all_text(axis.find("./c:title", NS)),
                "minimum": parse_number(attr_val(scaling.find("./c:min", NS)) if scaling is not None else None),
                "maximum": parse_number(attr_val(scaling.find("./c:max", NS)) if scaling is not None else None),
                "log_base": parse_number(attr_val(scaling.find("./c:logBase", NS)) if scaling is not None else None),
                "orientation": attr_val(scaling.find("./c:orientation", NS)) if scaling is not None else None,
                "number_format": num_fmt.get("formatCode") if num_fmt is not None else None,
                "display_units": attr_val(display_units),
            })
    return axes


def build_points(data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    indices: set[int] = set()
    for item in data.values():
        indices.update(item["values"].keys())
    points = []
    for idx in sorted(indices):
        category = data["cat"]["values"].get(idx)
        if isinstance(category, list):
            category_levels = category
            category = " / ".join(str(x) for x in category if x is not None)
        else:
            category_levels = None
        point = {
            "point_index": idx,
            "category": category,
            "category_levels": category_levels,
            "value": data["val"]["values"].get(idx),
            "x": data["xVal"]["values"].get(idx),
            "y": data["yVal"]["values"].get(idx),
            "size": data["bubbleSize"]["values"].get(idx),
            "source": "chart_cache_or_literal",
        }
        points.append(point)
    return points


def parse_classic_chart(root: ET.Element) -> dict[str, Any]:
    plot_area = root.find("./c:chart/c:plotArea", NS)
    chart_types: list[str] = []
    series_out: list[dict[str, Any]] = []
    if plot_area is not None:
        for plot in list(plot_area):
            plot_type = local_name(plot.tag)
            if not plot_type.endswith("Chart"):
                continue
            chart_types.append(plot_type)
            grouping = attr_val(plot.find("./c:grouping", NS))
            axis_ids = [x.get("val") for x in plot.findall("./c:axId", NS) if x.get("val")]
            for fallback_order, series in enumerate(plot.findall("./c:ser", NS)):
                data = {role: classic_data_node(series, role) for role in CLASSIC_SERIES_ROLES}
                order = parse_number(attr_val(series.find("./c:order", NS)))
                series_out.append({
                    "series_index": parse_number(attr_val(series.find("./c:idx", NS))),
                    "series_order": fallback_order if order is None else order,
                    "name": series_name_classic(series),
                    "name_formula": series_name_formula_classic(series),
                    "chart_type": plot_type,
                    "grouping": grouping,
                    "axis_ids": axis_ids,
                    "formulas": {role: item["formula"] for role, item in data.items() if item["formula"]},
                    "number_format": data["val"]["format_code"] or data["yVal"]["format_code"],
                    "visual": series_visual(series),
                    "points": build_points(data),
                })
    return {
        "schema": "drawingml-chart",
        "title": chart_title_classic(root),
        "chart_types": list(dict.fromkeys(chart_types)),
        "axes": parse_classic_axes(root),
        "series": series_out,
    }


def chartex_values(level: ET.Element, numeric: bool) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for child in list(level):
        if local_name(child.tag) not in {"v", "pt"}:
            continue
        try:
            idx = int(child.get("idx", str(len(result))))
        except ValueError:
            idx = len(result)
        result[idx] = parse_number(text_of(child)) if numeric else text_of(child)
    return result


def parse_chartex_chart(root: ET.Element) -> dict[str, Any]:
    datasets: dict[str, dict[str, Any]] = {}
    for data in root.findall("./cx:chartData/cx:data", NS):
        data_id = data.get("id")
        if not data_id:
            continue
        dims: list[dict[str, Any]] = []
        for dim in list(data):
            dim_name = local_name(dim.tag)
            if dim_name not in {"numDim", "strDim"}:
                continue
            numeric = dim_name == "numDim"
            levels = [chartex_values(level, numeric) for level in dim.findall("./cx:lvl", NS)]
            dims.append({
                "kind": "numeric" if numeric else "string",
                "dimension_type": dim.get("type"),
                "formula": text_of(dim.find("./cx:f", NS)),
                "name_formula": text_of(dim.find("./cx:nf", NS)),
                "levels": levels,
            })
        datasets[data_id] = {"dimensions": dims}

    series_out: list[dict[str, Any]] = []
    chart_types: list[str] = []
    for index, series in enumerate(root.findall("./cx:chart/cx:plotArea/cx:plotAreaRegion/cx:series", NS)):
        layout = series.get("layoutId", "unknown")
        chart_types.append(layout)
        data_id_node = series.find("./cx:dataId", NS)
        data_id = data_id_node.get("val") if data_id_node is not None else None
        dataset = datasets.get(data_id or "", {"dimensions": []})
        dims = dataset["dimensions"]
        count = max((max(level.keys(), default=-1) for d in dims for level in d["levels"]), default=-1) + 1
        points: list[dict[str, Any]] = []
        for point_idx in range(count):
            point: dict[str, Any] = {
                "point_index": point_idx,
                "source": "chartex_cache_or_literal",
                "dimensions": {},
            }
            for dim_index, dim in enumerate(dims):
                values = [level.get(point_idx) for level in dim["levels"]]
                key = dim.get("dimension_type") or ("value" if dim["kind"] == "numeric" else "category")
                key = re.sub(r"[^A-Za-z0-9_]+", "_", key).strip("_") or f"dimension_{dim_index}"
                value = values[0] if len(values) == 1 else values
                point["dimensions"][key] = value
                canonical_key = {
                    "cat": "category", "category": "category", "val": "value", "value": "value",
                    "x": "x", "xVal": "x", "y": "y", "yVal": "y", "size": "size",
                    "bubbleSize": "size",
                }.get(key)
                if canonical_key:
                    point[canonical_key] = value
            points.append(point)
        title = all_text(series.find("./cx:tx", NS))
        series_out.append({
            "series_index": index,
            "series_order": index,
            "name": title,
            "chart_type": layout,
            "axis_ids": [x.get("val") or text_of(x) for x in series.findall("./cx:axisId", NS)
                         if x.get("val") or text_of(x)],
            "data_id": data_id,
            "formulas": {f"dimension_{i}": d["formula"] for i, d in enumerate(dims) if d["formula"]},
            "dimensions": [{k: v for k, v in d.items() if k != "levels"} for d in dims],
            "points": points,
        })

    axes = []
    for axis in root.findall("./cx:chart/cx:plotArea/cx:axis", NS):
        val_scaling = axis.find("./cx:valScaling", NS)
        axes.append({
            "axis_type": "value" if val_scaling is not None else "category",
            "axis_id": axis.get("id"),
            "title": all_text(axis.find("./cx:title", NS)),
            "minimum": parse_number(attr_val(val_scaling.find("./cx:min", NS)) if val_scaling is not None else None),
            "maximum": parse_number(attr_val(val_scaling.find("./cx:max", NS)) if val_scaling is not None else None),
        })
    return {
        "schema": "drawingml-chartex-2014",
        "title": all_text(root.find("./cx:chart/cx:title", NS)),
        "chart_types": list(dict.fromkeys(chart_types)),
        "axes": axes,
        "series": series_out,
    }


def excel_col_to_index(col: str) -> int:
    value = 0
    for char in col.upper():
        value = value * 26 + ord(char) - 64
    return value


def excel_ref(cell_ref: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", cell_ref)
    if not match:
        raise ValueError(cell_ref)
    return int(match.group(2)), excel_col_to_index(match.group(1))


def parse_formula_range(formula: str) -> tuple[str, tuple[int, int], tuple[int, int]] | None:
    value = formula.strip().lstrip("=")
    if "!" not in value:
        return None
    sheet, cells = value.rsplit("!", 1)
    sheet = re.sub(r"^\[[^]]+\]", "", sheet)
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    start, _, end = cells.partition(":")
    end = end or start
    try:
        return sheet, excel_ref(start), excel_ref(end)
    except ValueError:
        return None


class WorkbookReader:
    def __init__(self, data: bytes, limits: Limits):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        validate_zip(self.archive, limits)
        self.shared_strings = self._shared_strings()
        self.date1904 = False
        self.sheets: dict[str, str] = {}
        self.cells: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}
        self._load_workbook()

    def _shared_strings(self) -> list[str]:
        try:
            root = parse_xml(self.archive.read("xl/sharedStrings.xml"), "xl/sharedStrings.xml")
        except KeyError:
            return []
        return ["".join(t.text or "" for t in si.findall(".//s:t", NS)) for si in root.findall("./s:si", NS)]

    def _load_workbook(self) -> None:
        try:
            root = parse_xml(self.archive.read("xl/workbook.xml"), "xl/workbook.xml")
        except KeyError:
            return
        workbook_pr = root.find("./s:workbookPr", NS)
        self.date1904 = workbook_pr is not None and workbook_pr.get("date1904") in {"1", "true"}
        rels = relationships(self.archive, "xl/workbook.xml")
        for sheet in root.findall("./s:sheets/s:sheet", NS):
            name, rid = sheet.get("name"), sheet.get(RID)
            rel = rels.get(rid or "")
            if name and rel and rel["target_mode"] == "Internal":
                self.sheets[name] = rel["target"]
                self.cells[name] = self._load_sheet(rel["target"])

    def _load_sheet(self, part: str) -> dict[tuple[int, int], dict[str, Any]]:
        try:
            root = parse_xml(self.archive.read(part), part)
        except KeyError:
            return {}
        result: dict[tuple[int, int], dict[str, Any]] = {}
        for cell in root.findall(".//s:c", NS):
            ref = cell.get("r")
            if not ref:
                continue
            try:
                coord = excel_ref(ref)
            except ValueError:
                continue
            cell_type = cell.get("t", "n")
            raw = text_of(cell.find("./s:v", NS))
            if cell_type == "s" and isinstance(parse_number(raw), int):
                index = int(raw or "0")
                value = self.shared_strings[index] if 0 <= index < len(self.shared_strings) else raw
            elif cell_type == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
            elif cell_type == "b":
                value = raw == "1"
            elif cell_type in {"str", "e"}:
                value = raw
            else:
                value = parse_number(raw)
            result[coord] = {
                "value": value,
                "raw": raw,
                "formula": text_of(cell.find("./s:f", NS)),
                "style_index": parse_number(cell.get("s")),
            }
        return result

    def range_values(self, formula: str) -> list[Any] | None:
        parsed = parse_formula_range(formula)
        if not parsed:
            return None
        sheet, (r1, c1), (r2, c2) = parsed
        cells = self.cells.get(sheet)
        if cells is None:
            return None
        values: list[Any] = []
        for row in range(min(r1, r2), max(r1, r2) + 1):
            for col in range(min(c1, c2), max(c1, c2) + 1):
                values.append(cells.get((row, col), {}).get("value"))
        return values


def embedded_workbook(archive: zipfile.ZipFile, chart_part: str, limits: Limits) -> tuple[dict[str, Any] | None, WorkbookReader | None, bytes | None]:
    rels = relationships(archive, chart_part)
    external = []
    internal = []
    for rid, rel in rels.items():
        record = {"relationship_id": rid, **rel}
        if rel["target_mode"] == "External":
            external.append(record)
        elif rel["type"].endswith(EMBEDDED_REL_SUFFIXES) or rel["target"].lower().endswith((".xlsx", ".xlsm")):
            internal.append(record)
    info = {"internal_packages": internal, "external_links": external}
    for record in internal:
        target = record["target"]
        try:
            data = archive.read(target)
            return info, WorkbookReader(data, limits), data
        except (KeyError, zipfile.BadZipFile, ExtractionError):
            continue
    return info if internal or external else None, None, None


def enrich_classic_from_workbook(chart: dict[str, Any], workbook: WorkbookReader | None) -> None:
    if workbook is None:
        return
    for series in chart.get("series", []):
        if series.get("name_formula"):
            names = workbook.range_values(series["name_formula"])
            if names:
                series["name_cache"] = series.get("name")
                series["name"] = names[0]
        formulas = series.get("formulas", {})
        resolved = {role: workbook.range_values(formula) for role, formula in formulas.items()}
        if not any(value is not None for value in resolved.values()):
            continue
        max_len = max((len(value) for value in resolved.values() if value is not None), default=0)
        while len(series["points"]) < max_len:
            series["points"].append({"point_index": len(series["points"])})
        for idx, point in enumerate(series["points"]):
            mappings = {"cat": "category", "val": "value", "xVal": "x", "yVal": "y", "bubbleSize": "size"}
            used = False
            for role, key in mappings.items():
                values = resolved.get(role)
                if values is not None and idx < len(values):
                    point[f"{key}_cache"] = point.get(key)
                    point[key] = values[idx]
                    used = True
            if used:
                point["source"] = "embedded_workbook"


def flatten_point(chart: dict[str, Any], series: dict[str, Any], point: dict[str, Any]) -> dict[str, Any]:
    known = {"point_index", "category", "category_levels", "value", "x", "y", "size", "source"}
    extra = {k: v for k, v in point.items() if k not in known and not k.endswith("_cache")}
    return {
        "slide_number": chart["slide_number"],
        "chart_id": chart["chart_id"],
        "chart_title": chart.get("title"),
        "chart_type": series.get("chart_type"),
        "series_order": series.get("series_order"),
        "series_name": series.get("name"),
        "point_index": point.get("point_index"),
        "category": point.get("category"),
        "category_levels": json.dumps(point.get("category_levels"), ensure_ascii=False) if point.get("category_levels") is not None else None,
        "x": point.get("x"),
        "y": point.get("y"),
        "value": point.get("value"),
        "size": point.get("size"),
        "number_format": series.get("number_format"),
        "source": point.get("source"),
        "extra_dimensions": json.dumps(extra, ensure_ascii=False) if extra else None,
    }


def convert_legacy_ppt(input_path: Path, soffice: str, timeout: int) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temp = tempfile.TemporaryDirectory(prefix="ppt-chart-convert-")
    profile = Path(temp.name) / "lo-profile"
    output = Path(temp.name) / "output"
    profile.mkdir()
    output.mkdir()
    command = [
        soffice, "--headless", f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to", "pptx", "--outdir", str(output), str(input_path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        temp.cleanup()
        raise ExtractionError(f"LibreOffice conversion failed: {exc}") from exc
    candidates = sorted(output.glob("*.pptx"))
    if proc.returncode != 0 or not candidates:
        detail = (proc.stderr or proc.stdout).strip()
        temp.cleanup()
        raise ExtractionError(f"LibreOffice did not produce PPTX: {detail}")
    return candidates[0], temp


def input_kind(path: Path) -> str:
    with path.open("rb") as handle:
        magic = handle.read(8)
    if magic.startswith(b"PK\x03\x04"):
        return "ooxml"
    if magic == bytes.fromhex("D0CF11E0A1B11AE1"):
        return "ole-ppt"
    raise ExtractionError("Input is neither an OOXML package nor a legacy OLE PowerPoint file")


def extract(input_path: Path, output_dir: Path, limits: Limits, extract_workbooks: bool,
            soffice: str, conversion_timeout: int) -> dict[str, Any]:
    original_path = input_path.resolve()
    if not original_path.is_file():
        raise ExtractionError(f"Input file does not exist: {input_path}")
    kind = input_kind(original_path)
    converted_temp: tempfile.TemporaryDirectory[str] | None = None
    parse_path = original_path
    if kind == "ole-ppt":
        parse_path, converted_temp = convert_legacy_ppt(original_path, soffice, conversion_timeout)

    try:
        with zipfile.ZipFile(parse_path) as archive:
            validate_zip(archive, limits)
            slides_out: list[dict[str, Any]] = []
            charts_out: list[dict[str, Any]] = []
            global_warnings: list[dict[str, Any]] = []
            if "ppt/vbaProject.bin" in archive.namelist():
                global_warnings.append({
                    "code": "macro_project_present",
                    "message": "The macro project was not executed or extracted; chart parts were parsed read-only.",
                })
            for slide_number, slide_part in presentation_slides(archive):
                root = parse_xml(archive.read(slide_part), slide_part)
                rels = relationships(archive, slide_part)
                slide = {
                    "slide_number": slide_number,
                    "slide_part": slide_part,
                    "title": slide_title(root),
                    "charts": [],
                    "visual_fallback_candidates": picture_candidates(root),
                }
                for chart_index, ref in enumerate(chart_refs_on_slide(root), start=1):
                    rel = rels.get(ref["rid"])
                    if not rel or rel["target_mode"] != "Internal" or not rel["type"].endswith(CHART_REL_SUFFIXES):
                        global_warnings.append({"code": "unresolved_chart_relationship", "slide_number": slide_number,
                                                "relationship_id": ref["rid"]})
                        continue
                    chart_part = rel["target"]
                    try:
                        chart_root = parse_xml(archive.read(chart_part), chart_part)
                    except KeyError:
                        global_warnings.append({"code": "missing_chart_part", "slide_number": slide_number,
                                                "chart_part": chart_part})
                        continue
                    if namespace(chart_root.tag) == NS["cx"]:
                        chart = parse_chartex_chart(chart_root)
                    else:
                        chart = parse_classic_chart(chart_root)
                    chart_id = f"slide-{slide_number:03d}-chart-{chart_index:02d}"
                    wb_info, workbook, workbook_bytes = embedded_workbook(archive, chart_part, limits)
                    if chart["schema"] == "drawingml-chart":
                        enrich_classic_from_workbook(chart, workbook)
                    chart.update({
                        "chart_id": chart_id,
                        "slide_number": slide_number,
                        "chart_index": chart_index,
                        "chart_part": chart_part,
                        "bbox_emu": ref["bbox_emu"],
                        "data_source": "embedded_workbook" if workbook else "chart_cache_or_literal",
                        "workbook": wb_info,
                        "confidence": 0.99 if workbook else (0.95 if chart.get("series") else 0.4),
                        "warnings": [],
                    })
                    if wb_info and wb_info.get("external_links") and not workbook:
                        chart["warnings"].append({"code": "external_source_unavailable",
                                                  "message": "External workbooks are never fetched; cached chart data was used."})
                    if not chart.get("series"):
                        chart["warnings"].append({"code": "no_native_series",
                                                  "message": "No native series data was decoded; visual extraction may be required."})
                    if extract_workbooks and workbook_bytes:
                        wb_dir = output_dir / "workbooks"
                        wb_dir.mkdir(parents=True, exist_ok=True)
                        wb_path = wb_dir / f"{chart_id}.xlsx"
                        wb_path.write_bytes(workbook_bytes)
                        chart["workbook"]["extracted_file"] = str(wb_path.resolve())
                    slide["charts"].append(chart_id)
                    charts_out.append(chart)
                slides_out.append(slide)

            native_count = len(charts_out)
            picture_count = sum(len(slide["visual_fallback_candidates"]) for slide in slides_out)
            return {
                "schema_version": "1.0.0",
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "input": {
                    "path": str(original_path),
                    "filename": original_path.name,
                    "sha256": sha256_file(original_path),
                    "size_bytes": original_path.stat().st_size,
                    "kind": kind,
                    "converted_for_parsing": kind == "ole-ppt",
                },
                "parser": {"name": "extract-ppt-chart-data", "version": "1.0.0", "engine": "stdlib-ooxml"},
                "summary": {
                    "slide_count": len(slides_out),
                    "native_chart_count": native_count,
                    "visual_fallback_candidate_count": picture_count,
                    "status": "complete" if native_count and not global_warnings else "partial" if global_warnings or picture_count else "complete",
                },
                "slides": slides_out,
                "charts": charts_out,
                "warnings": global_warnings,
            }
    finally:
        if converted_temp is not None:
            converted_temp.cleanup()


def write_outputs(result: dict[str, Any], output_dir: Path, overwrite: bool, pretty: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / "charts.json", output_dir / "charts.csv", output_dir / "chart-points.csv"]
    if not overwrite:
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            raise ExtractionError("Output files already exist; use --overwrite: " + ", ".join(existing))
    json_text = json.dumps(result, ensure_ascii=False, indent=2 if pretty else None, allow_nan=False)
    targets[0].write_text(json_text + "\n", encoding="utf-8")

    chart_fields = ["slide_number", "chart_id", "chart_title", "chart_types", "schema", "series_count",
                    "data_source", "confidence", "chart_part", "bbox_emu", "warning_codes"]
    with targets[1].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=chart_fields)
        writer.writeheader()
        for chart in result["charts"]:
            writer.writerow({
                "slide_number": chart["slide_number"], "chart_id": chart["chart_id"],
                "chart_title": chart.get("title"), "chart_types": "|".join(chart.get("chart_types", [])),
                "schema": chart.get("schema"), "series_count": len(chart.get("series", [])),
                "data_source": chart.get("data_source"), "confidence": chart.get("confidence"),
                "chart_part": chart.get("chart_part"),
                "bbox_emu": json.dumps(chart.get("bbox_emu"), ensure_ascii=False),
                "warning_codes": "|".join(x.get("code", "") for x in chart.get("warnings", [])),
            })

    point_fields = ["slide_number", "chart_id", "chart_title", "chart_type", "series_order", "series_name",
                    "point_index", "category", "category_levels", "x", "y", "value", "size", "number_format",
                    "source", "extra_dimensions"]
    with targets[2].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=point_fields)
        writer.writeheader()
        for chart in result["charts"]:
            for series in chart.get("series", []):
                for point in series.get("points", []):
                    writer.writerow(flatten_point(chart, series, point))
    return targets


def find_soffice(value: str | None) -> str:
    if value:
        return value
    return shutil.which("soffice") or shutil.which("libreoffice") or "soffice"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input .ppt, .pptx, .pptm, .pps, or .ppsx file")
    parser.add_argument("--output-dir", "-o", required=True, type=Path, help="Directory for JSON/CSV output")
    parser.add_argument("--extract-workbooks", action="store_true", help="Write embedded XLSX packages to workbooks/")
    parser.add_argument("--overwrite", action="store_true", help="Replace charts.json/charts.csv/chart-points.csv")
    parser.add_argument("--compact", action="store_true", help="Write compact rather than indented JSON")
    parser.add_argument("--soffice", help="LibreOffice/soffice executable for legacy .ppt conversion")
    parser.add_argument("--conversion-timeout", type=int, default=120)
    parser.add_argument("--max-entries", type=int, default=Limits.max_entries)
    parser.add_argument("--max-entry-mib", type=int, default=Limits.max_entry_bytes // (1024 * 1024))
    parser.add_argument("--max-total-mib", type=int, default=Limits.max_total_bytes // (1024 * 1024))
    parser.add_argument("--max-compression-ratio", type=float, default=Limits.max_ratio)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limits = Limits(max_entries=args.max_entries, max_entry_bytes=args.max_entry_mib * 1024 * 1024,
                    max_total_bytes=args.max_total_mib * 1024 * 1024, max_ratio=args.max_compression_ratio)
    try:
        result = extract(args.input, args.output_dir, limits, args.extract_workbooks,
                         find_soffice(args.soffice), args.conversion_timeout)
        targets = write_outputs(result, args.output_dir, args.overwrite, pretty=not args.compact)
    except (ExtractionError, zipfile.BadZipFile, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": result["summary"]["status"], "summary": result["summary"],
                      "files": [str(path.resolve()) for path in targets]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
