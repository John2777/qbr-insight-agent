from __future__ import annotations

from typing import Any


class ServiceComponent:
    """Base for focused services that share the composition root's dependencies."""

    def __init__(self, root: Any) -> None:
        """Initialize the service component and its dependencies."""
        self._root = root

    def __getattr__(self, name: str) -> Any:
        """Delegate unresolved attributes to the composed root service."""
        return getattr(self._root, name)
