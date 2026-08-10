from __future__ import annotations

from typing import Any


class ServiceComponent:
    """Base for focused services that share the composition root's dependencies."""

    def __init__(self, root: Any) -> None:
        self._root = root

    def __getattr__(self, name: str) -> Any:
        return getattr(self._root, name)
