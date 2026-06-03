"""Tests for setup.py's in-place orchestrator update + restart path.

The bug it locks: re-running setup after adding ANTHROPIC_WEBHOOK_SIGNING_KEY
left the old server running with stale config (and create_if_not_exists doesn't
update existing sandbox runtime config), so deliveries kept being rejected with
503 or the sandbox could stay on an older image. The fix updates the sandbox
spec, kills the old server, and starts a fresh one with the current env passed
at the process level while preserving the stable preview URL.
"""
import pytest

import setup


class _FakeProc:
    def __init__(self, name="", command="", pid=""):
        self.name = name
        self.command = command
        self.pid = pid


class _FakeProcessAPI:
    def __init__(self, existing):
        self._existing = existing
        self.killed = []
        self.execed = []

    async def list(self):
        return self._existing

    async def kill(self, identifier):
        self.killed.append(identifier)

    async def exec(self, spec):
        self.execed.append(spec)


class _FakeSandbox:
    def __init__(self, existing):
        self.process = _FakeProcessAPI(existing)


def test_orchestrator_sandbox_body_includes_runtime_config(monkeypatch):
    monkeypatch.setattr(setup, "NAME", "cma-orchestrator-test")
    monkeypatch.setattr(setup, "IMAGE", "sandbox/cma-orchestrator:test")
    monkeypatch.setattr(setup, "ORCHESTRATOR_TTL", "3d")
    monkeypatch.setenv("BL_REGION", "us-pdx-1")
    envs = [{"name": "BL_WORKSPACE", "value": "main"}]

    body = setup._orchestrator_sandbox_body(envs)

    assert body["metadata"]["name"] == "cma-orchestrator-test"
    assert body["spec"]["region"] == "us-pdx-1"
    runtime = body["spec"]["runtime"]
    assert runtime["image"] == "sandbox/cma-orchestrator:test"
    assert runtime["ttl"] == "3d"
    assert runtime["memory"] == 2048
    assert runtime["envs"] == envs
    assert runtime["ports"] == [
        {"name": "sandbox-api", "target": 8080, "protocol": "HTTP"},
        {"name": "webhook", "target": setup.PORT, "protocol": "HTTP"},
    ]


async def test_upsert_creates_then_updates_existing_sandbox(monkeypatch):
    calls = []
    fake_client = object()
    updated_model = object()

    class _FakeSandboxInstance:
        @classmethod
        async def create_if_not_exists(cls, body):
            calls.append(("create", body))

        def __init__(self, model=None):
            self.model = model

    async def _fake_update_sandbox(name, *, client, body):
        calls.append(("update", name, client, body))
        return updated_model

    monkeypatch.setattr(setup, "SandboxInstance", _FakeSandboxInstance)
    monkeypatch.setattr(setup, "update_sandbox", _fake_update_sandbox)
    monkeypatch.setattr(setup, "blaxel_client", fake_client)

    body = {"metadata": {"name": "cma-orchestrator-app"}, "spec": {}}
    result = await setup._upsert_orchestrator_sandbox(body)

    assert calls == [
        ("create", body),
        ("update", setup.NAME, fake_client, body),
    ]
    assert result.model is updated_model


def test_upsert_rejects_empty_or_error_update_response():
    class Error:
        code = "bad_request"
        message = "bad image"
        status_code = 400

    with pytest.raises(RuntimeError, match="empty response"):
        setup._raise_if_resource_error(None)
    with pytest.raises(RuntimeError, match="bad image"):
        setup._raise_if_resource_error(Error())


def test_is_webhook_server_process_matches_name_and_command():
    assert setup._is_webhook_server_process("webhook-server-abc123", "")
    assert setup._is_webhook_server_process("anything", "python3 -m uvicorn app:app")
    assert not setup._is_webhook_server_process("ant-poll-1", "node -v")
    assert not setup._is_webhook_server_process("probe-9", "python3 -c import app")
    assert not setup._is_webhook_server_process("", "")


async def test_restart_kills_stale_server_and_passes_current_env(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(setup.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(setup, "ORCHESTRATOR_KEEPALIVE_TIMEOUT", 123)

    existing = [
        _FakeProc(name="webhook-server-old1", command="python3 -m uvicorn app:app"),
        _FakeProc(name="probe-1", command="python3 -c import app"),  # must NOT be killed
    ]
    sbx = _FakeSandbox(existing)
    env_map = {
        "ANTHROPIC_ENVIRONMENT_ID": "env_x",
        "ANTHROPIC_ENVIRONMENT_KEY": "sk-ant-oat01-x",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY": "whsec_x",
        "BL_API_KEY": "bl_x",
    }

    name = await setup._restart_webhook_server(sbx, env_map)

    # the stale server was killed; the unrelated probe was left alone
    assert sbx.process.killed == ["webhook-server-old1"]
    # exactly one new server started, with a unique webhook-server-* name
    assert len(sbx.process.execed) == 1
    spec = sbx.process.execed[0]
    assert spec["name"] == name
    assert name.startswith("webhook-server-")
    # the current env -- including the signing key -- is passed at the process level
    assert spec["env"] == env_map
    assert spec["env"]["ANTHROPIC_WEBHOOK_SIGNING_KEY"] == "whsec_x"
    assert spec["wait_for_completion"] is False
    assert spec["keep_alive"] is True
    assert spec["timeout"] == 123
    assert "uvicorn app:app" in spec["command"]


async def test_restart_tolerates_list_failure_on_fresh_sandbox(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(setup.asyncio, "sleep", _no_sleep)

    class _BoomProcessAPI(_FakeProcessAPI):
        async def list(self):
            raise RuntimeError("process API unavailable")

    sbx = _FakeSandbox([])
    sbx.process = _BoomProcessAPI([])
    # a list() failure (or a fresh sandbox with nothing to kill) still starts a server
    name = await setup._restart_webhook_server(sbx, {"BL_API_KEY": "x"})
    assert sbx.process.killed == []
    assert len(sbx.process.execed) == 1
    assert name.startswith("webhook-server-")
