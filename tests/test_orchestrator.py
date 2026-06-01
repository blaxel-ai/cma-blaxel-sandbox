"""Behavior tests for the CMA self-hosted orchestrator (orchestrator/app.py).

Most tests lock already-hardened behavior so the cookbook stays safe to demo and
extend: webhook-duplicate suppression, the per-session restart cooldown, and the
/webhook status branches. `test_worker_name_*` drive a real red->green extraction
of the worker-name sanitizer out of `_spawn_worker`.

No network or real sandboxes: the Anthropic client, the clock, and the spawn call
are all stubbed.
"""
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app


# --- _duration_to_seconds: pure parser; also pins the ttl/idle unit semantics ---

@pytest.mark.parametrize(
    "value, expected",
    [
        ("60s", 60),
        ("90", 90),        # a bare number means seconds
        ("5m", 300),
        ("2h", 7200),
        ("1d", 86400),
        ("1w", 604800),
        ("  2h ", 7200),   # surrounding whitespace tolerated
        ("2H", 7200),      # case-insensitive
    ],
)
def test_duration_to_seconds_parses(value, expected):
    assert app._duration_to_seconds(value, default=999) == expected


@pytest.mark.parametrize("value", ["", "abc", "10x", "h", "1.5h"])
def test_duration_to_seconds_falls_back_on_garbage(value):
    assert app._duration_to_seconds(value, default=42) == 42


# --- _worker_name: external session id -> a valid Blaxel sandbox name ---
# Blaxel names allow only [a-z0-9-]. The id comes from Anthropic, so sanitize it.

def test_worker_name_sanitizes_underscores_and_case():
    assert app._worker_name("sesn_01ABCdef") == "cma-worker-sesn-01abcdef"


def test_worker_name_only_valid_chars():
    name = app._worker_name("sesn_01.AB/xy")
    assert re.fullmatch(r"[a-z0-9-]+", name), name


def test_worker_name_bounded_length():
    name = app._worker_name("sesn_" + "a" * 200)
    assert name.startswith("cma-worker-")
    assert len(name) <= len("cma-worker-") + 40


# --- _spawn_worker_once: duplicate suppression + per-session restart cooldown ---

@pytest.fixture
def fake_clock(monkeypatch):
    holder = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: holder["t"])
    return holder


@pytest.fixture
def spawn_calls(monkeypatch):
    calls = []

    async def _fake_spawn(session_id):
        calls.append(session_id)
        return True

    monkeypatch.setattr(app, "_spawn_worker", _fake_spawn)
    return calls


async def test_spawn_once_suppresses_duplicate_within_cooldown(fake_clock, spawn_calls):
    assert await app._spawn_worker_once("sesn_a") is True
    assert await app._spawn_worker_once("sesn_a") is True  # retry at same instant
    assert spawn_calls == ["sesn_a"]  # only one real spawn


async def test_spawn_once_restarts_after_cooldown(fake_clock, spawn_calls):
    await app._spawn_worker_once("sesn_a")
    fake_clock["t"] += app.worker_restart_cooldown + 1
    await app._spawn_worker_once("sesn_a")
    assert spawn_calls == ["sesn_a", "sesn_a"]  # cooldown elapsed -> later turn respawns


async def test_spawn_once_is_per_session(fake_clock, spawn_calls):
    await app._spawn_worker_once("sesn_a")
    await app._spawn_worker_once("sesn_b")
    assert spawn_calls == ["sesn_a", "sesn_b"]


async def test_spawn_once_failed_spawn_is_retryable(fake_clock, monkeypatch):
    calls = []

    async def _failing(session_id):
        calls.append(session_id)
        return False

    monkeypatch.setattr(app, "_spawn_worker", _failing)
    assert await app._spawn_worker_once("sesn_a") is False
    # A failed spawn is not recorded, so the next delivery actually retries
    # instead of being suppressed as a duplicate.
    assert await app._spawn_worker_once("sesn_a") is False
    assert calls == ["sesn_a", "sesn_a"]


# --- /webhook status branches ---

@pytest.fixture
def client():
    return TestClient(app.app)


@pytest.fixture
def stub_unwrap(monkeypatch):
    """Replace the Anthropic client so unwrap() returns a chosen event or raises."""

    def _set(event=None, raise_exc=None):
        def _unwrap(payload, headers=None):
            if raise_exc is not None:
                raise raise_exc
            return event

        webhooks = SimpleNamespace(unwrap=_unwrap)
        monkeypatch.setattr(app, "client", SimpleNamespace(beta=SimpleNamespace(webhooks=webhooks)))

    return _set


def _started_event(session_id="sesn_x"):
    return SimpleNamespace(data=SimpleNamespace(type="session.status_run_started", id=session_id))


def test_webhook_503_when_no_signing_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", raising=False)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 503
    assert "signing key" in r.json()["error"]


def test_webhook_401_on_bad_signature(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(raise_exc=ValueError("bad sig"))
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 401
    assert "signature" in r.json()["error"]


def test_webhook_200_spawns_worker_on_run_started(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=_started_event("sesn_x"))
    seen = []

    async def _spawn(session_id):
        seen.append(session_id)
        return True

    monkeypatch.setattr(app, "_spawn_worker_once", _spawn)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert seen == ["sesn_x"]


def test_webhook_503_when_spawn_fails(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=_started_event())

    async def _spawn(session_id):
        return False

    monkeypatch.setattr(app, "_spawn_worker_once", _spawn)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 503
    assert "poller" in r.json()["error"]


def test_webhook_ignores_non_started_events(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=SimpleNamespace(data=SimpleNamespace(type="session.status_run_completed", id="sesn_x")))
    called = []

    async def _spawn(session_id):
        called.append(session_id)
        return True

    monkeypatch.setattr(app, "_spawn_worker_once", _spawn)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 200
    assert called == []  # only session.status_run_started spawns a worker


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
