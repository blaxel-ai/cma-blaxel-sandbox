#!/usr/bin/env python3
"""Inspect and clean up this cookbook's exact Anthropic and Blaxel resources."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic
from blaxel.core import SandboxInstance, VolumeInstance

from bootstrap import ENV_PATH, merged_env
from example.direct_dispatch import worker_name
from example.session_runtime import as_dict, usage_receipt
from scripts.cma_setup import DEFAULT_AGENT_MODEL


def _load_dotenv() -> None:
    for name, value in merged_env(dict(os.environ), ENV_PATH).items():
        if value:
            os.environ[name] = value


def _safe_name(value: str, max_len: int = 48) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
    return (safe or "session")[:max_len].strip("-") or "session"


def volume_name(session_id: str, prefix: str = "cma-workspace") -> str:
    safe_prefix = _safe_name(prefix, 32)
    return f"{safe_prefix}-{_safe_name(session_id)}"[:63].strip("-")


def is_missing_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not found" in message


def resource_is_terminal(resource: Any) -> bool:
    status = str(getattr(resource, "status", "") or as_dict(resource).get("status") or "").lower()
    return status in {"deleted", "terminated"}


async def wait_until_missing(
    get_resource: Callable[[str], Awaitable[Any]],
    name: str,
    *,
    timeout_seconds: float = 120,
    poll_seconds: float = 2,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            resource = await get_resource(name)
            if resource_is_terminal(resource):
                return
        except Exception as exc:
            if is_missing_error(exc):
                return
            raise
        await asyncio.sleep(poll_seconds)
    raise TimeoutError(f"resource {name!r} still exists after {timeout_seconds:g}s")


async def wait_until_session_settled(
    get_session: Callable[[str], Awaitable[Any]],
    session_id: str,
    *,
    timeout_seconds: float = 120,
    poll_seconds: float = 2,
) -> str:
    """Wait until an interrupted session is no longer actively executing."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session = as_dict(await get_session(session_id))
        state = str(session.get("status") or "unknown")
        if state in {"idle", "terminated"}:
            return state
        await asyncio.sleep(poll_seconds)
    raise TimeoutError(
        f"session {session_id!r} still active after {timeout_seconds:g}s"
    )


async def _all_blaxel(page: Any) -> list[Any]:
    if hasattr(page, "auto_paging_iter"):
        return [item async for item in page.auto_paging_iter()]
    return list(page)


def _blaxel_model(item: Any) -> Any:
    return getattr(item, "sandbox", None) or getattr(item, "volume", None) or item


def _blaxel_dict(item: Any) -> dict[str, Any]:
    model = _blaxel_model(item)
    if isinstance(model, dict):
        return model
    if hasattr(model, "to_dict"):
        return model.to_dict()
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return {"value": str(model)}


def _resource_name(item: Any) -> str:
    direct = getattr(item, "name", None)
    if direct:
        return str(direct)
    metadata = getattr(item, "metadata", None)
    return str(getattr(metadata, "name", "") or "")


def configuration_warnings(agent: Any | None) -> list[str]:
    if agent is None:
        return [
            "ANTHROPIC_AGENT_ID is not set; create the default agent before running sessions."
        ]
    model = (as_dict(agent).get("model") or {})
    model_id = model if isinstance(model, str) else model.get("id")
    if model_id != DEFAULT_AGENT_MODEL:
        return [
            f"configured agent uses {model_id or 'an unknown model'}; the cookbook default "
            f"is {DEFAULT_AGENT_MODEL}. Create a new agent and replace ANTHROPIC_AGENT_ID."
        ]
    return []


async def status() -> None:
    _load_dotenv()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID"):
        if not os.environ.get(name):
            raise SystemExit(f"missing required env: {name}")

    client = AsyncAnthropic()
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    environment, stats = await asyncio.gather(
        client.beta.environments.retrieve(environment_id),
        client.beta.environments.work.stats(environment_id),
    )
    agent = None
    if agent_id := os.environ.get("ANTHROPIC_AGENT_ID"):
        agent = await client.beta.agents.retrieve(agent_id)

    sessions_page = await client.beta.sessions.list(limit=25, order="desc", include_archived=True)
    sessions = []
    async for session in sessions_page:
        item = as_dict(session)
        if (item.get("metadata") or {}).get("cookbook") == "blaxel-cma":
            sessions.append(usage_receipt(item))
        if len(sessions) >= 10:
            break

    sandboxes = []
    volumes = []
    if os.environ.get("BL_WORKSPACE"):
        sandboxes = [
            _blaxel_dict(item)
            for item in await _all_blaxel(await SandboxInstance.list(limit=100))
            if _resource_name(item).startswith("cma-worker-")
        ]
        volumes = [
            _blaxel_dict(item)
            for item in await _all_blaxel(await VolumeInstance.list(limit=100))
            if _resource_name(item).startswith(
                _safe_name(os.environ.get("BLAXEL_WORKER_VOLUME_PREFIX", "cma-workspace"), 32)
            )
        ]

    payload = {
        "environment": as_dict(environment),
        "queue": as_dict(stats),
        "agent": as_dict(agent) if agent else None,
        "expected_default_model": DEFAULT_AGENT_MODEL,
        "warnings": configuration_warnings(agent),
        "recent_cookbook_sessions": sessions,
        "blaxel_worker_sandboxes": sandboxes,
        "blaxel_worker_volumes": volumes,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


async def _delete_if_present(delete, get, name: str, *, timeout_seconds: float) -> str:
    try:
        resource = await get(name)
        if resource_is_terminal(resource):
            return "already inactive"
    except Exception as exc:
        if is_missing_error(exc):
            return "already absent"
        raise
    await delete(name)
    await wait_until_missing(get, name, timeout_seconds=timeout_seconds)
    return "deleted and confirmed inactive"


async def _stop_session_work(client, environment_id: str, session_id: str) -> int:
    """Force-stop active or queued work before deleting its worker sandbox."""
    page = await client.beta.environments.work.list(environment_id, limit=100)
    works = [work async for work in page] if hasattr(page, "__aiter__") else getattr(page, "data", [])
    stopped = 0
    for work in works:
        data = getattr(work, "data", None)
        if getattr(data, "type", None) != "session" or getattr(data, "id", None) != session_id:
            continue
        if getattr(work, "state", None) in {"stopped", "completed"}:
            continue
        await client.beta.environments.work.stop(
            work.id,
            environment_id=environment_id,
            force=True,
        )
        stopped += 1
    return stopped


async def cleanup(args: argparse.Namespace) -> None:
    _load_dotenv()
    sandbox = args.sandbox or worker_name(args.session)
    volume = args.volume or volume_name(
        args.session,
        os.environ.get("BLAXEL_WORKER_VOLUME_PREFIX", "cma-workspace"),
    )
    plan = {
        "session": args.session,
        "session_action": args.session_action,
        "interrupt_running_session": args.interrupt,
        "work_action": "force-stop matching active or queued items",
        "sandbox": sandbox,
        "volume": None if args.keep_volume else volume,
    }
    print("Cleanup plan:")
    print(json.dumps(plan, indent=2, sort_keys=True))
    if not args.apply:
        print("\nPlan only. Add --apply to execute these exact actions.")
        return

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ENVIRONMENT_ID",
        "ANTHROPIC_ENVIRONMENT_KEY",
        "BL_WORKSPACE",
    ):
        if not os.environ.get(name):
            raise SystemExit(f"missing required env: {name}")
    client = AsyncAnthropic()
    session = await client.beta.sessions.retrieve(args.session)
    session_state = as_dict(session).get("status")
    if session_state in {"running", "rescheduling"}:
        if not args.interrupt:
            raise SystemExit(
                f"session is {session_state}; rerun with --interrupt to stop it first"
            )
        await client.beta.sessions.events.send(
            args.session,
            events=[{"type": "user.interrupt"}],
        )
        print("session: interrupt sent")
        settled_state = await wait_until_session_settled(
            client.beta.sessions.retrieve,
            args.session,
            timeout_seconds=args.wait_seconds,
        )
        print(f"session: interrupt settled ({settled_state})")

    async with AsyncAnthropic(
        auth_token=os.environ["ANTHROPIC_ENVIRONMENT_KEY"]
    ) as environment_client:
        stopped_work = await _stop_session_work(
            environment_client,
            os.environ["ANTHROPIC_ENVIRONMENT_ID"],
            args.session,
        )
    print(f"work: stopped {stopped_work} matching item(s)")

    print(
        "sandbox:",
        await _delete_if_present(
            SandboxInstance.delete,
            SandboxInstance.get,
            sandbox,
            timeout_seconds=args.wait_seconds,
        ),
    )
    if not args.keep_volume:
        print(
            "volume:",
            await _delete_if_present(
                VolumeInstance.delete,
                VolumeInstance.get,
                volume,
                timeout_seconds=args.wait_seconds,
            ),
        )
    if args.session_action == "archive":
        await client.beta.sessions.archive(args.session)
        print("session: archived")
    elif args.session_action == "delete":
        await client.beta.sessions.delete(args.session)
        print("session: deleted")
    else:
        print("session: kept")
    print("Cleanup complete. The environment, agent, images, and orchestrator were retained.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show queue, agent, sessions, workers, and volumes")
    clean = commands.add_parser("cleanup", help="plan or run exact per-session cleanup")
    clean.add_argument("--session", required=True, help="exact Anthropic session id")
    clean.add_argument("--sandbox", help="exact worker sandbox name; derived by default")
    clean.add_argument("--volume", help="exact Volume name; derived by default")
    clean.add_argument("--keep-volume", action="store_true")
    clean.add_argument("--interrupt", action="store_true")
    clean.add_argument(
        "--session-action",
        choices=("archive", "delete", "keep"),
        default="archive",
    )
    clean.add_argument("--wait-seconds", type=float, default=120)
    clean.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if getattr(args, "wait_seconds", 1) <= 0:
        parser.error("--wait-seconds must be greater than zero")
    return args


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "status":
        await status()
    else:
        await cleanup(args)


if __name__ == "__main__":
    asyncio.run(main())
