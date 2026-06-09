import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "example"))

run_session = importlib.import_module("run_session")


def test_proof_preflight_allows_quiet_environment(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 0, "pending": 0, "workers_polling": 0},
    )

    run_session.require_quiet_proof_environment()


def test_proof_preflight_rejects_existing_pollers(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 0, "pending": 0, "workers_polling": 2},
    )

    with pytest.raises(SystemExit) as exc:
        run_session.require_quiet_proof_environment()

    message = str(exc.value)
    assert "workers_polling=2" in message
    assert "fresh environment" in message


def test_proof_preflight_rejects_existing_work(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 1, "pending": 1, "workers_polling": 0},
    )

    with pytest.raises(SystemExit) as exc:
        run_session.require_quiet_proof_environment()

    message = str(exc.value)
    assert "depth=1" in message
    assert "pending=1" in message


def test_proof_lines_include_inspect_command():
    text = "\n".join(run_session.proof_lines("cma-worker-x", "ant-run-y", "main"))

    assert "sandbox: cma-worker-x" in text
    assert "process: ant-run-y" in text
    assert "bl get sandbox cma-worker-x process --workspace main -o json" in text


def test_claimed_elsewhere_names_the_shared_environment_invariant():
    text = "\n".join(run_session.claimed_elsewhere_lines("cma-worker-sesn-abc", "main"))

    assert "cma-worker-sesn-abc" in text
    assert "NOT found in workspace main" in text
    assert "one Anthropic environment per Blaxel workspace" in text
    assert "--direct-dispatch" in text


async def test_worker_sandbox_exists_when_get_succeeds(monkeypatch):
    seen = []

    class FakeSandboxInstance:
        @staticmethod
        async def get(name):
            seen.append(name)
            return object()

    monkeypatch.setattr(run_session, "SandboxInstance", FakeSandboxInstance)

    assert await run_session.worker_sandbox_exists("cma-worker-x")
    assert seen == ["cma-worker-x"]


async def test_worker_sandbox_missing_when_get_raises(monkeypatch):
    class FakeSandboxInstance:
        @staticmethod
        async def get(name):
            raise RuntimeError("404 Not Found")

    monkeypatch.setattr(run_session, "SandboxInstance", FakeSandboxInstance)

    assert not await run_session.worker_sandbox_exists("cma-worker-x")
