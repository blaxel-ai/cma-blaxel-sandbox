"""Shared test setup for the orchestrator suite.

`orchestrator/app.py` reads ANTHROPIC_ENVIRONMENT_ID / _KEY at import time, so
they must exist before any test module does `import app`. This conftest is
imported by pytest before the test modules, so setting them here is enough.
"""
import os

# Harmless placeholders: every test that touches Anthropic stubs the client.
os.environ.setdefault("ANTHROPIC_ENVIRONMENT_ID", "env_test")
os.environ.setdefault("ANTHROPIC_ENVIRONMENT_KEY", "sk-ant-oat01-test")

import app  # noqa: E402  (must follow the env setup above; on path via pytest.ini)
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def reset_dispatcher_lock():
    """Each test leaves dispatcher process-local state in a clean state."""
    assert not app._dispatcher_lock.locked()
    app._background_tasks.clear()
    app._scheduled_session_ids.clear()
    app._worker_ready_tasks.clear()
    app._work_ids_in_flight.clear()
    yield
    assert not app._dispatcher_lock.locked()
    app._background_tasks.clear()
    app._scheduled_session_ids.clear()
    app._worker_ready_tasks.clear()
    app._work_ids_in_flight.clear()
