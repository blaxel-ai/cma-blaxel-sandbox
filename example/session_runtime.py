"""Supported Claude Managed Agents session lifecycle helpers.

The cookbook examples use this module instead of hand-written HTTP calls. It
keeps event streaming, complete paginated history, terminal error handling,
budgets, and usage receipts consistent across every example.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic

DEFAULT_BUDGET_CENTS = 100


class SessionExecutionError(RuntimeError):
    """The Managed Agents event stream reported a terminal execution error."""


class SessionTimeoutError(TimeoutError):
    """A Managed Agents turn did not finish before its local deadline."""


def as_dict(value: Any) -> dict[str, Any]:
    """Return an SDK model or mapping as a plain dictionary."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"unsupported SDK value: {type(value).__name__}")


def session_budget(cents: int) -> dict[str, Any]:
    if cents <= 0:
        raise ValueError("budget cents must be greater than zero")
    return {
        "type": "limit",
        "max_list_cost": {"amount": str(cents), "currency": "USD"},
    }


def event_type(event: Any) -> str:
    return str(as_dict(event).get("type") or "")


def _budget_event_key(event: Any, occurrence: int = 1) -> str:
    """Identify one budget idle event across stream and paginated history reads."""
    item = as_dict(event)
    if event_id := item.get("id"):
        return f"id:{event_id}"
    return f"body:{json.dumps(item, sort_keys=True, default=str)}#{occurrence}"


def idle_stop_reason(events: list[Any]) -> str | None:
    reason = None
    for event in events:
        item = as_dict(event)
        if item.get("type") == "session.status_idle":
            reason = (item.get("stop_reason") or {}).get("type") or reason
    return reason


def idle_stop_count(events: list[Any], reason: str) -> int:
    return sum(
        1
        for event in events
        if as_dict(event).get("type") == "session.status_idle"
        and (as_dict(event).get("stop_reason") or {}).get("type") == reason
    )


def session_error_message(event: Any) -> str:
    item = as_dict(event)
    error = item.get("error") or {}
    kind = error.get("type") or "unknown"
    message = error.get("message") or json.dumps(error, sort_keys=True)
    retry = error.get("retry_status")
    suffix = f"; retry_status={retry}" if retry else ""
    return f"{kind}: {message}{suffix}"


def final_agent_text(events: list[Any]) -> str:
    messages = [as_dict(event) for event in events if event_type(event) == "agent.message"]
    if not messages:
        return ""
    parts = []
    for block in messages[-1].get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def tool_errors(events: list[Any]) -> list[dict[str, Any]]:
    errors = []
    for event in events:
        item = as_dict(event)
        if item.get("type") in {"user.tool_result", "agent.tool_result"} and item.get("is_error"):
            errors.append(item)
    return errors


def event_summary(event: Any) -> str | None:
    """Create one concise progress line for a persisted event."""
    item = as_dict(event)
    kind = item.get("type")
    if kind == "agent.tool_use":
        return f"tool {item.get('name')}: {json.dumps(item.get('input'), sort_keys=True)[:160]}"
    if kind in {"user.tool_result", "agent.tool_result"} and item.get("is_error"):
        return f"tool error: {json.dumps(item.get('content'), sort_keys=True)[:200]}"
    if kind == "session.error":
        return f"session error: {session_error_message(item)}"
    if kind == "session.status_idle":
        return f"idle: {(item.get('stop_reason') or {}).get('type', 'unknown')}"
    if kind == "session.status_running":
        return "running"
    return None


def usage_receipt(session: Any) -> dict[str, Any]:
    """Extract a stable, JSON-ready cost and timing receipt from a session."""
    item = as_dict(session)
    usage = item.get("usage") or {}
    stats = item.get("stats") or {}
    model = ((item.get("agent") or {}).get("model") or {})
    if isinstance(model, str):
        model_id = model
    else:
        model_id = model.get("id")
    cost = usage.get("list_cost") or {}
    return {
        "session_id": item.get("id"),
        "status": item.get("status"),
        "model": model_id,
        "list_cost_cents": cost.get("amount"),
        "currency": cost.get("currency"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "active_seconds": usage.get("active_seconds"),
        "duration_seconds": stats.get("duration_seconds"),
    }


@dataclass
class SessionRun:
    session_id: str
    events: list[Any]
    stop_reason: str
    dispatch_result: Any = None


class ManagedSessionRuntime:
    """Small production-style client for one cookbook process."""

    def __init__(self, client: AsyncAnthropic | None = None, *, poll_seconds: float = 3.0):
        self.client = client or AsyncAnthropic()
        self.poll_seconds = poll_seconds

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        budget_cents: int | None = DEFAULT_BUDGET_CENTS,
        metadata: dict[str, str] | None = None,
        title: str | None = None,
        resources: list[dict[str, Any]] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "agent": agent_id,
            "environment_id": environment_id,
            "metadata": metadata or {"cookbook": "blaxel-cma"},
        }
        if resources:
            kwargs["resources"] = resources
        if budget_cents is not None:
            kwargs["budget"] = session_budget(budget_cents)
        if title:
            kwargs["title"] = title
        return await self.client.beta.sessions.create(**kwargs)

    async def all_events(
        self,
        session_id: str,
        *,
        created_at_gte: str | None = None,
    ) -> list[Any]:
        """Return authoritative event history, optionally from a timestamp cursor."""
        kwargs: dict[str, Any] = {"limit": 100, "order": "asc"}
        if created_at_gte:
            kwargs["created_at_gte"] = created_at_gte
        page = await self.client.beta.sessions.events.list(session_id, **kwargs)
        return [event async for event in page]

    async def all_threads(self, session_id: str) -> list[Any]:
        page = await self.client.beta.sessions.threads.list(session_id, limit=100)
        return [thread async for thread in page]

    async def queue_stats(self, environment_id: str) -> Any:
        return await self.client.beta.environments.work.stats(environment_id)

    async def _raise_budget(self, session_id: str, requested_cents: int) -> None:
        session = as_dict(await self.client.beta.sessions.retrieve(session_id))
        amount = ((session.get("usage") or {}).get("list_cost") or {}).get("amount")
        try:
            consumed_cents = Decimal(str(amount))
        except (InvalidOperation, TypeError):
            raise SessionExecutionError(
                f"cannot validate budget increase: invalid consumed list cost {amount!r}"
            ) from None
        if Decimal(requested_cents) <= consumed_cents:
            raise SessionExecutionError(
                f"resume budget {requested_cents} cents must exceed consumed list cost "
                f"{consumed_cents} cents"
            )
        await self.client.beta.sessions.update(
            session_id,
            budget=session_budget(requested_cents),
        )

    async def require_quiet_environment(self, environment_id: str) -> None:
        stats = as_dict(await self.queue_stats(environment_id))
        depth = stats.get("depth") or 0
        pending = stats.get("pending") or 0
        workers_polling = stats.get("workers_polling") or 0
        if depth or pending:
            raise RuntimeError(
                "example proof requires an empty self-hosted work queue "
                f"(depth={depth}, pending={pending}, workers_polling={workers_polling}). "
                "Wait for queued work to finish or use a fresh environment."
            )

    async def _send_message(self, session_id: str, message: str) -> None:
        await self.client.beta.sessions.events.send(
            session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": message}],
            }],
        )

    async def _interrupt(self, session_id: str) -> None:
        try:
            await self.client.beta.sessions.events.send(
                session_id,
                events=[{"type": "user.interrupt"}],
            )
        except Exception:
            pass

    async def _raise_for_errors(self, events: list[Any]) -> None:
        for event in events:
            if event_type(event) == "session.error":
                raise SessionExecutionError(session_error_message(event))

    async def _poll_until_idle(
        self,
        session_id: str,
        *,
        resume_budget_cents: int | None,
        budget_raised: bool,
        seen_ids: set[str],
        baseline_ids: set[str],
        handled_budget_events: set[str],
        stall_timeout_seconds: float | None,
        history: list[Any],
    ) -> tuple[list[Any], str, bool]:
        events = list(history)
        last_count = len(events)
        last_change = asyncio.get_running_loop().time()
        while True:
            timestamps = [
                str(as_dict(event).get("created_at"))
                for event in events
                if as_dict(event).get("created_at")
            ]
            batch = await self.all_events(
                session_id,
                created_at_gte=max(timestamps) if timestamps else None,
            )
            by_id = {
                str(as_dict(event).get("id")): index
                for index, event in enumerate(events)
                if as_dict(event).get("id")
            }
            for event in batch:
                event_id = str(as_dict(event).get("id") or "")
                if event_id and event_id in by_id:
                    events[by_id[event_id]] = event
                else:
                    events.append(event)
                    if event_id:
                        by_id[event_id] = len(events) - 1
            if len(events) != last_count:
                last_count = len(events)
                last_change = asyncio.get_running_loop().time()
            elif (
                stall_timeout_seconds is not None
                and asyncio.get_running_loop().time() - last_change >= stall_timeout_seconds
            ):
                raise SessionTimeoutError(
                    f"session {session_id} made no event progress for "
                    f"{stall_timeout_seconds:g}s"
                )
            for event in events:
                item = as_dict(event)
                event_id = str(item.get("id") or "")
                if event_id and event_id not in seen_ids:
                    seen_ids.add(event_id)
                    if line := event_summary(item):
                        print(f"  {line}")
            turn_events = [
                event
                for event in events
                if str(as_dict(event).get("id") or "") not in baseline_ids
            ]
            await self._raise_for_errors(turn_events)
            reason = idle_stop_reason(turn_events)
            if reason == "budget_reached":
                budget_events = [
                    event
                    for event in turn_events
                    if event_type(event) == "session.status_idle"
                    and (as_dict(event).get("stop_reason") or {}).get("type") == reason
                ]
                budget_event = budget_events[-1]
                key = _budget_event_key(budget_event, len(budget_events))
                if key in handled_budget_events:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                if resume_budget_cents and not budget_raised:
                    await self._raise_budget(session_id, resume_budget_cents)
                    budget_raised = True
                    handled_budget_events.add(key)
                    print(f"  budget raised to {resume_budget_cents} cents; session resumed")
                else:
                    return events, reason, budget_raised
            elif reason == "requires_action":
                await asyncio.sleep(self.poll_seconds)
                continue
            elif reason:
                return events, reason, budget_raised
            await asyncio.sleep(self.poll_seconds)

    async def run_turn(
        self,
        session_id: str,
        message: str,
        *,
        timeout_seconds: float = 360,
        stall_timeout_seconds: float | None = None,
        resume_budget_cents: int | None = None,
        on_started: Callable[[], Awaitable[Any]] | None = None,
    ) -> SessionRun:
        """Send one turn, stream progress, and reconcile with complete history.

        The stream is the fast path. Incremental paginated history reconciliation
        is the source of truth after completion and after a stream disconnect.
        """
        sent = False
        budget_raised = False
        seen_ids: set[str] = set()
        handled_budget_events: set[str] = set()
        stream_budget_occurrences = 0
        dispatch_task: asyncio.Task | None = None
        dispatch_result = None

        try:
            async with asyncio.timeout(timeout_seconds):
                baseline = await self.all_events(session_id)
                baseline_ids = {
                    str(as_dict(event).get("id"))
                    for event in baseline
                    if as_dict(event).get("id")
                }
                seen_ids.update(baseline_ids)
                try:
                    stream = await self.client.beta.sessions.events.stream(session_id)
                    async with stream:
                        await self._send_message(session_id, message)
                        sent = True
                        if on_started:
                            dispatch_task = asyncio.create_task(on_started())
                        iterator = stream.__aiter__()
                        while True:
                            try:
                                if stall_timeout_seconds is None:
                                    event = await anext(iterator)
                                else:
                                    event = await asyncio.wait_for(
                                        anext(iterator),
                                        timeout=stall_timeout_seconds,
                                    )
                            except StopAsyncIteration:
                                break
                            except TimeoutError as exc:
                                raise SessionTimeoutError(
                                    f"session {session_id} made no streamed event progress for "
                                    f"{stall_timeout_seconds:g}s"
                                ) from exc
                            item = as_dict(event)
                            event_id = str(item.get("id") or "")
                            if event_id and event_id in baseline_ids:
                                continue
                            if event_id:
                                seen_ids.add(event_id)
                            if line := event_summary(item):
                                print(f"  {line}")
                            if item.get("type") == "session.error":
                                raise SessionExecutionError(session_error_message(item))
                            if item.get("type") != "session.status_idle":
                                continue
                            reason = (item.get("stop_reason") or {}).get("type")
                            if reason == "budget_reached" and resume_budget_cents and not budget_raised:
                                await self._raise_budget(session_id, resume_budget_cents)
                                budget_raised = True
                                stream_budget_occurrences += 1
                                handled_budget_events.add(
                                    _budget_event_key(item, stream_budget_occurrences)
                                )
                                print(f"  budget raised to {resume_budget_cents} cents; session resumed")
                                continue
                            if reason == "requires_action":
                                continue
                            if reason:
                                break
                except (SessionExecutionError, SessionTimeoutError):
                    raise
                except Exception as exc:
                    print(f"  event stream disconnected ({type(exc).__name__}); reconciling history")

                if not sent:
                    await self._send_message(session_id, message)
                    sent = True
                    if on_started:
                        dispatch_task = asyncio.create_task(on_started())

                events, reason, budget_raised = await self._poll_until_idle(
                    session_id,
                    resume_budget_cents=resume_budget_cents,
                    budget_raised=budget_raised,
                    seen_ids=seen_ids,
                    baseline_ids=baseline_ids,
                    handled_budget_events=handled_budget_events,
                    stall_timeout_seconds=stall_timeout_seconds,
                    history=baseline,
                )
                if dispatch_task:
                    dispatch_result = await dispatch_task
                return SessionRun(session_id, events, reason, dispatch_result)
        except SessionTimeoutError:
            await self._interrupt(session_id)
            if dispatch_task and not dispatch_task.done():
                dispatch_task.cancel()
            raise
        except TimeoutError as exc:
            await self._interrupt(session_id)
            if dispatch_task and not dispatch_task.done():
                dispatch_task.cancel()
            raise SessionTimeoutError(
                f"session {session_id} exceeded {timeout_seconds:g}s; interrupt sent"
            ) from exc

    async def receipt(self, session_id: str) -> dict[str, Any]:
        return usage_receipt(await self.client.beta.sessions.retrieve(session_id))


def print_receipt(receipt: dict[str, Any]) -> None:
    print("\nUsage receipt:")
    print(json.dumps(receipt, indent=2, sort_keys=True))
