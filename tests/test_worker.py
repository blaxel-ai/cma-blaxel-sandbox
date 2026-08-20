import asyncio
import importlib.util
import os
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


def test_worker_passes_scoped_secret_explicitly_and_scrubs_process_env(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *, auth_token):
            calls.append(("client", auth_token))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeWorker:
        def __init__(self, client, **kwargs):
            calls.append(("worker", kwargs))

        async def handle_item(self, **kwargs):
            calls.append(("handle", kwargs))

    monkeypatch.setattr(worker, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(worker, "EnvironmentWorker", FakeWorker)
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_KEY", "env-key")
    monkeypatch.setenv("ANTHROPIC_WORK_SECRET", "work-secret")

    asyncio.run(worker.main())

    assert ("handle", {"environment_key": "env-key", "work_secret": "work-secret"}) in calls
    assert "ANTHROPIC_ENVIRONMENT_KEY" not in os.environ
    assert "ANTHROPIC_WORK_SECRET" not in os.environ


def test_duration_seconds_rejects_invalid_or_composite_values():
    with pytest.raises(ValueError, match="ANT_MAX_IDLE"):
        worker.duration_seconds("")
    with pytest.raises(ValueError, match="ANT_MAX_IDLE"):
        worker.duration_seconds("1h30m")
