from types import SimpleNamespace

from example import direct_dispatch


def _session_work(session_id: str, state: str = "queued"):
    return SimpleNamespace(
        id=session_id,
        environment_id="env_test",
        state=state,
        data=SimpleNamespace(type="session", id=session_id),
    )


async def test_dispatch_available_work_only_prewarms_still_queued_sessions(monkeypatch):
    work_api_calls = []
    ready_calls = []

    class FakeWorkAPI:
        async def list(self, environment_id, limit):
            assert environment_id == "env_test"
            assert limit == 50
            return SimpleNamespace(data=[
                _session_work("sesn_queued", "queued"),
                _session_work("sesn_stale_active", "active"),
                _session_work("sesn_stopped", "stopped"),
                SimpleNamespace(
                    id="mem_1",
                    environment_id="env_test",
                    state="queued",
                    data=SimpleNamespace(type="memory", id="mem_1"),
                ),
            ])

        async def poller(self, **kwargs):
            work_api_calls.append(kwargs)
            if False:
                yield None

    fake_work_api = FakeWorkAPI()
    fake_client = SimpleNamespace(
        beta=SimpleNamespace(environments=SimpleNamespace(work=fake_work_api))
    )

    async def ready_worker_for_session(session_id):
        ready_calls.append(session_id)
        return object()

    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_ID", "env_test")
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_KEY", "sk-env-test")
    monkeypatch.setattr(direct_dispatch, "AsyncAnthropic", lambda auth_token: fake_client)
    monkeypatch.setattr(direct_dispatch, "DISPATCHER_DEBOUNCE_MS", 0)
    monkeypatch.setattr(direct_dispatch, "DISPATCHER_WORKER_ID", "direct-test-worker")
    monkeypatch.setattr(direct_dispatch, "ready_worker_for_session", ready_worker_for_session)

    assert await direct_dispatch.dispatch_available_work() == []

    assert ready_calls == ["sesn_queued"]
    assert work_api_calls == [{
        "environment_id": "env_test",
        "environment_key": "sk-env-test",
        "worker_id": "direct-test-worker",
        "block_ms": 999,
        "reclaim_older_than_ms": direct_dispatch.DISPATCHER_RECLAIM_MS,
        "drain": True,
        "auto_stop": False,
    }]
