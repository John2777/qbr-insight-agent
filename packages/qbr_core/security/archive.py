from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from packages.qbr_core.foundation.errors import UnsafeArchive, UnsupportedFile

OOXML_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MACRO_PARTS = {"ppt/vbaProject.bin", "ppt/vbaData.xml"}
ACTIVE_SUFFIXES = (".exe", ".dll", ".com", ".bat", ".cmd", ".js", ".vbs", ".ps1")


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    """Report validated OOXML archive size and extracted member metadata."""
    slide_count: int
    expanded_bytes: int
    entry_count: int


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def inspect_pptx(
    path: Path,
    *,
    max_entries: int = 20_000,
    max_entry_bytes: int = 256 * 1024 * 1024,
    max_total_bytes: int = 1024 * 1024 * 1024,
    max_ratio: float = 200.0,
    max_slides: int = 200,
) -> ArchiveInspection:
    try:
        with path.open("rb") as handle:
            if handle.read(4) != b"PK\x03\x04":
                raise UnsupportedFile("Only genuine OOXML .pptx packages are accepted.")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise UnsafeArchive(f"Archive has {len(infos)} entries; limit is {max_entries}.")
            total = 0
            names = set()
            for info in infos:
                if not _safe_name(info.filename):
                    raise UnsafeArchive(f"Unsafe archive path: {info.filename!r}")
                lower = info.filename.lower()
                if lower.endswith(ACTIVE_SUFFIXES):
                    raise UnsafeArchive(f"Executable content is not allowed: {info.filename!r}")
                if info.file_size > max_entry_bytes:
                    raise UnsafeArchive(f"Expanded entry is too large: {info.filename!r}")
                if info.file_size and info.compress_size == 0:
                    raise UnsafeArchive(f"Suspicious compression metadata: {info.filename!r}")
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > max_ratio:
                    raise UnsafeArchive(f"Suspicious compression ratio in {info.filename!r}")
                total += info.file_size
                if total > max_total_bytes:
                    raise UnsafeArchive("Expanded package exceeds the configured limit.")
                names.add(posixpath.normpath(info.filename))
            if "ppt/presentation.xml" not in names:
                raise UnsupportedFile("The package does not contain a PowerPoint presentation part.")
            if names.intersection(MACRO_PARTS):
                raise UnsupportedFile("Macro-enabled PowerPoint packages are not accepted.")
            for name in names:
                if not name.endswith(".rels"):
                    continue
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError as exc:
                    raise UnsafeArchive(f"Invalid relationship XML in {name}") from exc
                for rel in root.findall(f"{{{REL_NS}}}Relationship"):
                    if rel.attrib.get("TargetMode") == "External":
                        rel_type = rel.attrib.get("Type", "")
                        if not rel_type.endswith("/hyperlink"):
                            raise UnsafeArchive(f"External package relationship is blocked: {name}")
            slide_count = sum(
                1
                for name in names
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                and "/_rels/" not in name
            )
            if slide_count < 1:
                raise UnsupportedFile("The presentation has no slides.")
            if slide_count > max_slides:
                raise UnsafeArchive(f"Presentation has {slide_count} slides; limit is {max_slides}.")
            return ArchiveInspection(slide_count, total, len(infos))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFile("The uploaded file is not a valid OOXML ZIP package.") from exc

