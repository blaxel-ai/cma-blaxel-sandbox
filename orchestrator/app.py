"""
Claude Managed Agents (self-hosted) orchestrator.

This runs INSIDE a Blaxel sandbox, exposed on a public preview URL that is
registered as the Anthropic webhook target.

Design:
  - On `session.status_run_started`, spawn ONE worker sandbox and return 200.
  - The orchestrator never polls/claims/babysits the work queue.
  - The worker self-claims by running `ant beta:worker poll` and exits after
    queue idle; TTL is a max-age cleanup backstop. No orchestrator-side
    supervision.

Running in a sandbox gives the webhook a public preview URL and lets the
process resume with the sandbox on the next inbound webhook.
"""
import asyncio
import os
import re
import time
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
# `ttl` is a max-age from creation: Blaxel deletes the sandbox this long after it is
# created, regardless of activity. Units are m/h/d/w (NOT seconds). Keep it well above
# a session's expected length so it never deletes a worker mid-run -- it's a cleanup
# backstop, not idle-based. For idle-based cleanup, use a `ttl-idle` lifecycle policy
# instead (see Blaxel docs; not exposed in the installed SDK build).
worker_ttl = os.environ.get("BLAXEL_WORKER_TTL", "2h")
# Keep --max-idle generous enough to span the agent's reasoning between tool
# calls; the worker exits once the queue is quiet for this long.
worker_max_idle = os.environ.get("ANT_MAX_IDLE", "60s")
# keep_alive holds the worker sandbox active while the poller runs. Without it the
# sandbox standbys ~15s after spawn (the poller only makes outbound calls, so there
# is no inbound connection to hold it active) and the poll loop freezes mid-session.
# Bounds a stuck session; set ANT_KEEPALIVE_TIMEOUT=0 to run until natural exit.
worker_keepalive_timeout = int(os.environ.get("ANT_KEEPALIVE_TIMEOUT", "3600"))
worker_region = os.environ.get("BL_REGION")


def _worker_name(session_id: str) -> str:
    """Derive a valid Blaxel sandbox name from an Anthropic session id.

    Blaxel names allow only lowercase alphanumerics and hyphens. Session ids look
    like `sesn_01Ab...`; the underscore (and any other unexpected char) maps to a
    hyphen, and the id is bounded so the worker name stays short.
    """
    safe_id = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    return f"cma-worker-{safe_id[:40]}"


def _duration_to_seconds(value: str, default: int) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value.lower())
    if not match:
        return default
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[
        match.group(2)
    ]
    return amount * multiplier


# `session.status_run_started` can be delivered more than once (webhook retries)
# and can also arrive while the previous poller is intentionally still alive for
# --max-idle. Suppress duplicate starts for roughly that idle window. If a later
# turn arrives after the previous poller idled out, a fresh process is started.
worker_restart_cooldown = _duration_to_seconds(
    os.environ.get("ANT_RESTART_COOLDOWN", worker_max_idle),
    default=60,
)
_session_locks_guard = asyncio.Lock()
_session_locks: dict[str, asyncio.Lock] = {}
_session_last_started_at: dict[str, float] = {}


async def _lock_for_session(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        return _session_locks.setdefault(session_id, asyncio.Lock())


def _prune_session_state(now: float) -> None:
    # Bound memory in long-lived orchestrators. Keep state for several duplicate
    # delivery windows but drop old, unlocked sessions.
    max_age = max(worker_restart_cooldown * 4, 300)
    for session_id, started_at in list(_session_last_started_at.items()):
        if now - started_at <= max_age:
            continue
        lock = _session_locks.get(session_id)
        if lock and lock.locked():
            continue
        _session_last_started_at.pop(session_id, None)
        _session_locks.pop(session_id, None)


async def _spawn_worker_once(session_id: str) -> bool:
    lock = await _lock_for_session(session_id)
    async with lock:
        now = time.monotonic()
        _prune_session_state(now)
        if started_at := _session_last_started_at.get(session_id):
            age = now - started_at
            if age < worker_restart_cooldown:
                logger.info(
                    "worker start skipped for session %s; poller started %.1fs ago",
                    session_id,
                    age,
                )
                return True

        started = await _spawn_worker(session_id)
        if started:
            _session_last_started_at[session_id] = time.monotonic()
        return started


async def _spawn_worker(session_id: str) -> bool:
    """Create (or revive) a self-claiming worker sandbox for a session.

    The worker polls the environment queue itself, so the orchestrator never
    touches the queue. The sandbox is named per session and the name is sanitized
    (Blaxel names allow only lowercase alphanumerics + hyphens).

    `session.status_run_started` fires once per run/turn, not once per session.
    Keying the sandbox on the session id makes create idempotent across turns:
    the first turn creates it; later turns reuse the same sandbox and just
    (re)start the poller below, needed when the previous turn's poller already
    exited on --max-idle. Overlapping pollers are harmless because the queue
    hands each work item to a single claimer. The TTL (max-age from creation)
    eventually removes the sandbox as a cleanup backstop, so there is no
    orchestrator-side delete to babysit; keep it well above a session's length.
    """
    name = _worker_name(session_id)
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
        "ttl": worker_ttl,        # max-age cleanup backstop: no orchestrator-side delete
        "envs": envs,
    }
    if worker_region:
        spec["region"] = worker_region
    worker = await SandboxInstance.create_if_not_exists(spec)
    logger.info("worker %s created for session %s", name, session_id)

    # Wait for the in-sandbox API to accept commands, then start the poller
    # detached. `poll` self-claims the queued session, runs its tool calls,
    # and shuts down after --max-idle. keep_alive holds the sandbox active for
    # the whole session; without it the sandbox standbys ~15s after spawn and
    # the poll loop freezes mid-session. We do NOT wait_for_completion and we do
    # NOT delete; the worker owns its own lifecycle (the TTL handles max-age
    # cleanup after sandbox creation).
    #
    # Give each poller launch a unique process name. Process records survive
    # completion, so reusing `ant-poll` makes later turns or webhook retries fail
    # with process-name conflicts even though starting a new poller is safe.
    # `_spawn_worker_once` suppresses duplicate webhook deliveries during the
    # max-idle window, while this unique name lets a later turn restart cleanly.
    # Bounded so a cold spawn returns within Anthropic's webhook delivery timeout
    # (calibrate during the validation run); on timeout the delivery is retried and
    # create_if_not_exists is idempotent, so the retry finishes fast.
    process_name = f"ant-poll-{uuid4().hex[:8]}"
    for attempt in range(20):
        try:
            await worker.process.exec({
                "name": process_name,
                "command": f"ant beta:worker poll --workdir /workspace --max-idle {worker_max_idle}",
                "wait_for_completion": False,
                "keep_alive": True,
                "timeout": worker_keepalive_timeout,
            })
            logger.info(
                "worker %s polling as %s (session %s)",
                name,
                process_name,
                session_id,
            )
            return True
        except Exception as exc:
            if attempt in (0, 4, 9, 19):
                logger.warning(
                    "worker %s poll start attempt %d failed: %r",
                    name,
                    attempt + 1,
                    exc,
                )
            await asyncio.sleep(2)
    logger.error("worker %s never accepted poll command %s", name, process_name)
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("orchestrator up (env %s, worker image %s)", environment_id, worker_image)
    yield
    logger.info("orchestrator shutting down")


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY"):
        # No signing key yet: say so plainly instead of masking it as a signature
        # failure. Deliveries can't be verified until setup.py is re-run with
        # ANTHROPIC_WEBHOOK_SIGNING_KEY set.
        return JSONResponse({"error": "webhook signing key not configured"}, status_code=503)
    try:
        event = client.beta.webhooks.unwrap(raw.decode(), headers=dict(request.headers))
    except Exception:
        return JSONResponse({"error": "signature verification failed"}, status_code=401)

    if event.data.type == "session.status_run_started":
        # event.data.id is the id of the resource that triggered the event, the
        # session. The worker self-claims, so this is only used to name the
        # sandbox; the uuid fallback is defensive and normally unused.
        session_id = getattr(event.data, "id", None) or f"sesn-{uuid4().hex[:12]}"
        # Spawn synchronously. Holding the inbound webhook connection open for the
        # spawn keeps THIS orchestrator sandbox active, so it can't standby (and
        # freeze) mid-spawn. Once started, the worker holds itself alive via process
        # keep_alive, so the orchestrator can safely standby after we return. If a
        # cold spawn exceeds Anthropic's webhook delivery timeout, the delivery is
        # retried and create_if_not_exists is idempotent; _spawn_worker_once also
        # collapses duplicate deliveries while the previous poller is still alive.
        if not await _spawn_worker_once(session_id):
            return JSONResponse({"error": "worker poller did not start"}, status_code=503)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
