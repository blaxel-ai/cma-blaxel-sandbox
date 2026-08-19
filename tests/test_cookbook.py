from types import SimpleNamespace

import pytest

import cookbook


def test_volume_name_matches_session_scoped_resource():
    assert cookbook.volume_name("sesn_ABC_123") == "cma-workspace-sesn-abc-123"


def test_cleanup_defaults_to_plan_only_and_archive():
    args = cookbook.parse_args(["cleanup", "--session", "sesn_123"])
    assert args.apply is False
    assert args.session_action == "archive"
    assert args.interrupt is False


def test_configuration_warnings_report_model_drift():
    agent = {"model": {"id": "claude-opus-4-7"}}
    warnings = cookbook.configuration_warnings(agent)
    assert "claude-opus-4-7" in warnings[0]
    assert "claude-sonnet-5" in warnings[0]
    assert cookbook.configuration_warnings({"model": "claude-sonnet-5"}) == []


async def test_cleanup_plan_does_not_call_external_apis(monkeypatch, capsys):
    monkeypatch.setattr(cookbook, "_load_dotenv", lambda: None)
    args = cookbook.parse_args(["cleanup", "--session", "sesn_123"])
    await cookbook.cleanup(args)
    output = capsys.readouterr().out
    assert "Plan only" in output
    assert "cma-worker-sesn-123" in output


async def test_cleanup_waits_for_interrupt_to_settle_before_deleting_worker(monkeypatch):
    timeline = []
    states = iter(("running", "rescheduling", "idle"))

    async def retrieve(session_id):
        state = next(states)
        timeline.append(f"retrieve:{state}")
        return {"id": session_id, "status": state}

    async def send(session_id, *, events):
        timeline.append("interrupt")

    async def no_sleep(_):
        timeline.append("sleep")

    async def delete_if_present(delete, get, name, *, timeout_seconds):
        timeline.append(f"delete:{name}")
        return "deleted"

    async def archive(session_id):
        timeline.append(f"archive:{session_id}")

    client = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(
                retrieve=retrieve,
                events=SimpleNamespace(send=send),
                archive=archive,
            ),
        ),
    )
    monkeypatch.setattr(cookbook, "_load_dotenv", lambda: None)
    monkeypatch.setattr(cookbook, "AsyncAnthropic", lambda: client)
    monkeypatch.setattr(cookbook.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(cookbook, "_delete_if_present", delete_if_present)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("BL_WORKSPACE", "test")
    args = cookbook.parse_args([
        "cleanup",
        "--session",
        "sesn_123",
        "--interrupt",
        "--apply",
    ])

    await cookbook.cleanup(args)

    assert timeline == [
        "retrieve:running",
        "interrupt",
        "retrieve:rescheduling",
        "sleep",
        "retrieve:idle",
        "delete:cma-worker-sesn-123",
        "delete:cma-workspace-sesn-123",
        "archive:sesn_123",
    ]


async def test_wait_until_missing_waits_through_a_tombstone(monkeypatch):
    calls = 0

    async def get_resource(name):
        nonlocal calls
        calls += 1
        if calls < 3:
            return SimpleNamespace(status="TERMINATED")
        raise RuntimeError("404 Not Found")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(cookbook.asyncio, "sleep", no_sleep)
    await cookbook.wait_until_missing(get_resource, "cma-worker-x", timeout_seconds=1)
    assert calls == 3


async def test_wait_until_missing_does_not_mask_auth_failure():
    async def get_resource(name):
        raise RuntimeError("401 Unauthorized")

    with pytest.raises(RuntimeError, match="401"):
        await cookbook.wait_until_missing(get_resource, "cma-worker-x")
