"""Framework-independent QBR Insight Agent core public API."""

from .analysis.answering import DeterministicAnswerEngine
from .application.qa_service import QAApplicationService
from .application.service import QBRService
from .compatibility import install_legacy_module_aliases
from .foundation.config import Settings
from .planning import QueryPlan, QueryPlannerAgent

install_legacy_module_aliases(__name__)

__all__ = [
    "DeterministicAnswerEngine",
    "QAApplicationService",
    "QBRService",
    "QueryPlan",
    "QueryPlannerAgent",
    "Settings",
]
