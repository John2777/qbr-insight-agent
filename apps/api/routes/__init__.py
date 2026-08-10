"""Versioned API route groups."""

from .auth import router as auth_router
from .conversations import router as conversations_router
from .documents import router as documents_router
from .health import router as health_router
from .jobs import router as jobs_router
from .reviews import router as reviews_router

__all__ = [
    "auth_router",
    "conversations_router",
    "documents_router",
    "health_router",
    "jobs_router",
    "reviews_router",
]
