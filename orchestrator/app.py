"""
Claude Managed Agents (self-hosted) orchestrator.

This runs INSIDE a Blaxel sandbox, exposed on a public preview URL that is
registered as the Anthropic webhook target.

Design:
  - On `session.status_run_started`, schedule dispatch and return 200 quickly.
  - The background dispatcher readies the session sandbox before claiming work.
  - For each claimed session work item, start one Blaxel worker sandbox process
    bound to that exact work id and session id.
  - The worker runs `ant beta:worker run`; it heartbeats and stops the claimed
    work item itself. TTL is a max-age cleanup backstop.

Running in a sandbox gives the webhook a public preview URL and lets the
process resume with the sandbox on the next inbound webhook.
"""
import asyncio
import os
import re
from contextlib import asynccontextmanager
from logging import getLogger
from uuid import uuid4

from anthropic import AsyncAnthropic
from blaxel.core import SandboxInstance
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from blaxel_features import apply_worker_features

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
# `ant beta:worker run --max-idle` stops after the session goes idle with
# stop_reason=end_turn. 0 disables the timeout.
worker_max_idle = os.environ.get("ANT_MAX_IDLE", "60s")
# keep_alive holds the worker sandbox active while `ant run` serves the session.
# Without it the sandbox can standby while the worker is making outbound calls.
# Bounds a stuck session; set ANT_KEEPALIVE_TIMEOUT=0 to run until natural exit.
worker_keepalive_timeout = int(os.environ.get("ANT_KEEPALIVE_TIMEOUT", "3600"))
worker_region = os.environ.get("BL_REGION")
dispatcher_poll_block_ms = int(os.environ.get("ANT_DISPATCHER_POLL_BLOCK_MS", "999"))
dispatcher_reclaim_ms = int(os.environ.get("ANT_DISPATCHER_RECLAIM_MS", "30000"))
dispatcher_debounce_ms = int(os.environ.get("ANT_DISPATCHER_DEBOUNCE_MS", "250"))
dispatcher_worker_id = os.environ.get(
    "ANTHROPIC_DISPATCHER_WORKER_ID",
    f"cma-orchestrator-{re.sub(r'[^a-z0-9-]', '-', (os.environ.get('BLAXEL_SANDBOX_NAME') or os.environ.get('HOSTNAME') or 'local').lower())[:48]}",
)
worker_readiness_attempts = int(os.environ.get("BLAXEL_WORKER_READY_ATTEMPTS", "45"))
worker_readiness_sleep = float(os.environ.get("BLAXEL_WORKER_READY_SLEEP", "2"))
worker_run_attempts = int(os.environ.get("ANT_RUN_START_ATTEMPTS", "10"))

_dispatcher_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()
_scheduled_session_ids: set[str] = set()
_worker_ready_tasks: dict[str, asyncio.Task] = {}
_work_ids_in_flight: set[str] = set()


def _worker_name(session_id: str) -> str:
    """Derive a valid Blaxel sandbox name from an Anthropic session id.

    Blaxel names allow only lowercase alphanumerics and hyphens. Session ids look
    like `sesn_01Ab...`; the underscore (and any other unexpected char) maps to a
    hyphen, and the id is bounded so the worker name stays short.
    """
    safe_id = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    return f"cma-worker-{safe_id[:40]}"


def _process_name(work_id: str, unique_suffix: str | None = None) -> str:
    safe_id = re.sub(r"[^a-z0-9-]", "-", work_id.lower())
    suffix = unique_suffix or uuid4().hex[:8]
    return f"ant-run-{safe_id[:40]}-{suffix}"


def _duration_to_seconds(value: str, default: int) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value.lower())
    if not match:
        return default
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[
        match.group(2)
    ]
    return amount * multiplier


def _work_session_id(work) -> str | None:
    data = getattr(work, "data", None)
    if getattr(data, "type", None) != "session":
        return None
    return getattr(data, "id", None)


async def _stop_work(work, *, force: bool = True) -> None:
    try:
        await client.beta.environments.work.stop(
            work.id,
            environment_id=work.environment_id,
            force=force,
        )
    except Exception as exc:
        logger.warning("failed to stop work %s: %r", getattr(work, "id", "?"), exc)


async def _wait_for_worker_ready(worker, name: str) -> bool:
    last_error = None
    for attempt in range(worker_readiness_attempts):
        try:
            await worker.process.exec({
                "name": f"probe-{uuid4().hex[:8]}",
                "command": "node -v",
                "wait_for_completion": True,
            })
            return True
        except Exception as exc:
            last_error = exc
            if attempt in (0, 4, 14, worker_readiness_attempts - 1):
                logger.warning(
                    "worker %s readiness attempt %d failed: %r",
                    name,
                    attempt + 1,
                    exc,
                )
            await asyncio.sleep(worker_readiness_sleep)
    logger.error("worker %s never became ready: %r", name, last_error)
    return False


async def _ensure_worker_ready(session_id: str):
    name = _worker_name(session_id)
    spec = {
        "name": name,
        "image": worker_image,
        "memory": 4096,
        "ttl": worker_ttl,        # max-age cleanup backstop: no orchestrator-side delete
    }
    if worker_region:
        spec["region"] = worker_region
    spec = await apply_worker_features(spec, session_id=session_id, region=worker_region)

    worker = await SandboxInstance.create_if_not_exists(spec)
    logger.info("worker %s readying for session %s", name, session_id)
    if not await _wait_for_worker_ready(worker, name):
        raise RuntimeError(f"worker {name} never became ready")
    return worker


async def _worker_ready_for_session(session_id: str):
    task = _worker_ready_tasks.get(session_id)
    if task is None:
        task = asyncio.create_task(
            _ensure_worker_ready(session_id),
            name=f"ready-{_worker_name(session_id)}",
        )
        _worker_ready_tasks[session_id] = task

        def _clear_ready_task(done: asyncio.Task, sid: str = session_id) -> None:
            if _worker_ready_tasks.get(sid) is done:
                _worker_ready_tasks.pop(sid, None)

        task.add_done_callback(_clear_ready_task)
    return await task


async def _ready_workers_for_sessions(session_ids: set[str]) -> dict[str, object]:
    """Ready currently-pending session sandboxes before the queue is claimed."""
    workers: dict[str, object] = {}
    if not session_ids:
        return workers
    ordered = sorted(session_ids)
    results = await asyncio.gather(
        *(_worker_ready_for_session(session_id) for session_id in ordered),
        return_exceptions=True,
    )
    for session_id, result in zip(ordered, results):
        if isinstance(result, Exception):
            logger.error(
                "failed to ready worker sandbox %s before claim: %r",
                _worker_name(session_id),
                result,
            )
            continue
        workers[session_id] = result
    return workers


async def _queued_work_session_ids() -> set[str]:
    """Best-effort read-only look at still-queued work before claiming it."""
    try:
        page = await client.beta.environments.work.list(environment_id, limit=50)
    except Exception as exc:
        logger.warning("failed to list queued work before claim: %r", exc)
        return set()
    session_ids: set[str] = set()
    for work in getattr(page, "data", None) or []:
        if getattr(work, "state", None) != "queued":
            continue
        if session_id := _work_session_id(work):
            session_ids.add(session_id)
    return session_ids


def _mark_work_in_flight(work_id: str) -> bool:
    if work_id in _work_ids_in_flight:
        return False
    _work_ids_in_flight.add(work_id)
    return True


async def _dispatch_work_item(work, *, prepared_worker=None) -> bool:
    """Launch one Blaxel worker process for one already-claimed CMA work item."""
    session_id = _work_session_id(work)
    if not session_id:
        logger.warning(
            "unsupported work %s type %s; force-stopping",
            getattr(work, "id", "?"),
            getattr(getattr(work, "data", None), "type", None),
        )
        await _stop_work(work, force=True)
        return True

    if not _mark_work_in_flight(work.id):
        logger.info("work %s is already dispatching; suppressing duplicate claim", work.id)
        return True

    try:
        worker = prepared_worker or await _worker_ready_for_session(session_id)
    except Exception as exc:
        logger.error(
            "failed to ready worker sandbox %s for work %s: %r",
            _worker_name(session_id),
            work.id,
            exc,
        )
        _work_ids_in_flight.discard(work.id)
        await _stop_work(work, force=True)
        return False

    name = _worker_name(session_id)
    process_name = _process_name(work.id)
    process_env = {
        "ANTHROPIC_WORK_ID": work.id,
        "ANTHROPIC_SESSION_ID": session_id,
        "ANTHROPIC_ENVIRONMENT_ID": getattr(work, "environment_id", environment_id),
        "ANTHROPIC_ENVIRONMENT_KEY": environment_key,
    }
    if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
        process_env["ANTHROPIC_BASE_URL"] = base_url

    # Do not heartbeat here. `ant beta:worker run` owns the first heartbeat for
    # the claimed item; a dispatcher-side heartbeat would change the expected
    # lease token and can make the stock worker lose the handoff. Keep this gap
    # short by readying the sandbox before the SDK claim and bounding retries.
    try:
        for attempt in range(worker_run_attempts):
            try:
                await worker.process.exec({
                    "name": process_name,
                    "command": f"ant beta:worker run --workdir /workspace --max-idle {worker_max_idle}",
                    "wait_for_completion": False,
                    "keep_alive": True,
                    "timeout": worker_keepalive_timeout,
                    "env": process_env,
                })
                logger.info(
                    "worker %s running %s for session %s",
                    name,
                    process_name,
                    session_id,
                )
                return True
            except Exception as exc:
                if attempt in (0, 4, 9, worker_run_attempts - 1):
                    logger.warning(
                        "worker %s ant run start attempt %d failed: %r",
                        name,
                        attempt + 1,
                        exc,
                    )
                await asyncio.sleep(2)
        logger.error("worker %s never accepted ant run command %s", name, process_name)
        await _stop_work(work, force=True)
        return False
    finally:
        # This set only protects the local claim-to-process-start handoff. Once
        # Blaxel accepts the process, `ant run` owns the lease. If it dies before
        # heartbeating, Anthropic reclaim must be able to re-deliver this work id.
        _work_ids_in_flight.discard(work.id)


async def _drain_and_dispatch_work(*, prepared_workers: dict[str, object] | None = None) -> bool:
    """Claim currently queued work and hand each item to its session sandbox."""
    prepared_workers = prepared_workers or {}
    async with _dispatcher_lock:
        dispatched = 0
        failed = 0
        try:
            async for work in client.beta.environments.work.poller(
                environment_id=environment_id,
                environment_key=environment_key,
                worker_id=dispatcher_worker_id,
                block_ms=dispatcher_poll_block_ms,
                reclaim_older_than_ms=dispatcher_reclaim_ms,
                drain=True,
                auto_stop=False,
            ):
                dispatched += 1
                session_id = _work_session_id(work)
                prepared_worker = prepared_workers.get(session_id) if session_id else None
                if session_id and prepared_worker is None:
                    logger.warning(
                        "claimed work %s for session %s without a pre-readied sandbox; "
                        "webhook delivery may have arrived after this drain started",
                        work.id,
                        session_id,
                    )
                if not await _dispatch_work_item(work, prepared_worker=prepared_worker):
                    failed += 1
        except Exception as exc:
            logger.error("dispatcher failed while claiming work: %r", exc)
            return False
        if dispatched == 0:
            logger.info("dispatcher found no queued work; another worker may have claimed it")
            return True
        logger.info("dispatcher handled %d work item(s), failures=%d", dispatched, failed)
        return failed == 0


async def _dispatch_for_session(session_id: str) -> None:
    """Prepare the session sandbox before claiming work, then drain quickly."""
    try:
        if dispatcher_debounce_ms > 0:
            await asyncio.sleep(dispatcher_debounce_ms / 1000)
        session_ids = set(_scheduled_session_ids)
        session_ids.add(session_id)
        session_ids.update(await _queued_work_session_ids())
        prepared_workers = await _ready_workers_for_sessions(session_ids)
        if session_id not in prepared_workers:
            raise RuntimeError(f"worker {_worker_name(session_id)} never became ready")
        ok = await _drain_and_dispatch_work(prepared_workers=prepared_workers)
        if not ok:
            logger.error("background dispatch for session %s had failures", session_id)
    except Exception as exc:
        logger.error("background dispatch for session %s failed before claim: %r", session_id, exc)
    finally:
        _scheduled_session_ids.discard(session_id)


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        try:
            done.result()
        except Exception as exc:
            logger.error("background dispatch task crashed: %r", exc)

    task.add_done_callback(_done)


def _schedule_dispatch_for_session(session_id: str) -> bool:
    if session_id in _scheduled_session_ids:
        logger.info("dispatch for session %s is already scheduled", session_id)
        return False
    _scheduled_session_ids.add(session_id)
    task = asyncio.create_task(
        _dispatch_for_session(session_id),
        name=f"dispatch-{_worker_name(session_id)}",
    )
    _track_background_task(task)
    return True


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
        session_id = getattr(event.data, "id", None)
        if not session_id:
            return JSONResponse({"error": "session id missing from webhook event"}, status_code=503)
        _schedule_dispatch_for_session(session_id)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
