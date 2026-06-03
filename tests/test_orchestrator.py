"""Behavior tests for the CMA self-hosted orchestrator.

The orchestrator is a webhook-triggered dispatcher: it claims exact Anthropic
work items with the SDK, then launches one Blaxel worker process bound to that
work id and session id. No real network or sandboxes are used here.
"""
import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app


@pytest.mark.parametrize(
    "value, expected",
    [
        ("60s", 60),
        ("90", 90),
        ("5m", 300),
        ("2h", 7200),
        ("1d", 86400),
        ("1w", 604800),
        ("  2h ", 7200),
        ("2H", 7200),
    ],
)
def test_duration_to_seconds_parses(value, expected):
    assert app._duration_to_seconds(value, default=999) == expected


@pytest.mark.parametrize("value", ["", "abc", "10x", "h", "1.5h"])
def test_duration_to_seconds_falls_back_on_garbage(value):
    assert app._duration_to_seconds(value, default=42) == 42


def test_worker_name_sanitizes_underscores_and_case():
    assert app._worker_name("sesn_01ABCdef") == "cma-worker-sesn-01abcdef"


def test_worker_name_only_valid_chars():
    name = app._worker_name("sesn_01.AB/xy")
    assert re.fullmatch(r"[a-z0-9-]+", name), name


def test_worker_name_bounded_length():
    name = app._worker_name("sesn_" + "a" * 200)
    assert name.startswith("cma-worker-")
    assert len(name) <= len("cma-worker-") + 40


def test_process_name_uses_work_id_and_valid_chars():
    name = app._process_name("work__01.AB/xy")
    assert name.startswith("ant-run-work--01-ab-xy")
    assert re.fullmatch(r"[a-z0-9-]+", name), name


def _session_work(work_id="work_1", session_id="sesn_x"):
    return SimpleNamespace(
        id=work_id,
        environment_id="env_test",
        data=SimpleNamespace(type="session", id=session_id),
    )


class FakeProcess:
    def __init__(self, fail_run=False, order=None):
        self.calls = []
        self.fail_run = fail_run
        self.order = order

    async def exec(self, spec):
        self.calls.append(spec)
        if self.order is not None:
            self.order.append(("exec", spec["command"]))
        if self.fail_run and "ant beta:worker run" in spec.get("command", ""):
            raise RuntimeError("run failed")
        return SimpleNamespace(logs="ok")


class FakeSandbox:
    created_specs = []
    fail_create = False
    process = None

    @classmethod
    async def create_if_not_exists(cls, spec):
        if cls.fail_create:
            raise RuntimeError("create failed")
        cls.created_specs.append(spec)
        cls.process = FakeProcess()
        return SimpleNamespace(process=cls.process)


@pytest.fixture
def fake_sandbox(monkeypatch):
    FakeSandbox.created_specs = []
    FakeSandbox.fail_create = False
    FakeSandbox.process = None
    monkeypatch.setattr(app, "SandboxInstance", FakeSandbox)
    return FakeSandbox


@pytest.fixture
def stop_calls(monkeypatch):
    calls = []

    async def _stop(work, *, force=True):
        calls.append((work.id, work.environment_id, force))

    monkeypatch.setattr(app, "_stop_work", _stop)
    return calls


async def test_dispatch_work_item_starts_ant_run_with_work_and_session_env(fake_sandbox):
    work = _session_work(work_id="work_123", session_id="sesn_ABC")

    assert await app._dispatch_work_item(work) is True

    assert fake_sandbox.created_specs == [{
        "name": "cma-worker-sesn-abc",
        "image": app.worker_image,
        "memory": 4096,
        "ttl": app.worker_ttl,
    }]
    probe, run = fake_sandbox.process.calls
    assert probe["command"] == "node -v"
    assert run["command"] == f"ant beta:worker run --workdir /workspace --max-idle {app.worker_max_idle}"
    assert run["wait_for_completion"] is False
    assert run["keep_alive"] is True
    assert run["timeout"] == app.worker_keepalive_timeout
    assert run["env"]["ANTHROPIC_WORK_ID"] == "work_123"
    assert run["env"]["ANTHROPIC_SESSION_ID"] == "sesn_ABC"
    assert run["env"]["ANTHROPIC_ENVIRONMENT_ID"] == "env_test"
    assert run["env"]["ANTHROPIC_ENVIRONMENT_KEY"] == app.environment_key


async def test_dispatch_does_not_heartbeat_before_ant_run(monkeypatch):
    heartbeat_calls = []
    process = FakeProcess()
    prepared_worker = SimpleNamespace(process=process)

    async def _heartbeat(work_id, *, environment_id, desired_ttl_seconds=None, expected_last_heartbeat=None):
        heartbeat_calls.append((work_id, environment_id, desired_ttl_seconds, expected_last_heartbeat))

    monkeypatch.setattr(
        app,
        "client",
        SimpleNamespace(
            beta=SimpleNamespace(
                environments=SimpleNamespace(work=SimpleNamespace(heartbeat=_heartbeat))
            )
        ),
    )

    assert await app._dispatch_work_item(
        _session_work(work_id="work_no_bridge", session_id="sesn_no_bridge"),
        prepared_worker=prepared_worker,
    ) is True

    assert heartbeat_calls == []
    assert process.calls[0]["command"] == (
        f"ant beta:worker run --workdir /workspace --max-idle {app.worker_max_idle}"
    )


async def test_dispatch_work_item_uses_prepared_worker_without_readiness_probe(fake_sandbox):
    process = FakeProcess()
    prepared_worker = SimpleNamespace(process=process)

    assert await app._dispatch_work_item(
        _session_work(work_id="work_prepared", session_id="sesn_ready"),
        prepared_worker=prepared_worker,
    ) is True

    assert fake_sandbox.created_specs == []
    assert len(process.calls) == 1
    assert process.calls[0]["command"] == (
        f"ant beta:worker run --workdir /workspace --max-idle {app.worker_max_idle}"
    )


async def test_dispatch_suppresses_duplicate_work_id(fake_sandbox):
    process = FakeProcess()
    prepared_worker = SimpleNamespace(process=process)
    work = _session_work(work_id="work_dup", session_id="sesn_dup")

    assert await app._dispatch_work_item(work, prepared_worker=prepared_worker) is True
    assert await app._dispatch_work_item(work, prepared_worker=prepared_worker) is True

    run_calls = [c for c in process.calls if "ant beta:worker run" in c["command"]]
    assert len(run_calls) == 1
    assert fake_sandbox.created_specs == []


async def test_dispatch_non_session_work_force_stops(stop_calls, fake_sandbox):
    work = SimpleNamespace(
        id="work_other",
        environment_id="env_test",
        data=SimpleNamespace(type="memory", id="mem_x"),
    )

    assert await app._dispatch_work_item(work) is True

    assert stop_calls == [("work_other", "env_test", True)]
    assert fake_sandbox.created_specs == []


async def test_dispatch_create_failure_force_stops_work(monkeypatch, stop_calls, fake_sandbox):
    fake_sandbox.fail_create = True

    assert await app._dispatch_work_item(_session_work()) is False

    assert stop_calls == [("work_1", "env_test", True)]


async def test_dispatch_run_failure_force_stops_work(monkeypatch, stop_calls):
    class FailingRunSandbox(FakeSandbox):
        @classmethod
        async def create_if_not_exists(cls, spec):
            cls.created_specs.append(spec)
            cls.process = FakeProcess(fail_run=True)
            return SimpleNamespace(process=cls.process)

    FailingRunSandbox.created_specs = []
    monkeypatch.setattr(app, "SandboxInstance", FailingRunSandbox)
    monkeypatch.setattr(app, "worker_run_attempts", 1)

    assert await app._dispatch_work_item(_session_work()) is False

    assert stop_calls == [("work_1", "env_test", True)]


async def test_drain_dispatches_every_claimed_work(monkeypatch):
    works = [_session_work("work_1", "sesn_a"), _session_work("work_2", "sesn_b")]
    dispatched = []
    prepared_worker = object()

    async def _dispatch(work, *, prepared_worker=None):
        dispatched.append((work.id, prepared_worker))
        return True

    async def _poller(**kwargs):
        assert kwargs["environment_id"] == app.environment_id
        assert kwargs["environment_key"] == app.environment_key
        assert kwargs["reclaim_older_than_ms"] == app.dispatcher_reclaim_ms
        assert kwargs["drain"] is True
        assert kwargs["auto_stop"] is False
        for work in works:
            yield work

    monkeypatch.setattr(app, "_dispatch_work_item", _dispatch)
    monkeypatch.setattr(app, "client", SimpleNamespace(
        beta=SimpleNamespace(
            environments=SimpleNamespace(
                work=SimpleNamespace(poller=_poller)
            )
        )
    ))

    assert await app._drain_and_dispatch_work(prepared_workers={"sesn_a": prepared_worker}) is True
    assert dispatched == [("work_1", prepared_worker), ("work_2", None)]


async def test_drain_returns_false_when_any_dispatch_fails(monkeypatch):
    works = [_session_work("work_1"), _session_work("work_2")]

    async def _dispatch(work, *, prepared_worker=None):
        return work.id != "work_2"

    async def _poller(**kwargs):
        for work in works:
            yield work

    monkeypatch.setattr(app, "_dispatch_work_item", _dispatch)
    monkeypatch.setattr(app, "client", SimpleNamespace(
        beta=SimpleNamespace(
            environments=SimpleNamespace(
                work=SimpleNamespace(poller=_poller)
            )
        )
    ))

    assert await app._drain_and_dispatch_work() is False


async def test_duplicate_drains_do_not_overlap(monkeypatch):
    active = 0
    max_active = 0

    async def _dispatch(work, *, prepared_worker=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    async def _poller(**kwargs):
        yield _session_work(f"work_{id(kwargs)}")

    monkeypatch.setattr(app, "_dispatch_work_item", _dispatch)
    monkeypatch.setattr(app, "client", SimpleNamespace(
        beta=SimpleNamespace(
            environments=SimpleNamespace(
                work=SimpleNamespace(poller=_poller)
            )
        )
    ))

    assert await asyncio.gather(app._drain_and_dispatch_work(), app._drain_and_dispatch_work()) == [True, True]
    assert max_active == 1


async def test_dispatch_for_session_readies_worker_before_draining(monkeypatch):
    order = []
    prepared_worker = object()

    async def _ready(session_id):
        order.append(("ready", session_id))
        return prepared_worker

    async def _drain(*, prepared_workers=None):
        order.append(("drain", prepared_workers))
        return True

    async def _active():
        return set()

    monkeypatch.setattr(app, "_worker_ready_for_session", _ready)
    monkeypatch.setattr(app, "_drain_and_dispatch_work", _drain)
    monkeypatch.setattr(app, "_active_work_session_ids", _active)
    monkeypatch.setattr(app, "dispatcher_debounce_ms", 0)
    app._scheduled_session_ids.add("sesn_x")

    await app._dispatch_for_session("sesn_x")

    assert order == [
        ("ready", "sesn_x"),
        ("drain", {"sesn_x": prepared_worker}),
    ]
    assert "sesn_x" not in app._scheduled_session_ids


async def test_dispatch_for_session_readies_all_known_sessions_before_draining(monkeypatch):
    order = []
    workers = {
        "sesn_a": object(),
        "sesn_b": object(),
        "sesn_c": object(),
    }

    async def _ready(session_id):
        order.append(("ready", session_id))
        return workers[session_id]

    async def _active():
        order.append(("list-active", None))
        return {"sesn_c"}

    async def _drain(*, prepared_workers=None):
        order.append(("drain", prepared_workers))
        return True

    monkeypatch.setattr(app, "_worker_ready_for_session", _ready)
    monkeypatch.setattr(app, "_active_work_session_ids", _active)
    monkeypatch.setattr(app, "_drain_and_dispatch_work", _drain)
    monkeypatch.setattr(app, "dispatcher_debounce_ms", 0)
    app._scheduled_session_ids.update({"sesn_a", "sesn_b"})

    await app._dispatch_for_session("sesn_a")

    assert order[:1] == [("list-active", None)]
    assert set(order[1:4]) == {
        ("ready", "sesn_a"),
        ("ready", "sesn_b"),
        ("ready", "sesn_c"),
    }
    assert order[4] == ("drain", workers)
    assert "sesn_a" not in app._scheduled_session_ids
    assert "sesn_b" in app._scheduled_session_ids


async def test_schedule_dispatch_suppresses_duplicate_session(monkeypatch):
    started = []

    async def _dispatch(session_id):
        started.append(session_id)
        app._scheduled_session_ids.discard(session_id)

    monkeypatch.setattr(app, "_dispatch_for_session", _dispatch)

    assert app._schedule_dispatch_for_session("sesn_x") is True
    assert app._schedule_dispatch_for_session("sesn_x") is False
    await asyncio.gather(*app._background_tasks)

    assert started == ["sesn_x"]


@pytest.fixture
def client():
    return TestClient(app.app)


@pytest.fixture
def stub_unwrap(monkeypatch):
    def _set(event=None, raise_exc=None):
        def _unwrap(payload, headers=None):
            if raise_exc is not None:
                raise raise_exc
            return event

        webhooks = SimpleNamespace(unwrap=_unwrap)
        environments = SimpleNamespace(work=SimpleNamespace())
        monkeypatch.setattr(
            app,
            "client",
            SimpleNamespace(beta=SimpleNamespace(webhooks=webhooks, environments=environments)),
        )

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


def test_webhook_200_schedules_background_dispatch_on_run_started(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=_started_event("sesn_x"))
    calls = []

    def _schedule(session_id):
        calls.append(session_id)
        return True

    monkeypatch.setattr(app, "_schedule_dispatch_for_session", _schedule)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert calls == ["sesn_x"]


def test_webhook_503_when_session_id_missing(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=SimpleNamespace(data=SimpleNamespace(type="session.status_run_started")))

    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 503
    assert "session id" in r.json()["error"]


def test_webhook_ignores_non_started_events(client, monkeypatch, stub_unwrap):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "whsec_test")
    stub_unwrap(event=SimpleNamespace(data=SimpleNamespace(type="session.status_run_completed", id="sesn_x")))
    called = []

    async def _drain():
        called.append("drain")
        return True

    monkeypatch.setattr(app, "_drain_and_dispatch_work", _drain)
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 200
    assert called == []


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
