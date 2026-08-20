import importlib
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "example"))

run_session = importlib.import_module("run_session")
direct_dispatch = importlib.import_module("direct_dispatch")


def test_default_run_has_a_one_dollar_hard_budget():
    args = run_session.parse_args([])
    assert args.scenario == "hello"
    assert args.budget_cents == 100
    assert args.no_budget is False


def test_no_budget_requires_an_explicit_flag():
    args = run_session.parse_args(["--no-budget"])
    assert args.no_budget is True


def test_follow_ups_and_memory_store_are_repeatable_session_inputs():
    args = run_session.parse_args([
        "--follow-up", "second",
        "--follow-up", "third",
        "--memory-store-id", "mem_one",
        "--memory-store-id", "mem_two",
    ])
    assert args.follow_up == ["second", "third"]
    assert args.memory_store_id == ["mem_one", "mem_two"]


def test_resume_budget_must_raise_the_ceiling():
    with pytest.raises(SystemExit):
        run_session.parse_args(["--budget-cents", "100", "--resume-budget-cents", "100"])


def test_skill_scenario_requires_a_skill_name():
    with pytest.raises(SystemExit):
        run_session.parse_args(["--scenario", "skill"])
    args = run_session.parse_args(["--scenario", "skill", "--skill-name", "xlsx"])
    assert args.skill_name == "xlsx"


def test_hello_scenario_requires_tools_marker_and_end_turn():
    events = [
        {"type": "agent.tool_use", "id": "one", "name": "write", "input": {"file_path": "hello.txt"}},
        {"type": "agent.tool_use", "id": "two", "name": "bash", "input": {"command": "cat /workspace/hello.txt"}},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    ok, checks = run_session._scenario_verdict("hello", events, "BLAXEL_CMA_OK")
    assert ok is True
    assert all(checks.values())


def test_advisor_scenario_requires_a_real_advisor_thread():
    events = [
        {"type": "agent.tool_use", "id": "one", "name": "write", "input": {"file_path": "advisor-proof.txt"}},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    ok, checks = run_session._scenario_verdict(
        "advisor",
        events,
        "BLAXEL_ADVISOR_OK",
        advisor_threads=[{"agent": {"type": "advisor", "model": "claude-opus-5"}}],
    )
    assert ok is True
    assert checks["advisor_thread"] is True


def test_proof_lines_include_inspect_command():
    text = "\n".join(run_session.proof_lines("cma-worker-x", "cma-run-y", "main"))
    assert "sandbox: cma-worker-x" in text
    assert "process: cma-run-y" in text
    assert "bl get sandbox cma-worker-x process --workspace main -o json" in text


def test_claimed_elsewhere_names_the_shared_environment_invariant():
    text = "\n".join(run_session.claimed_elsewhere_lines("cma-worker-sesn-abc", "main"))
    assert "cma-worker-sesn-abc" in text
    assert "NOT found in workspace main" in text
    assert "one Anthropic environment per Blaxel workspace" in text
    assert "--direct-dispatch" in text


async def test_direct_dispatch_reuses_live_worker_without_polling(monkeypatch):
    worker = SimpleNamespace(
        process=SimpleNamespace(
            list=lambda: None,
        ),
    )

    async def list_processes():
        return [SimpleNamespace(name="cma-run-work-old-abc", status="running")]

    worker.process.list = list_processes

    async def ready(session_id):
        return worker

    async def unexpected_poll(**kwargs):
        raise AssertionError("a live session worker must not claim another work item")

    monkeypatch.setattr(direct_dispatch, "ready_worker_for_session", ready)
    monkeypatch.setattr(direct_dispatch, "dispatch_available_work", unexpected_poll)

    result = await direct_dispatch.dispatch_until_session_work("sesn_x")

    assert result.process_name == "cma-run-work-old-abc"
    assert result.work_id == "active"


async def test_direct_dispatch_runs_for_initial_and_follow_up_turns(monkeypatch):
    callbacks = []
    dispatches = []
    events = [
        {"type": "agent.tool_use", "id": "one", "name": "write", "input": {"file_path": "hello.txt"}},
        {"type": "agent.tool_use", "id": "two", "name": "bash", "input": {"command": "cat /workspace/hello.txt"}},
        {"type": "agent.message", "content": [{"type": "text", "text": "BLAXEL_CMA_OK"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]

    class FakeRuntime:
        async def require_quiet_environment(self, environment_id):
            return None

        async def create_session(self, **kwargs):
            return {"id": "sesn_x"}

        async def run_turn(self, session_id, message, **kwargs):
            callback = kwargs.get("on_started")
            callbacks.append(callback)
            result = await callback() if callback else None
            return SimpleNamespace(
                session_id=session_id,
                events=events,
                stop_reason="end_turn",
                dispatch_result=result,
            )

        async def receipt(self, session_id):
            return {}

    async def dispatch(session_id):
        dispatches.append(session_id)
        return SimpleNamespace(sandbox_name="cma-worker-sesn-x", process_name="cma-run-x")

    monkeypatch.setattr(run_session, "ManagedSessionRuntime", FakeRuntime)
    monkeypatch.setattr(run_session, "dispatch_until_session_work", dispatch)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ENVIRONMENT_ID",
        "ANTHROPIC_AGENT_ID",
        "ANTHROPIC_ENVIRONMENT_KEY",
        "BL_API_KEY",
        "BL_WORKSPACE",
    ):
        monkeypatch.setenv(name, "test")

    await run_session.main(["--direct-dispatch", "--follow-up", "again"])

    assert all(callback is not None for callback in callbacks)
    assert dispatches == ["sesn_x", "sesn_x"]


async def test_worker_sandbox_lookup_found(monkeypatch):
    sandbox = object()

    class FakeSandboxInstance:
        @staticmethod
        async def get(name):
            assert name == "cma-worker-x"
            return sandbox

    monkeypatch.setattr(run_session, "SandboxInstance", FakeSandboxInstance)
    assert await run_session.worker_sandbox_lookup("cma-worker-x") == ("found", sandbox)


async def test_worker_sandbox_lookup_missing_on_not_found(monkeypatch):
    class FakeSandboxInstance:
        @staticmethod
        async def get(name):
            raise RuntimeError("404 Not Found")

    monkeypatch.setattr(run_session, "SandboxInstance", FakeSandboxInstance)
    assert await run_session.worker_sandbox_lookup("cma-worker-x") == ("missing", None)


async def test_worker_sandbox_lookup_unknown_on_auth_failure(monkeypatch):
    class FakeSandboxInstance:
        @staticmethod
        async def get(name):
            raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(run_session, "SandboxInstance", FakeSandboxInstance)
    assert await run_session.worker_sandbox_lookup("cma-worker-x") == ("unknown", None)


async def test_ant_run_process_name_picks_latest_ant_run():
    class FakeProcessAPI:
        async def list(self):
            return [
                SimpleNamespace(name="probe-abc123"),
                SimpleNamespace(name="cma-run-sesn-x-1"),
                SimpleNamespace(name="cma-run-sesn-x-2"),
            ]

    assert await run_session.ant_run_process_name(SimpleNamespace(process=FakeProcessAPI())) == (
        "cma-run-sesn-x-2"
    )


async def test_ant_run_process_name_none_when_listing_fails():
    class FakeProcessAPI:
        async def list(self):
            raise RuntimeError("boom")

    assert await run_session.ant_run_process_name(SimpleNamespace(process=FakeProcessAPI())) is None
