from types import SimpleNamespace

import pytest

from example import session_runtime


class AsyncItems:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        async def generate():
            for item in self.items:
                yield item

        return generate()


class FakeStream(AsyncItems):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def fake_client(*, stream_events=None, baseline=None, history=None, stream_error=None, stats=None):
    sent = []
    updates = []

    async def stream(session_id):
        if stream_error:
            raise stream_error
        return FakeStream(stream_events or [])

    async def send(session_id, *, events):
        sent.append((session_id, events))

    history_calls = 0

    async def list_events(session_id, **kwargs):
        nonlocal history_calls
        history_calls += 1
        items = (baseline or []) if history_calls == 1 else (history or [])
        return AsyncItems(items)

    async def update(session_id, **kwargs):
        updates.append((session_id, kwargs))

    async def work_stats(environment_id):
        return stats or {"depth": 0, "pending": 0, "workers_polling": 0}

    client = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(
                events=SimpleNamespace(stream=stream, send=send, list=list_events),
                update=update,
            ),
            environments=SimpleNamespace(work=SimpleNamespace(stats=work_stats)),
        )
    )
    return client, sent, updates


def test_session_budget_uses_whole_usd_cents():
    assert session_runtime.session_budget(2500) == {
        "type": "limit",
        "max_list_cost": {"amount": "2500", "currency": "USD"},
    }
    with pytest.raises(ValueError):
        session_runtime.session_budget(0)


def test_event_helpers_find_final_text_errors_and_stop_reason():
    events = [
        {"type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        {"type": "user.tool_result", "is_error": True},
        {"type": "session.status_idle", "stop_reason": {"type": "budget_reached"}},
    ]
    assert session_runtime.final_agent_text(events) == "done"
    assert len(session_runtime.tool_errors(events)) == 1
    assert session_runtime.idle_stop_reason(events) == "budget_reached"
    assert session_runtime.idle_stop_count(events, "budget_reached") == 1


async def test_all_events_consumes_the_complete_async_paginator():
    events = [{"id": "one"}, {"id": "two"}, {"id": "three"}]
    client, _, _ = fake_client(baseline=events, history=events)
    runtime = session_runtime.ManagedSessionRuntime(client)
    assert await runtime.all_events("sesn_x") == events


async def test_run_turn_streams_then_reconciles_authoritative_history():
    history = [
        {"id": "run", "type": "session.status_running"},
        {"id": "msg", "type": "agent.message", "content": [{"type": "text", "text": "ok"}]},
        {"id": "idle", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    client, sent, _ = fake_client(stream_events=history, history=history)
    runtime = session_runtime.ManagedSessionRuntime(client, poll_seconds=0)

    async def dispatch():
        return "worker-proof"

    result = await runtime.run_turn("sesn_x", "hello", on_started=dispatch)
    assert result.stop_reason == "end_turn"
    assert result.events == history
    assert result.dispatch_result == "worker-proof"
    assert sent == [("sesn_x", [{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}])]


async def test_run_turn_falls_back_to_history_after_stream_disconnect():
    history = [
        {"id": "idle", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    client, sent, _ = fake_client(stream_error=RuntimeError("disconnect"), history=history)
    runtime = session_runtime.ManagedSessionRuntime(client, poll_seconds=0)
    result = await runtime.run_turn("sesn_x", "hello")
    assert result.stop_reason == "end_turn"
    assert len(sent) == 1


async def test_run_turn_ignores_an_idle_event_from_a_previous_turn():
    old_idle = {"id": "old", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}}
    new_idle = {"id": "new", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}}
    client, _, _ = fake_client(
        baseline=[old_idle],
        stream_events=[old_idle, new_idle],
        history=[old_idle, new_idle],
    )
    runtime = session_runtime.ManagedSessionRuntime(client, poll_seconds=0)
    result = await runtime.run_turn("sesn_x", "next turn")
    assert result.stop_reason == "end_turn"
    assert result.events == [old_idle, new_idle]


async def test_run_turn_raises_on_session_error_event():
    error = {
        "id": "err",
        "type": "session.error",
        "error": {"type": "billing_error", "message": "payment required", "retry_status": "not_retryable"},
    }
    client, _, _ = fake_client(stream_events=[error], history=[error])
    runtime = session_runtime.ManagedSessionRuntime(client)
    with pytest.raises(session_runtime.SessionExecutionError, match="payment required"):
        await runtime.run_turn("sesn_x", "hello")


async def test_run_turn_can_raise_a_reached_budget_once():
    stream_events = [
        {"id": "budget", "type": "session.status_idle", "stop_reason": {"type": "budget_reached"}},
        {"id": "done", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    client, _, updates = fake_client(stream_events=stream_events, history=stream_events)
    runtime = session_runtime.ManagedSessionRuntime(client, poll_seconds=0)
    result = await runtime.run_turn("sesn_x", "hello", resume_budget_cents=250)
    assert result.stop_reason == "end_turn"
    assert updates == [("sesn_x", {"budget": session_runtime.session_budget(250)})]


async def test_quiet_environment_rejects_other_claimants():
    client, _, _ = fake_client(stats={"depth": 1, "pending": 0, "workers_polling": 2})
    runtime = session_runtime.ManagedSessionRuntime(client)
    with pytest.raises(RuntimeError, match="workers_polling=2"):
        await runtime.require_quiet_environment("env_x")


def test_usage_receipt_captures_cost_timing_and_resolved_model():
    receipt = session_runtime.usage_receipt({
        "id": "sesn_x",
        "status": "idle",
        "agent": {"model": {"id": "claude-sonnet-5"}},
        "usage": {
            "list_cost": {"amount": "42", "currency": "USD"},
            "input_tokens": 10,
            "output_tokens": 20,
            "active_seconds": 3.5,
        },
        "stats": {"duration_seconds": 5.0},
    })
    assert receipt["model"] == "claude-sonnet-5"
    assert receipt["list_cost_cents"] == "42"
    assert receipt["active_seconds"] == 3.5
