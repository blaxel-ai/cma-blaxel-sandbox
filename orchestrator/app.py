"""
Claude Managed Agents (self-hosted) orchestrator.

This runs INSIDE a Blaxel sandbox, exposed on a public preview URL that is
registered as the Anthropic webhook target.

Design:
  - On `session.status_run_started`, spawn ONE worker sandbox and return 200.
  - The orchestrator never polls/claims/babysits the work queue.
  - The worker self-claims by running `ant beta:worker poll` and exits on idle;
    a TTL auto-cleans the worker sandbox. No orchestrator-side supervision.

Running in a sandbox (rather than as a long-lived HTTP service) means the
process persists across scale-to-zero/standby with memory intact and resumes on
the inbound webhook, so background spawning is safe and there is no
execution-time ceiling on the handler.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from logging import getLogger
from uuid import uuid4

from anthropic import AsyncAnthropic
from blaxel.core import SandboxInstance
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = getLogger("cma-orchestrator")

# Anthropic side: verify the webhook, identify the environment.
environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
environment_key = os.environ["ANTHROPIC_ENVIRONMENT_KEY"]
client = AsyncAnthropic(
    auth_token=environment_key,
    webhook_key=os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY"),
)

# Worker config (all overridable via env).
worker_image = os.environ.get("BLAXEL_WORKER_IMAGE", "sandbox/cma-worker:latest")
worker_ttl = os.environ.get("BLAXEL_WORKER_TTL", "600s")
# Keep --max-idle generous enough to span the agent's reasoning between tool
# calls; the worker exits once the queue is quiet for this long.
worker_max_idle = os.environ.get("ANT_MAX_IDLE", "60s")
worker_region = os.environ.get("BL_REGION")

# Keep a reference to each spawn task so it is not garbage-collected mid-flight
# and so failures are logged rather than silently swallowed.
_spawn_tasks: set[asyncio.Task] = set()


async def _spawn_worker(session_id: str) -> None:
    """Create (or revive) a self-claiming worker sandbox for a session.

    The worker polls the environment queue itself, so the orchestrator never
    touches the queue. The sandbox is named per session and the name is sanitized
    (Blaxel names allow only lowercase alphanumerics + hyphens).

    `session.status_run_started` fires once per run/turn, not once per session.
    Keying the sandbox on the session id makes create idempotent across turns:
    the first turn creates it; later turns reuse the same sandbox and just
    (re)start the poller below, needed when the previous turn's poller already
    exited on --max-idle. Overlapping pollers are harmless because the queue
    hands each work item to a single claimer. The TTL auto-deletes the sandbox,
    so there is no orchestrator-side delete to babysit; keep the TTL larger than
    the expected gap between a session's turns.
    """
    safe_id = session_id.replace("_", "-").lower()
    name = f"cma-worker-{safe_id[:40]}"
    envs = [
        {"name": "ANTHROPIC_ENVIRONMENT_ID", "value": environment_id},
        {"name": "ANTHROPIC_ENVIRONMENT_KEY", "value": environment_key},
    ]
    if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
        envs.append({"name": "ANTHROPIC_BASE_URL", "value": base_url})

    spec = {
        "name": name,
        "image": worker_image,
        "memory": 4096,
        "ttl": worker_ttl,        # self-cleaning: no orchestrator-side delete
        "envs": envs,
    }
    if worker_region:
        spec["region"] = worker_region
    worker = await SandboxInstance.create_if_not_exists(spec)
    logger.info("worker %s created for session %s", name, session_id)

    # Wait for the in-sandbox API to accept commands, then start the poller
    # detached. `poll` self-claims the queued session, runs its tool calls,
    # and shuts down after --max-idle. We do NOT wait_for_completion and we do
    # NOT delete; the worker owns its own lifecycle.
    for attempt in range(45):
        try:
            await worker.process.exec({
                "name": "ant-poll",
                "command": f"ant beta:worker poll --workdir /workspace --unrestricted-paths --max-idle {worker_max_idle}",
                "wait_for_completion": False,
            })
            logger.info("worker %s polling (session %s)", name, session_id)
            return
        except Exception:
            await asyncio.sleep(2)
    logger.error("worker %s never accepted the poll command", name)


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _spawn_tasks.add(task)
    task.add_done_callback(lambda t: (_spawn_tasks.discard(t), t.exception() and logger.error("spawn failed: %r", t.exception())))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("orchestrator up (env %s, worker image %s)", environment_id, worker_image)
    yield
    logger.info("orchestrator shutting down")


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    try:
        event = client.beta.webhooks.unwrap(raw.decode(), headers=dict(request.headers))
    except Exception:
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    if event.data.type == "session.status_run_started":
        # event.data.id is the id of the resource that triggered the event, the
        # session. The worker self-claims, so this is only used to name the
        # sandbox; the uuid fallback is defensive and normally unused.
        session_id = getattr(event.data, "id", None) or f"sesn-{uuid4().hex[:12]}"
        # Spawn fast, return immediately. No claim, no wait.
        _track(_spawn_worker(session_id))

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
