from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps

from packages.qbr_core.security.archive import OOXML_MIME, inspect_pptx
from packages.qbr_core.skills.registry import (
    NATIVE_CHART_CAPABILITY,
    PARSER_SKILL_KIND,
    SkillDescriptor,
    SkillRegistry,
    default_skill_paths,
)

PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True, slots=True)
class ParsedElement:
    """Represent one parser-preserved slide element and its source geometry."""
    element_type: str
    reading_order: int
    bbox: dict[str, float]
    text: str | None
    structured: dict[str, Any]
    provenance: dict[str, Any]
    confidence: float


@dataclass(frozen=True, slots=True)
class ParsedSlide:
    """Group parsed elements, notes, and provenance for one slide."""
    slide_no: int
    title: str | None
    notes: str | None
    width_emu: int
    height_emu: int
    elements: list[ParsedElement]


@dataclass(frozen=True, slots=True)
class ParsedPresentation:
    """Carry a complete native presentation parse and its content hash."""
    sha256: str
    slides: list[ParsedSlide]
    charts: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    status: str
    canonical: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relationships(archive: zipfile.ZipFile, source: str) -> dict[str, str]:
    parent, name = source.rsplit("/", 1)
    rel_path = f"{parent}/_rels/{name}.rels"
    if rel_path not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rel_path))
    result: dict[str, str] = {}
    for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
        target = rel.attrib.get("Target", "")
        if rel.attrib.get("TargetMode") == "External":
            continue
        base = Path(parent)
        result[rel.attrib.get("Id", "")] = str((base / target).as_posix())
        # Pure path normalization without allowing filesystem traversal.
        parts: list[str] = []
        for part in result[rel.attrib.get("Id", "")].split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        result[rel.attrib.get("Id", "")] = "/".join(parts)
    return result


def _presentation_slides(archive: zipfile.ZipFile) -> tuple[list[str], int, int]:
    root = ET.fromstring(archive.read("ppt/presentation.xml"))
    rels = _relationships(archive, "ppt/presentation.xml")
    parts: list[str] = []
    slide_list = root.find(f"{{{PML}}}sldIdLst")
    if slide_list is not None:
        for item in list(slide_list):
            target = rels.get(item.attrib.get(f"{{{REL}}}id", ""))
            if target:
                parts.append(target)
    size = root.find(f"{{{PML}}}sldSz")
    width = int(size.attrib.get("cx", "12192000")) if size is not None else 12192000
    height = int(size.attrib.get("cy", "6858000")) if size is not None else 6858000
    return parts, width, height


def _shape_bbox(shape: ET.Element, width: int, height: int) -> dict[str, float]:
    xfrm = shape.find(f".//{{{PML}}}xfrm") or shape.find(f".//{{{DML}}}xfrm")
    if xfrm is None:
        return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    off = xfrm.find(f"{{{DML}}}off")
    ext = xfrm.find(f"{{{DML}}}ext")
    if off is None or ext is None:
        return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    return {
        "x": round(int(off.attrib.get("x", "0")) / width, 6),
        "y": round(int(off.attrib.get("y", "0")) / height, 6),
        "w": round(int(ext.attrib.get("cx", "0")) / width, 6),
        "h": round(int(ext.attrib.get("cy", "0")) / height, 6),
    }


def _text(node: ET.Element) -> str | None:
    values = [item.text or "" for item in node.findall(f".//{{{DML}}}t")]
    value = "\n".join(part.strip() for part in values if part.strip()).strip()
    return value or None


def _table(node: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in node.findall(f".//{{{DML}}}tr"):
        rows.append([_text(cell) or "" for cell in row.findall(f"{{{DML}}}tc")])
    return rows


def _notes(archive: zipfile.ZipFile, slide_part: str) -> str | None:
    rels = _relationships(archive, slide_part)
    target = next((path for path in rels.values() if "/notesSlides/" in f"/{path}"), None)
    if not target or target not in archive.namelist():
        return None
    return _text(ET.fromstring(archive.read(target)))


def extract_native_content(path: Path) -> tuple[list[ParsedSlide], str]:
    with zipfile.ZipFile(path) as archive:
        slide_parts, width, height = _presentation_slides(archive)
        result: list[ParsedSlide] = []
        for slide_no, part in enumerate(slide_parts, start=1):
            root = ET.fromstring(archive.read(part))
            elements: list[ParsedElement] = []
            title: str | None = None
            order = 0
            tree = root.find(f".//{{{PML}}}spTree")
            for shape in list(tree) if tree is not None else []:
                tag = shape.tag.rsplit("}", 1)[-1]
                if tag in {"nvGrpSpPr", "grpSpPr"}:
                    continue
                text = _text(shape)
                table = _table(shape)
                element_type = "table" if table else "text"
                if tag == "pic":
                    element_type = "image"
                if shape.find(f".//{{{DML}}}graphicData") is not None and not table:
                    uri = shape.find(f".//{{{DML}}}graphicData").attrib.get("uri", "")
                    if "chart" in uri:
                        element_type = "chart"
                if element_type == "text" and not text:
                    continue
                if title is None and shape.find(f".//{{{PML}}}ph") is not None:
                    placeholder = shape.find(f".//{{{PML}}}ph")
                    if placeholder is not None and placeholder.attrib.get("type", "title") in {"title", "ctrTitle"}:
                        title = text
                order += 1
                elements.append(
                    ParsedElement(
                        element_type=element_type,
                        reading_order=order,
                        bbox=_shape_bbox(shape, width, height),
                        text=text,
                        structured={"rows": table} if table else {},
                        provenance={"source": "native_ooxml", "part": part},
                        confidence=1.0 if element_type in {"text", "table"} else 0.95,
                    )
                )
            result.append(ParsedSlide(slide_no, title, _notes(archive, part), width, height, elements))
    digest = _sha256_file(path)
    return result, digest


def parse_presentation(
    path: Path,
    output_dir: Path,
    *,
    max_slides: int = 200,
    skill_registry: SkillRegistry | None = None,
    parser_skill: SkillDescriptor | None = None,
) -> ParsedPresentation:
    inspect_pptx(path, max_slides=max_slides)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry = skill_registry or SkillRegistry(default_skill_paths())
    descriptor = parser_skill or registry.resolve(
        kind=PARSER_SKILL_KIND,
        capability=NATIVE_CHART_CAPABILITY,
        accepts=OOXML_MIME,
    )
    module = registry.load(descriptor).module
    chart_result = module.extract(
        path,
        output_dir,
        module.Limits(),
        True,
        module.find_soffice(None),
        120,
    )
    registry.validate_output(descriptor, chart_result)
    module.write_outputs(chart_result, output_dir, overwrite=True, pretty=True)
    slides, digest = extract_native_content(path)
    warnings = list(chart_result.get("warnings", []))
    status = chart_result["summary"]["status"]
    return ParsedPresentation(digest, slides, chart_result["charts"], warnings, status, chart_result)


PREVIEW_MAX_SIZE = (1920, 1080)
THUMBNAIL_SIZE = (320, 180)


def thumbnail_path_for(preview_path: Path) -> Path:
    return preview_path.with_name(f"{preview_path.stem}-thumbnail.webp")


def _write_webp_variants(source_path: Path, preview_path: Path) -> tuple[Path, Path]:
    thumbnail_path = thumbnail_path_for(preview_path)
    with Image.open(source_path) as source:
        preview = source.convert("RGB")
    try:
        preview.thumbnail(PREVIEW_MAX_SIZE, Image.Resampling.LANCZOS, reducing_gap=3.0)
        preview.save(preview_path, format="WEBP", quality=84, method=4)

        thumbnail_content = ImageOps.contain(preview, THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        try:
            with Image.new("RGB", THUMBNAIL_SIZE, "white") as thumbnail:
                left = (THUMBNAIL_SIZE[0] - thumbnail_content.width) // 2
                top = (THUMBNAIL_SIZE[1] - thumbnail_content.height) // 2
                thumbnail.paste(thumbnail_content, (left, top))
                thumbnail.save(thumbnail_path, format="WEBP", quality=76, method=4)
        finally:
            thumbnail_content.close()
    finally:
        preview.close()
    return preview_path, thumbnail_path


def _rendered_page_number(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-", 1)[-1])
    except ValueError:
        return 0


def render_slides(path: Path, output_dir: Path, slides: list[ParsedSlide]) -> tuple[list[Path], list[str]]:
    render_dir = output_dir / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    if soffice and pdftoppm:
        with tempfile.TemporaryDirectory(prefix="qbr-render-") as temp:
            profile = Path(temp) / "libreoffice-profile"
            profile.mkdir()
            proc = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    temp,
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            pdf = Path(temp) / f"{path.stem}.pdf"
            if proc.returncode == 0 and pdf.exists():
                prefix = Path(temp) / "slide"
                rendered = subprocess.run(
                    [pdftoppm, "-png", "-r", "160", str(pdf), str(prefix)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if rendered.returncode == 0:
                    images = sorted(Path(temp).glob("slide-*.png"), key=_rendered_page_number)
                    if len(images) == len(slides):
                        try:
                            previews: list[Path] = []
                            for index, image in enumerate(images, 1):
                                preview_path = render_dir / f"slide-{index:04d}.webp"
                                preview, _ = _write_webp_variants(image, preview_path)
                                previews.append(preview)
                            return previews, warnings
                        except OSError:
                            for generated in render_dir.glob("slide-*.webp"):
                                generated.unlink(missing_ok=True)
            warnings.append("LibreOffice/Poppler rendering failed; generated structural SVG previews.")
    else:
        warnings.append("LibreOffice or Poppler unavailable; generated structural SVG previews.")
    return [_write_svg_preview(slide, render_dir / f"slide-{slide.slide_no:04d}.svg") for slide in slides], warnings


def _write_svg_preview(slide: ParsedSlide, target: Path) -> Path:
    from html import escape

    width, height = 1280, 720
    blocks = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    blocks.append('<rect width="100%" height="100%" fill="#f8fafc"/>')
    for element in slide.elements:
        bbox = element.bbox
        x, y, w, h = bbox["x"] * width, bbox["y"] * height, bbox["w"] * width, bbox["h"] * height
        color = "#2563eb" if element.element_type == "chart" else "#94a3b8"
        blocks.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="white" stroke="{color}"/>')
        label = escape((element.text or element.element_type).replace("\n", " ")[:100])
        blocks.append(f'<text x="{x + 8:.1f}" y="{y + 24:.1f}" font-family="sans-serif" font-size="18" fill="#0f172a">{label}</text>')
    blocks.append("</svg>")
    target.write_text("".join(blocks), encoding="utf-8")
    return target


def canonical_json(parsed: ParsedPresentation) -> str:
    return json.dumps(parsed.canonical, ensure_ascii=False, indent=2, allow_nan=False)
