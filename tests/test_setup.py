"""Tests for setup.py's in-place webhook-server restart (the P0-1 re-run fix).

The bug it locks: re-running setup after adding ANTHROPIC_WEBHOOK_SIGNING_KEY
left the old server running with stale config (and create_if_not_exists doesn't
update the sandbox env), so deliveries kept being rejected with 503. The fix
kills the old server and starts a fresh one with the current env passed at the
process level, without recreating the sandbox (so the preview URL is stable).
"""
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
