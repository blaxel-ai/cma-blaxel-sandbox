"""
One-time setup for the all-sandbox CMA orchestrator.

Creates the orchestrator sandbox from the pushed `sandbox/cma-orchestrator`
image, starts the FastAPI webhook server inside it, exposes a public preview
URL, and prints the webhook URL to register in the Anthropic Console.

Run locally with your workspace selected:

    export BL_WORKSPACE=<your-workspace>
    export BL_API_KEY=...                          # to create sandboxes
    export ANTHROPIC_ENVIRONMENT_ID=env_...
    export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
    export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_... # after the webhook exists
    python3 setup.py

The orchestrator needs BL_API_KEY + BL_WORKSPACE in its own env so the
in-sandbox SDK can spawn worker sandboxes. The uvicorn process survives
scale-to-zero/standby (verified), so an inbound webhook resumes it.
"""
import asyncio
import os
from uuid import uuid4

from blaxel.core import SandboxInstance

NAME = os.environ.get("ORCHESTRATOR_NAME", "cma-orchestrator-app")
IMAGE = os.environ.get("ORCHESTRATOR_IMAGE", "sandbox/cma-orchestrator:latest")
PORT = int(os.environ.get("ORCHESTRATOR_PORT", "8000"))

PASSTHROUGH = [
    "ANTHROPIC_ENVIRONMENT_ID",
    "ANTHROPIC_ENVIRONMENT_KEY",
    "ANTHROPIC_WEBHOOK_SIGNING_KEY",
    "ANTHROPIC_BASE_URL",
    "BL_API_KEY",
    "BL_WORKSPACE",
    "BL_REGION",
    "BLAXEL_WORKER_IMAGE",
    "BLAXEL_WORKER_TTL",
    "ANT_MAX_IDLE",
    "ANT_RESTART_COOLDOWN",
    "ANT_KEEPALIVE_TIMEOUT",
]


def _is_webhook_server_process(name: str, command: str) -> bool:
    """True for a previously-started orchestrator web server we should replace.

    On a re-run we kill the old uvicorn so the TCP port frees and its stale config
    (e.g. a missing webhook signing key from the first run) is gone.
    """
    return (name or "").startswith("webhook-server") or "uvicorn" in (command or "")


async def _restart_webhook_server(sbx, env_map: dict) -> str:
    """(Re)start the FastAPI webhook server with the CURRENT env, in place.

    `create_if_not_exists` returns an existing sandbox WITHOUT applying env
    changes, and a server already running baked its config at import time. So when
    you add ANTHROPIC_WEBHOOK_SIGNING_KEY and re-run setup, the old server would
    keep rejecting deliveries with 503. We kill the old server and start a fresh
    one with the env passed at the PROCESS level (which merges onto the sandbox
    env), so the current keys reach the server even though the sandbox env is
    stale. We never recreate the sandbox, so its public preview URL -- already
    registered as the Anthropic webhook -- stays stable.
    """
    try:
        for proc in await sbx.process.list():
            if _is_webhook_server_process(getattr(proc, "name", ""), getattr(proc, "command", "")):
                try:
                    await sbx.process.kill(getattr(proc, "name", "") or getattr(proc, "pid", ""))
                except Exception:
                    pass
    except Exception:
        pass
    await asyncio.sleep(2)  # let the TCP port free before the new server binds
    process_name = f"webhook-server-{uuid4().hex[:8]}"
    await sbx.process.exec({
        "name": process_name,
        "command": f"python3 -m uvicorn app:app --host 0.0.0.0 --port {PORT}",
        "working_dir": "/app",
        "wait_for_completion": False,
        "env": env_map,  # process-level env delivers the current keys (incl. the
                         # webhook signing key) even when the sandbox env is stale
    })
    return process_name


async def main() -> None:
    env_map = {k: os.environ[k] for k in PASSTHROUGH if os.environ.get(k)}
    envs = [{"name": k, "value": v} for k, v in env_map.items()]
    for required in ("ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY", "BL_WORKSPACE"):
        if not os.environ.get(required):
            raise SystemExit(f"missing required env: {required}")

    spec = {
        "name": NAME,
        "image": IMAGE,
        "memory": 2048,
        "envs": envs,
    }
    if region := os.environ.get("BL_REGION"):
        spec["region"] = region
    sbx = await SandboxInstance.create_if_not_exists(spec)
    print(f"orchestrator sandbox '{NAME}' created; waiting for readiness...")

    for attempt in range(45):
        try:
            await sbx.process.exec({"name": f"probe-{uuid4().hex[:8]}", "command": "python3 -c \"import app\"", "working_dir": "/app", "wait_for_completion": True})
            break
        except Exception:
            await asyncio.sleep(2)
    else:
        raise SystemExit("orchestrator never became ready (could not import app in the sandbox); check the image build and that the env vars were passed")

    # Start (or, on a re-run, replace) the webhook server detached. It persists
    # across standby/resume; a re-run swaps in one carrying the current env so a
    # freshly added signing key actually reaches the server.
    process_name = await _restart_webhook_server(sbx, env_map)
    print(f"webhook server started ({process_name})")
    await asyncio.sleep(4)

    preview = await sbx.previews.create_if_not_exists({
        "metadata": {"name": "webhook"},
        "spec": {"port": PORT, "public": True},
    })
    url = getattr(preview.spec, "url", None)
    if url and url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith("http"):
        url = "https://" + url
    if os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY"):
        print("\nsigning key: configured — deliveries will be verified")
    else:
        print("\nsigning key: NOT set — deliveries will be rejected with 503 until you")
        print("re-run setup with ANTHROPIC_WEBHOOK_SIGNING_KEY exported")
    print("\n=== Register this as the Anthropic webhook URL ===")
    print(f"  {url}/webhook")
    print("Subscribe it to `session.status_run_started`, then export the")
    print("signing key as ANTHROPIC_WEBHOOK_SIGNING_KEY and re-run setup.")


if __name__ == "__main__":
    asyncio.run(main())
