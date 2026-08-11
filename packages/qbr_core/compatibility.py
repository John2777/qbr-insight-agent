"""Backward-compatible aliases for qbr_core's former flat module layout."""

from __future__ import annotations

import sys
from importlib import import_module

LEGACY_MODULE_ALIASES = {
    "answering": "analysis.answering",
    "calculations": "analysis.calculations",
    "chart_analysis": "analysis.charts.analyzer",
    "chart_reporting": "analysis.charts.reporting",
    "chart_semantics": "analysis.charts.semantics",
    "chart_structure": "analysis.charts.structure",
    "reasoning": "analysis.table_reasoning",
    "verification": "analysis.verification",
    "auth": "security.authentication",
    "config": "foundation.config",
    "db": "foundation.database",
    "errors": "foundation.errors",
    "ids": "foundation.identifiers",
    "lease": "foundation.leases",
    "observability": "foundation.observability",
    "service_support": "foundation.serialization",
    "parser": "documents.parser",
    "vision": "documents.vision",
    "conversation_context": "conversations.context",
    "conversation_summary": "conversations.summary",
    "query_builder": "planning.builder",
    "query_models": "planning.models",
    "query_planning": "planning",
    "planner_agent": "planning.agent",
    "language_rules": "planning.language",
    "terminology": "planning.terminology",
    "evidence": "retrieval.evidence",
    "coverage": "retrieval.coverage",
    "rerank": "retrieval.reranking",
    "vector": "retrieval.vector_store",
    "llm": "providers.llm",
    "skill_registry": "skills.registry",
    "service": "application.service",
    "qa_service": "application.qa_service",
    "qa_runtime": "application.runtime",
    "service_component": "application.component",
    "service_ingestion": "application.ingestion",
    "service_persistence": "application.persistence",
    "service_resources": "application.resources",
    "purge": "application.purge",
    "run_warnings": "application.warnings",
}


def install_legacy_module_aliases(package_name: str) -> None:
    """Register legacy import paths without retaining flat wrapper modules."""
    package = sys.modules[package_name]
    for legacy_name, canonical_name in LEGACY_MODULE_ALIASES.items():
        module = import_module(f"{package_name}.{canonical_name}")
        sys.modules[f"{package_name}.{legacy_name}"] = module
        setattr(package, legacy_name, module)
