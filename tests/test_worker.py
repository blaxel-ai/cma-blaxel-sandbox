import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "worker" / "worker.py"
SPEC = importlib.util.spec_from_file_location("cma_worker", PATH)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(worker)


def test_duration_seconds_supports_worker_idle_units():
    assert worker.duration_seconds("500ms") == 0.5
    assert worker.duration_seconds("60s") == 60
    assert worker.duration_seconds("2m") == 120
    assert worker.duration_seconds("1h") == 3600
    assert worker.duration_seconds("0") is None


def test_duration_seconds_rejects_invalid_or_composite_values():
    with pytest.raises(ValueError, match="ANT_MAX_IDLE"):
        worker.duration_seconds("")
    with pytest.raises(ValueError, match="ANT_MAX_IDLE"):
        worker.duration_seconds("1h30m")
