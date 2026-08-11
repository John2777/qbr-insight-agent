from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from packages.qbr_core.foundation.errors import UnsafeArchive, UnsupportedFile
from packages.qbr_core.security.archive import inspect_pptx


def test_rejects_non_ooxml_file(tmp_path: Path) -> None:
    path = tmp_path / "fake.pptx"
    path.write_bytes(b"not a package")
    with pytest.raises(UnsupportedFile):
        inspect_pptx(path)


def test_rejects_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "traversal.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='x'/>")
        archive.writestr("../escape", "bad")
    with pytest.raises(UnsafeArchive, match="Unsafe archive path"):
        inspect_pptx(path)


def test_rejects_external_package_relationship(tmp_path: Path) -> None:
    path = tmp_path / "external.pptx"
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
        Target="https://attacker.invalid/data.xlsx" TargetMode="External"/>
    </Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='x'/>")
        archive.writestr("ppt/slides/slide1.xml", "<p:sld xmlns:p='x'/>")
        archive.writestr("ppt/_rels/presentation.xml.rels", rels)
    with pytest.raises(UnsafeArchive, match="External package relationship"):
        inspect_pptx(path)
