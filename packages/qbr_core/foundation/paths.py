"""Stable source-tree path helpers used by local runtime integrations."""

from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    """Return the root directory of the qbr_core package."""
    return Path(__file__).resolve().parents[1]


def project_root() -> Path:
    """Return the project root that contains the packages directory."""
    return package_root().parent.parent


def bundled_skill_root() -> Path:
    """Return the repository directory containing bundled runtime skills."""
    return project_root() / "skills"
