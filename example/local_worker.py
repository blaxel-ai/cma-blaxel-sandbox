"""Local Blaxel worker launcher for examples.

This mirrors the orchestrator's true per-session path: claim exact CMA work with
the SDK, then run that work in the matching Blaxel sandbox with
`ant beta:worker run`.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from uuid import uuid4

from anthropic import AsyncAnthropic
from blaxel.core import SandboxInstance

WORKER_IMAGE = os.environ.get("BLAXEL_WORKER_IMAGE", "sandbox/cma-worker:latest")
WORKER_MAX_IDLE = os.environ.get("ANT_MAX_IDLE", "60s")
WORKER_TTL = os.environ.get("BLAXEL_WORKER_TTL", "2h")
KEEPALIVE_TIMEOUT = int(os.environ.get("ANT_KEEPALIVE_TIMEOUT", "3600"))
DISPATCHER_RECLAIM_MS = int(os.environ.get("ANT_DISPATCHER_RECLAIM_MS", "30000"))
DISPATCHER_DEBOUNCE_MS = int(os.environ.get("ANT_DISPATCHER_DEBOUNCE_MS", "250"))
_work_ids_in_flight: set[str] = set()


@dataclass
class DispatchResult:
    work_id: str
    session_id: str
    sandbox_name: str
    process_name: str
    worker: object


def worker_name(session_id: str) -> str:
    safe_id = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    return f"cma-worker-{safe_id[:40]}"


def process_name(work_id: str) -> str:
    safe_id = re.sub(r"[^a-z0-9-]", "-", work_id.lower())
    return f"ant-run-{safe_id[:48]}"


def work_session_id(work) -> str | None:
    data = getattr(work, "data", None)
    if getattr(data, "type", None) != "session":
        return None
    return getattr(data, "id", None)


async def wait_for_worker_ready(worker, sandbox_name: str, attempts: int = 45) -> None:
    last_error = None
    for _ in range(attempts):
        try:
            await worker.process.exec({
                "name": f"probe-{uuid4().hex[:8]}",
                "command": "node -v",
                "wait_for_completion": True,
            })
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(2)
    raise RuntimeError(f"worker {sandbox_name} never became ready: {last_error!r}")


async def ready_worker_for_session(session_id: str):
    sandbox_name = worker_name(session_id)
    spec = {
        "name": sandbox_name,
        "image": WORKER_IMAGE,
        "memory": 4096,
        "ttl": WORKER_TTL,
    }
    if region := os.environ.get("BL_REGION"):
        spec["region"] = region

    worker = await SandboxInstance.create_if_not_exists(spec)
    await wait_for_worker_ready(worker, sandbox_name)
    return worker


async def stop_work(client: AsyncAnthropic, work, *, force: bool = True) -> None:
    await client.beta.environments.work.stop(
        work.id,
        environment_id=work.environment_id,
        force=force,
    )


def mark_work_in_flight(work_id: str) -> bool:
    if work_id in _work_ids_in_flight:
        return False
    _work_ids_in_flight.add(work_id)
    return True


async def dispatch_work_item(
    client: AsyncAnthropic,
    work,
    *,
    label: str = "local-worker",
    prepared_worker=None,
) -> DispatchResult | None:
    session_id = work_session_id(work)
    if not session_id:
        await stop_work(client, work, force=True)
        print(f"[{label}] force-stopped unsupported work {work.id}")
        return None

    if not mark_work_in_flight(work.id):
        print(f"[{label}] {work.id} is already dispatching; skipping duplicate claim")
        return None

    sandbox_name = worker_name(session_id)
    try:
        worker = prepared_worker or await ready_worker_for_session(session_id)
    except Exception:
        _work_ids_in_flight.discard(work.id)
        await stop_work(client, work, force=True)
        raise

    proc_name = process_name(work.id)
    env = {
        "ANTHROPIC_WORK_ID": work.id,
        "ANTHROPIC_SESSION_ID": session_id,
        "ANTHROPIC_ENVIRONMENT_ID": work.environment_id,
        "ANTHROPIC_ENVIRONMENT_KEY": os.environ["ANTHROPIC_ENVIRONMENT_KEY"],
    }
    if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = base_url

    try:
        # `ant beta:worker run` must own the first heartbeat for this work item.
        # The host readies the sandbox before claiming so this handoff is short.
        await worker.process.exec({
            "name": proc_name,
            "command": f"ant beta:worker run --workdir /workspace --max-idle {WORKER_MAX_IDLE}",
            "wait_for_completion": False,
            "keep_alive": True,
            "timeout": KEEPALIVE_TIMEOUT,
            "env": env,
        })
    except Exception:
        _work_ids_in_flight.discard(work.id)
        await stop_work(client, work, force=True)
        raise

    print(f"[{label}] {sandbox_name} is running {work.id} as {proc_name}")
    return DispatchResult(
        work_id=work.id,
        session_id=session_id,
        sandbox_name=sandbox_name,
        process_name=proc_name,
        worker=worker,
    )


async def dispatch_available_work(
    *,
    label: str = "local-worker",
    prepared_workers: dict[str, object] | None = None,
) -> list[DispatchResult]:
    prepared_workers = prepared_workers or {}
    client = AsyncAnthropic(auth_token=os.environ["ANTHROPIC_ENVIRONMENT_KEY"])
    if DISPATCHER_DEBOUNCE_MS > 0:
        await asyncio.sleep(DISPATCHER_DEBOUNCE_MS / 1000)
    try:
        page = await client.beta.environments.work.list(
            os.environ["ANTHROPIC_ENVIRONMENT_ID"],
            limit=50,
        )
        active_session_ids = {
            session_id
            for work in (getattr(page, "data", None) or [])
            if getattr(work, "state", None) != "stopped"
            if (session_id := work_session_id(work))
        }
    except Exception:
        active_session_ids = set()
    for session_id in sorted(active_session_ids - set(prepared_workers)):
        prepared_workers[session_id] = await ready_worker_for_session(session_id)
    results: list[DispatchResult] = []
    async for work in client.beta.environments.work.poller(
        environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
        environment_key=os.environ["ANTHROPIC_ENVIRONMENT_KEY"],
        worker_id=f"{label}-{uuid4().hex[:8]}",
        block_ms=999,
        reclaim_older_than_ms=DISPATCHER_RECLAIM_MS,
        drain=True,
        auto_stop=False,
    ):
        session_id = work_session_id(work)
        result = await dispatch_work_item(
            client,
            work,
            label=label,
            prepared_worker=prepared_workers.get(session_id) if session_id else None,
        )
        if result:
            results.append(result)
    return results


async def dispatch_until_session_work(
    session_id: str,
    *,
    label: str = "local-worker",
    timeout_s: float = 30.0,
) -> DispatchResult:
    prepared_worker = await ready_worker_for_session(session_id)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        results = await dispatch_available_work(
            label=label,
            prepared_workers={session_id: prepared_worker},
        )
        for result in results:
            if result.session_id == session_id:
                return result
        await asyncio.sleep(1)
    raise RuntimeError(f"no claimed work appeared for session {session_id} within {timeout_s:.0f}s")
