from __future__ import annotations

from pathlib import Path

from PIL import Image

from packages.qbr_core.documents.parser import (
    PREVIEW_MAX_SIZE,
    THUMBNAIL_SIZE,
    _rendered_page_number,
    _write_webp_variants,
    thumbnail_path_for,
)


def test_webp_preview_and_thumbnail_have_bounded_dimensions(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    preview_path = tmp_path / "slide-0001.webp"
    with Image.new("RGB", (2934, 1650), "white") as source:
        source.save(source_path, format="PNG")

    preview, thumbnail = _write_webp_variants(source_path, preview_path)

    assert preview == preview_path
    assert thumbnail == thumbnail_path_for(preview_path)
    with Image.open(preview) as preview_image:
        assert preview_image.format == "WEBP"
        assert preview_image.size == PREVIEW_MAX_SIZE
    with Image.open(thumbnail) as thumbnail_image:
        assert thumbnail_image.format == "WEBP"
        assert thumbnail_image.size == THUMBNAIL_SIZE


def test_rendered_pages_sort_numerically() -> None:
    paths = [Path("slide-10.png"), Path("slide-2.png"), Path("slide-1.png")]

    assert [path.name for path in sorted(paths, key=_rendered_page_number)] == [
        "slide-1.png",
        "slide-2.png",
        "slide-10.png",
    ]
