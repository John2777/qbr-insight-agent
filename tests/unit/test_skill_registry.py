from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from packages.qbr_core import QBRService, Settings
from packages.qbr_core.skills.registry import PARSER_SKILL_KIND, SkillRegistry, SkillRegistryError


def _write_parser_skill(
    root: Path,
    entrypoint: str = "scripts/parser.py",
    *,
    import_delay_seconds: float = 0.0,
) -> Path:
    skill_dir = root / "fixture-parser"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "parser.py").write_text(
        f"""from pathlib import Path
import time
time.sleep({import_delay_seconds!r})
Path(__file__).with_suffix('.loaded').write_text('loaded', encoding='utf-8')
class Limits: pass
def extract(*args, **kwargs): return {{}}
def write_outputs(*args, **kwargs): return []
def find_soffice(value): return value
""",
        encoding="utf-8",
    )
    (skill_dir / "schema.json").write_text(
        json.dumps({"type": "object", "required": [], "properties": {"schema_version": {"const": "1.0.0"}}}),
        encoding="utf-8",
    )
    (skill_dir / "manifest.yaml").write_text(
        f"""apiVersion: qbr-agent.skills/v1
kind: ParserSkill
metadata:
  name: fixture-parser
  version: 1.0.0
spec:
  entrypoint: {entrypoint}
  schemaVersion: 1.0.0
  accepts:
    mimeTypes: [application/test]
  outputSchema: schema.json
  capabilities: [native-chart-data]
""",
        encoding="utf-8",
    )
    return skill_dir


def test_registry_discovers_without_importing_and_caches_first_load(tmp_path: Path) -> None:
    skill_dir = _write_parser_skill(tmp_path)
    marker = skill_dir / "scripts" / "parser.loaded"
    registry = SkillRegistry([tmp_path])
    descriptor = registry.resolve(kind=PARSER_SKILL_KIND, capability="native-chart-data", accepts="application/test")

    assert not marker.exists()
    first = registry.load(descriptor)
    second = registry.load(descriptor)

    assert marker.read_text(encoding="utf-8") == "loaded"
    assert first.module is second.module
    assert registry.is_loaded("fixture-parser")


def test_dynamic_loading_defers_import_cost_and_makes_warm_loads_negligible(
    tmp_path: Path,
) -> None:
    import_delay_seconds = 0.08
    skill_dir = _write_parser_skill(tmp_path, import_delay_seconds=import_delay_seconds)
    marker = skill_dir / "scripts" / "parser.loaded"

    started = time.perf_counter()
    registry = SkillRegistry([tmp_path])
    discovery_seconds = time.perf_counter() - started
    descriptor = registry.resolve(
        kind=PARSER_SKILL_KIND,
        capability="native-chart-data",
        accepts="application/test",
    )

    assert not marker.exists(), "Skill discovery must not pay implementation import cost"
    started = time.perf_counter()
    first = registry.load(descriptor)
    first_load_seconds = time.perf_counter() - started

    warm_samples: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        cached = registry.load(descriptor)
        warm_samples.append(time.perf_counter() - started)
        assert cached.module is first.module
    warm_median_seconds = statistics.median(warm_samples)

    assert marker.exists()
    assert discovery_seconds < first_load_seconds
    assert first_load_seconds >= import_delay_seconds * 0.9
    assert warm_median_seconds < import_delay_seconds / 20


def test_registry_rejects_entrypoint_path_escape(tmp_path: Path) -> None:
    _write_parser_skill(tmp_path, entrypoint="../outside.py")

    with pytest.raises(SkillRegistryError, match="escapes"):
        SkillRegistry([tmp_path])


def test_table_reasoning_skill_loads_only_when_table_analysis_is_requested(tmp_path: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    skill_name = service.table_reasoning_skill.name
    source = {
        "content": "市场 | 2024A | 2025A\n香港 | 10 | 15",
        "chunk_type": "table",
        "slide_title": "Market table",
    }

    assert not service.skill_registry.is_loaded(skill_name)
    assert service.qa_service.answer_engine._table_reasoning_answer("香港从2024A到2025A提高了多少？", []) is None
    assert not service.skill_registry.is_loaded(skill_name)

    result = service.qa_service.answer_engine._table_reasoning_answer("香港从2024A到2025A提高了多少？", [source])

    assert result and "提高5" in result.answer
    assert service.skill_registry.is_loaded(skill_name)
    status = next(item for item in service.health()["skills"] if item["name"] == skill_name)
    assert status["loaded"] is True
    assert status["uses_model"] is False


def test_ingestion_uses_registry_selected_parser_metadata(tmp_path: Path, synthetic_pptx: Path) -> None:
    service = QBRService(Settings(tmp_path, tmp_path / "app.sqlite3", tmp_path / "objects"))
    uploaded = service.import_document(
        synthetic_pptx,
        filename="qbr.pptx",
        title="QBR",
        metadata={},
        deduplication="new_version",
        workspace_id="ws_demo",
        user_id="user_demo",
    )

    assert not service.skill_registry.is_loaded(service.parser_skill.name)
    assert service.process_next_job() == uploaded["job"]["id"]
    assert service.skill_registry.is_loaded(service.parser_skill.name)
    with service.db.read() as conn:
        parser_run = conn.execute("SELECT * FROM parser_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert parser_run
    assert parser_run["skill_name"] == service.parser_skill.name
    assert parser_run["skill_version"] == service.parser_skill.version
    assert parser_run["schema_version"] == service.parser_skill.schema_version
