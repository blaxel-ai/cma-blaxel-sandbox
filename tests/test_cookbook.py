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
