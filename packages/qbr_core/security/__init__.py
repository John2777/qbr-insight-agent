"""Authentication and safe document archive inspection."""

from .archive import OOXML_MIME, ArchiveInspection, inspect_pptx

__all__ = ["ArchiveInspection", "OOXML_MIME", "inspect_pptx"]
