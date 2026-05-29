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
    python setup.py

The orchestrator needs BL_API_KEY + BL_WORKSPACE in its own env so the
in-sandbox SDK can spawn worker sandboxes. The uvicorn process survives
scale-to-zero/standby (verified), so an inbound webhook resumes it.
"""
import asyncio
import os

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
]


async def main() -> None:
    envs = [{"name": k, "value": os.environ[k]} for k in PASSTHROUGH if os.environ.get(k)]
    for required in ("ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY"):
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
            await sbx.process.exec({"name": f"probe{attempt}", "command": "python3 -c \"import app\"", "working_dir": "/app", "wait_for_completion": True})
            break
        except Exception:
            await asyncio.sleep(2)
    else:
        raise SystemExit("orchestrator never became ready (could not import app in the sandbox); check the image build and that the env vars were passed")

    # Start the webhook server detached. It persists across standby/resume.
    await sbx.process.exec({
        "name": "webhook-server",
        "command": f"uvicorn app:app --host 0.0.0.0 --port {PORT}",
        "working_dir": "/app",
        "wait_for_completion": False,
    })
    print("webhook server started")
    await asyncio.sleep(4)

    preview = await sbx.previews.create({
        "metadata": {"name": "webhook"},
        "spec": {"port": PORT, "public": True},
    })
    url = getattr(preview.spec, "url", None)
    if url and url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith("http"):
        url = "https://" + url
    print("\n=== Register this as the Anthropic webhook URL ===")
    print(f"  {url}/webhook")
    print("Subscribe it to `session.status_run_started`, then export the")
    print("signing key as ANTHROPIC_WEBHOOK_SIGNING_KEY and re-run setup.")


if __name__ == "__main__":
    asyncio.run(main())
