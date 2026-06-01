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
def reset_session_state():
    """Each test starts and ends with clean per-session orchestrator state.

    `_session_locks` / `_session_last_started_at` are module-level dicts that
    persist across calls in a real run; isolate them per test.
    """
    app._session_locks.clear()
    app._session_last_started_at.clear()
    yield
    app._session_locks.clear()
    app._session_last_started_at.clear()
