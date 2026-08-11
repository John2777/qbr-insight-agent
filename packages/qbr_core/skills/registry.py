from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from packages.qbr_core.foundation.paths import bundled_skill_root

API_VERSION = "qbr-agent.skills/v1"
PARSER_SKILL_KIND = "ParserSkill"
REASONING_SKILL_KIND = "ReasoningSkill"
NATIVE_CHART_CAPABILITY = "native-chart-data"
STRUCTURED_TABLE_REASONING_CAPABILITY = "structured-table-reasoning"

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SkillRegistryError(RuntimeError):
    """Signal invalid skill discovery, loading, or runtime contracts."""
    pass


def default_skill_paths() -> tuple[Path, ...]:
    return (bundled_skill_root(),)


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """Describe one validated skill manifest and its resolved resources."""
    api_version: str
    kind: str
    name: str
    version: str
    skill_dir: Path
    manifest_path: Path
    entrypoint: Path
    capabilities: frozenset[str]
    accepts: frozenset[str]
    output_schema: Path | None
    schema_version: str
    uses_model: bool
    cost_class: str
    content_hash: str

    @property
    def reference(self) -> str:
        """Return the stable name and version reference for this skill."""
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """Pair a validated descriptor with its imported implementation module."""
    descriptor: SkillDescriptor
    module: ModuleType


class SkillRegistry:
    """Discover trusted manifests eagerly and import their implementations lazily."""

    def __init__(self, skill_paths: tuple[Path, ...] | list[Path]) -> None:
        """Initialize the skill registry and its dependencies."""
        roots = tuple(Path(path).resolve() for path in skill_paths)
        if not roots:
            raise SkillRegistryError("At least one trusted skill root is required")
        self._roots = roots
        self._descriptors: dict[tuple[str, str], SkillDescriptor] = {}
        self._loaded: dict[tuple[str, str], LoadedSkill] = {}
        self._lock = threading.RLock()
        self._discover()

    @property
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        """Return the descriptors for this skill registry."""
        return tuple(sorted(self._descriptors.values(), key=lambda item: (item.name, self._version_key(item.version))))

    def _discover(self) -> None:
        """Discover and validate manifests under every trusted root."""
        manifest_paths: set[Path] = set()
        for root in self._roots:
            if not root.is_dir():
                raise SkillRegistryError(f"Trusted skill root does not exist: {root}")
            direct = root / "manifest.yaml"
            if direct.is_file():
                manifest_paths.add(direct.resolve())
            manifest_paths.update(path.resolve() for path in root.rglob("manifest.yaml") if path.is_file())
        if not manifest_paths:
            raise SkillRegistryError("No manifest.yaml files were found in the trusted skill roots")
        for manifest_path in sorted(manifest_paths):
            descriptor = self._read_manifest(manifest_path)
            key = (descriptor.name, descriptor.version)
            if key in self._descriptors:
                raise SkillRegistryError(f"Duplicate skill registration: {descriptor.reference}")
            self._descriptors[key] = descriptor

    def _read_manifest(self, manifest_path: Path) -> SkillDescriptor:
        """Parse and validate one skill manifest into a descriptor."""
        root = self._trusted_root_for(manifest_path)
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SkillRegistryError(f"Cannot read skill manifest: {manifest_path}") from exc
        if not isinstance(raw, dict):
            raise SkillRegistryError(f"Skill manifest must be an object: {manifest_path}")
        metadata = raw.get("metadata")
        spec = raw.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise SkillRegistryError(f"Skill manifest requires metadata and spec objects: {manifest_path}")

        api_version = str(raw.get("apiVersion") or "")
        kind = str(raw.get("kind") or "")
        name = str(metadata.get("name") or "")
        version = str(metadata.get("version") or "")
        if api_version != API_VERSION:
            raise SkillRegistryError(f"Unsupported skill apiVersion in {manifest_path}: {api_version}")
        if kind not in {PARSER_SKILL_KIND, REASONING_SKILL_KIND}:
            raise SkillRegistryError(f"Unsupported skill kind in {manifest_path}: {kind}")
        if not _NAME_PATTERN.fullmatch(name):
            raise SkillRegistryError(f"Invalid skill name in {manifest_path}: {name}")
        if not _VERSION_PATTERN.fullmatch(version):
            raise SkillRegistryError(f"Invalid semantic version in {manifest_path}: {version}")

        skill_dir = manifest_path.parent.resolve()
        entrypoint = self._resolve_member(skill_dir, spec.get("entrypoint"), "entrypoint", root)
        if entrypoint.suffix != ".py" or not entrypoint.is_file():
            raise SkillRegistryError(f"Skill entrypoint must be an existing Python file: {entrypoint}")

        raw_capabilities = spec.get("capabilities")
        if not isinstance(raw_capabilities, list) or not raw_capabilities:
            raise SkillRegistryError(f"Skill capabilities must be a non-empty list: {manifest_path}")
        capabilities = frozenset(str(value) for value in raw_capabilities)
        if any(not _CAPABILITY_PATTERN.fullmatch(value) for value in capabilities):
            raise SkillRegistryError(f"Invalid capability in {manifest_path}")

        accepts_raw = spec.get("accepts") or {}
        if not isinstance(accepts_raw, dict):
            raise SkillRegistryError(f"Skill accepts must be an object: {manifest_path}")
        accepts_values: list[str] = []
        for field in ("mimeTypes", "contentTypes"):
            values = accepts_raw.get(field) or []
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise SkillRegistryError(f"Skill accepts.{field} must be a string list: {manifest_path}")
            accepts_values.extend(values)

        output_schema: Path | None = None
        schema_version = str(spec.get("schemaVersion") or version)
        schema_value = spec.get("outputSchema")
        if schema_value is not None:
            output_schema = self._resolve_member(skill_dir, schema_value, "outputSchema", root)
            if output_schema.suffix != ".json" or not output_schema.is_file():
                raise SkillRegistryError(f"Skill outputSchema must be an existing JSON file: {output_schema}")
            schema = self._read_schema(output_schema)
            const_version = (((schema.get("properties") or {}).get("schema_version") or {}).get("const"))
            if const_version is not None:
                schema_version = str(const_version)

        uses_model = spec.get("usesModel", False)
        if not isinstance(uses_model, bool):
            raise SkillRegistryError(f"Skill usesModel must be boolean: {manifest_path}")
        cost_class = str(spec.get("costClass") or "local")
        if cost_class not in {"local", "standard", "expensive"}:
            raise SkillRegistryError(f"Invalid skill costClass in {manifest_path}: {cost_class}")

        digest = hashlib.sha256()
        for path in (manifest_path, entrypoint, output_schema):
            if path is not None:
                digest.update(path.read_bytes())
        return SkillDescriptor(
            api_version=api_version,
            kind=kind,
            name=name,
            version=version,
            skill_dir=skill_dir,
            manifest_path=manifest_path,
            entrypoint=entrypoint,
            capabilities=capabilities,
            accepts=frozenset(accepts_values),
            output_schema=output_schema,
            schema_version=schema_version,
            uses_model=uses_model,
            cost_class=cost_class,
            content_hash=digest.hexdigest(),
        )

    def _trusted_root_for(self, path: Path) -> Path:
        """Return the trusted root containing a resolved resource path."""
        resolved = path.resolve()
        for root in self._roots:
            if resolved.is_relative_to(root):
                return root
        raise SkillRegistryError(f"Skill path is outside trusted roots: {path}")

    @staticmethod
    def _resolve_member(skill_dir: Path, value: Any, field: str, trusted_root: Path) -> Path:
        """Resolve a manifest member without allowing path escape."""
        if not isinstance(value, str) or not value.strip():
            raise SkillRegistryError(f"Skill {field} must be a non-empty relative path: {skill_dir}")
        relative = Path(value)
        if relative.is_absolute():
            raise SkillRegistryError(f"Skill {field} cannot be absolute: {value}")
        resolved = (skill_dir / relative).resolve()
        if not resolved.is_relative_to(skill_dir) or not resolved.is_relative_to(trusted_root):
            raise SkillRegistryError(f"Skill {field} escapes its skill directory: {value}")
        return resolved

    @staticmethod
    def _read_schema(path: Path) -> dict[str, Any]:
        """Load an optional JSON schema from the trusted skill directory."""
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillRegistryError(f"Cannot read skill output schema: {path}") from exc
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise SkillRegistryError(f"Skill output schema must describe an object: {path}")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(value, str) for value in required):
            raise SkillRegistryError(f"Skill output schema required must be a string list: {path}")
        return schema

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int, int, str]:
        """Return a sortable semantic-version key."""
        match = _VERSION_PATTERN.fullmatch(version)
        if not match:
            return (0, 0, 0, 0, version)
        stable = 1 if "-" not in version else 0
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), stable, version)

    def resolve(
        self,
        *,
        kind: str | None = None,
        name: str | None = None,
        version: str | None = None,
        capability: str | None = None,
        accepts: str | None = None,
    ) -> SkillDescriptor:
        """Resolve the best compatible skill descriptor."""
        candidates = [
            descriptor
            for descriptor in self._descriptors.values()
            if (kind is None or descriptor.kind == kind)
            and (name is None or descriptor.name == name)
            and (version is None or descriptor.version == version)
            and (capability is None or capability in descriptor.capabilities)
            and (accepts is None or not descriptor.accepts or accepts in descriptor.accepts)
        ]
        if not candidates:
            criteria = {"kind": kind, "name": name, "version": version, "capability": capability, "accepts": accepts}
            detail = ", ".join(f"{key}={value}" for key, value in criteria.items() if value is not None)
            raise SkillRegistryError(f"No skill matches: {detail}")
        return max(candidates, key=lambda item: (self._version_key(item.version), item.name))

    def load(self, descriptor: SkillDescriptor) -> LoadedSkill:
        """Load and validate a resolved skill implementation."""
        key = (descriptor.name, descriptor.version)
        registered = self._descriptors.get(key)
        if registered != descriptor:
            raise SkillRegistryError(f"Skill is not registered in this registry: {descriptor.reference}")
        with self._lock:
            cached = self._loaded.get(key)
            if cached is not None:
                return cached
            module_name = f"qbr_runtime_skill_{descriptor.content_hash[:16]}"
            module_spec = importlib.util.spec_from_file_location(module_name, descriptor.entrypoint)
            if module_spec is None or module_spec.loader is None:
                raise SkillRegistryError(f"Cannot create module spec for {descriptor.reference}")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            try:
                module_spec.loader.exec_module(module)
                self._validate_runtime_contract(descriptor, module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            loaded = LoadedSkill(descriptor, module)
            self._loaded[key] = loaded
            return loaded

    @staticmethod
    def _validate_runtime_contract(descriptor: SkillDescriptor, module: ModuleType) -> None:
        """Validate that a loaded module implements its declared skill contract."""
        required = {
            PARSER_SKILL_KIND: ("Limits", "extract", "write_outputs", "find_soffice"),
            REASONING_SKILL_KIND: ("answer",),
        }[descriptor.kind]
        missing = [name for name in required if not callable(getattr(module, name, None))]
        if missing:
            raise SkillRegistryError(
                f"Skill {descriptor.reference} is missing runtime callables: {', '.join(missing)}"
            )

    def validate_output(self, descriptor: SkillDescriptor, payload: Any) -> None:
        """Validate a skill result against its declared schema."""
        if not isinstance(payload, dict):
            raise SkillRegistryError(f"Skill {descriptor.reference} returned a non-object result")
        if descriptor.output_schema is not None:
            schema = self._read_schema(descriptor.output_schema)
            missing = [field for field in schema.get("required", []) if field not in payload]
            if missing:
                raise SkillRegistryError(
                    f"Skill {descriptor.reference} output is missing fields: {', '.join(missing)}"
                )
        if str(payload.get("schema_version") or "") != descriptor.schema_version:
            raise SkillRegistryError(
                f"Skill {descriptor.reference} returned schema_version={payload.get('schema_version')!r}; "
                f"expected {descriptor.schema_version!r}"
            )
        parser = payload.get("parser")
        if (
            descriptor.kind == PARSER_SKILL_KIND
            and isinstance(parser, dict)
            and (parser.get("name") != descriptor.name or parser.get("version") != descriptor.version)
        ):
            raise SkillRegistryError(f"Skill output identity does not match {descriptor.reference}")

    def is_loaded(self, name: str, version: str | None = None) -> bool:
        """Return whether the skill implementation is already loaded."""
        return any(skill_name == name and (version is None or skill_version == version) for skill_name, skill_version in self._loaded)

    def status(self) -> list[dict[str, Any]]:
        """Return public registry status and discovered skill metadata."""
        return [
            {
                "name": descriptor.name,
                "version": descriptor.version,
                "kind": descriptor.kind,
                "capabilities": sorted(descriptor.capabilities),
                "uses_model": descriptor.uses_model,
                "cost_class": descriptor.cost_class,
                "loaded": self.is_loaded(descriptor.name, descriptor.version),
                "content_hash": descriptor.content_hash,
            }
            for descriptor in self.descriptors
        ]
