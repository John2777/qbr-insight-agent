"""Strongly typed contracts crossing the answer-run application boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Evidence:
    """Represent one validated evidence atom used to persist a citation."""

    document_version_id: str
    slide_id: str
    quote: str
    confidence: float
    source_kind: str
    element_id: str | None = None
    chunk_id: str | None = None
    bbox: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Evidence:
        """Validate and convert a loose pipeline mapping into evidence."""
        document_version_id = str(value.get("document_version_id") or "")
        slide_id = str(value.get("slide_id") or "")
        quote = str(value.get("quote") or "")
        if not document_version_id or not slide_id or not quote:
            raise ValueError("Evidence requires document_version_id, slide_id, and quote")
        known = {
            "document_version_id",
            "slide_id",
            "quote",
            "confidence",
            "source_kind",
            "element_id",
            "chunk_id",
            "bbox",
        }
        return cls(
            document_version_id=document_version_id,
            slide_id=slide_id,
            quote=quote,
            confidence=max(0.0, min(float(value.get("confidence", 0.0)), 1.0)),
            source_kind=str(value.get("source_kind") or "unknown"),
            element_id=str(value["element_id"]) if value.get("element_id") else None,
            chunk_id=str(value["chunk_id"]) if value.get("chunk_id") else None,
            bbox=dict(value.get("bbox") or {}),
            attributes={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence in its compatible mapping representation."""
        return {
            **self.attributes,
            "document_version_id": self.document_version_id,
            "slide_id": self.slide_id,
            "element_id": self.element_id,
            "chunk_id": self.chunk_id,
            "quote": self.quote,
            "bbox": dict(self.bbox),
            "confidence": self.confidence,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class Citation:
    """Represent one enriched citation exposed by the public read model."""

    citation_id: str
    claim_no: int
    slide_id: str
    quote: str
    source_kind: str
    label: str
    preview_url: str
    attributes: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Citation:
        """Convert an enriched database mapping into a public citation."""
        citation_id = str(value.get("id") or "")
        slide_id = str(value.get("slide_id") or "")
        if not citation_id or not slide_id:
            raise ValueError("Citation requires id and slide_id")
        known = {"id", "claim_no", "slide_id", "quote", "source_kind", "label", "preview_url"}
        claim_no = int(value.get("claim_no") or 0)
        return cls(
            citation_id=citation_id,
            claim_no=claim_no,
            slide_id=slide_id,
            quote=str(value.get("quote") or ""),
            source_kind=str(value.get("source_kind") or "unknown"),
            label=str(value.get("label") or f"[{claim_no}]"),
            preview_url=str(value.get("preview_url") or f"/api/v1/slides/{slide_id}/preview"),
            attributes={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the citation in its public API representation."""
        return {
            **self.attributes,
            "id": self.citation_id,
            "claim_no": self.claim_no,
            "slide_id": self.slide_id,
            "quote": self.quote,
            "source_kind": self.source_kind,
            "label": self.label,
            "preview_url": self.preview_url,
        }


@dataclass(frozen=True, slots=True)
class RunMetadata:
    """Represent auditable metadata produced by one answer pipeline run."""

    show_visuals: bool
    query_plan: dict[str, Any]
    answer_routing: dict[str, Any]
    retrieval: dict[str, Any]
    evidence_pack: dict[str, Any]
    verification: dict[str, Any]
    conversation_context: dict[str, Any]
    calculation: dict[str, Any] = field(default_factory=dict)
    pipeline_version: str = "delivery-requirement-contract-v4"

    def to_dict(self) -> dict[str, Any]:
        """Return metadata in the persisted message schema."""
        return {
            "show_visuals": self.show_visuals,
            "knowledge_source": "document_evidence",
            "pipeline_version": self.pipeline_version,
            "query_plan": dict(self.query_plan),
            "answer_routing": dict(self.answer_routing),
            "retrieval": dict(self.retrieval),
            "evidence_pack": dict(self.evidence_pack),
            **self.calculation,
            "verification": dict(self.verification),
            "conversation_context": dict(self.conversation_context),
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    """Represent the complete typed output committed by a run repository."""

    answer: str
    evidence: tuple[Evidence, ...]
    warnings: tuple[str, ...]
    model: dict[str, Any]
    metadata: RunMetadata

    @classmethod
    def create(
        cls,
        *,
        answer: str,
        evidence: list[Mapping[str, Any]],
        warnings: list[str],
        model: Mapping[str, Any],
        metadata: RunMetadata,
    ) -> RunResult:
        """Validate loose pipeline output and create an immutable run result."""
        if not answer.strip():
            raise ValueError("RunResult answer must not be empty")
        return cls(
            answer=answer,
            evidence=tuple(Evidence.from_mapping(item) for item in evidence),
            warnings=tuple(dict.fromkeys(str(item) for item in warnings)),
            model=dict(model),
            metadata=metadata,
        )
