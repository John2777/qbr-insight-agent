from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def synthetic_pptx(tmp_path: Path) -> Path:
    script_dir = Path(__file__).resolve().parents[1] / "skills" / "extract-ppt-chart-data" / "scripts"
    sys.path.insert(0, str(script_dir))
    spec = importlib.util.spec_from_file_location("qbr_synthetic_fixture", script_dir / "self_test.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    path = tmp_path / "synthetic-qbr.pptx"
    module.make_pptx(path)
    return path

