#!/usr/bin/env python3
"""Prove long worker keep-alive and file-tool path containment."""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from direct_dispatch import dispatch_until_session_work
from session_runtime import (
    DEFAULT_BUDGET_CENTS,
    ManagedSessionRuntime,
    as_dict,
    final_agent_text,
    print_receipt,
)

DEFAULT_MESSAGE = (
    "Do these steps in order. Use one tool call for each step. Every bash command must print output.\n"
    "1. bash: `sleep 30 && echo A > /workspace/a.txt && cat /workspace/a.txt`\n"
    "2. bash: `sleep 30 && echo B > /workspace/b.txt && cat /workspace/b.txt`\n"
    "3. bash: `sleep 30 && echo C > /workspace/c.txt && cat /workspace/c.txt`\n"
    "4. bash: `cat /workspace/a.txt /workspace/b.txt /workspace/c.txt`\n"
    "5. Use the write file tool, not bash, to try writing x to absolute path "
    "/tmp/cma-escape-probe.txt. Do not retry through bash.\n"
    "Finish with exactly COMBINED=ABC."
)


def containment_refused(events: list[object]) -> bool:
    """True only when the exact outside-workdir write returned a tool error."""
    outside_write_ids = set()
    for event in events:
        item = as_dict(event)
        if item.get("type") != "agent.tool_use" or item.get("name") != "write":
            continue
        if "/tmp/cma-escape-probe.txt" in json.dumps(item.get("input")):
            outside_write_ids.add(item.get("id"))
    if not outside_write_ids:
        return False
    for event in events:
        item = as_dict(event)
        if (
            item.get("type") in {"user.tool_result", "agent.tool_result"}
            and item.get("tool_use_id") in outside_write_ids
            and item.get("is_error") is True
        ):
            return True
    return False


def long_run_checks(events: list[object], final: str) -> dict[str, bool]:
    uses = [as_dict(event) for event in events if as_dict(event).get("type") == "agent.tool_use"]
    bash_inputs = [json.dumps(item.get("input")) for item in uses if item.get("name") == "bash"]
    return {
        "three_delayed_steps": all(
            any(f"sleep 30" in value and f"/{letter.lower()}.txt" in value for value in bash_inputs)
            for letter in "ABC"
        ),
        "combined_result": "COMBINED=ABC" in final.replace(" ", ""),
        "outside_write_refused": containment_refused(events),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument(
        "--direct-dispatch",
        dest="direct_dispatch",
        action="store_true",
        default=True,
        help="spawn the worker directly (default)",
    )
    parser.add_argument(
        "--no-direct-dispatch",
        dest="direct_dispatch",
        action="store_false",
        help="use the webhook orchestrator",
    )
    parser.add_argument("--max-min", type=float, default=10.0)
    parser.add_argument("--stall-seconds", type=float, default=150.0)
    parser.add_argument("--budget-cents", type=int, default=DEFAULT_BUDGET_CENTS)
    args = parser.parse_args(argv)
    if args.max_min <= 0 or args.stall_seconds <= 0 or args.budget_cents <= 0:
        parser.error("timeouts and budget must be greater than zero")
    return args


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    required = ["ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_AGENT_ID"]
    if args.direct_dispatch:
        required.extend(["ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY", "BL_WORKSPACE"])
    for name in required:
        if not os.environ.get(name):
            raise SystemExit(f"missing required env: {name}")

    runtime = ManagedSessionRuntime()
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    try:
        await runtime.require_quiet_environment(environment_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    session = await runtime.create_session(
        agent_id=os.environ["ANTHROPIC_AGENT_ID"],
        environment_id=environment_id,
        budget_cents=args.budget_cents,
        metadata={"cookbook": "blaxel-cma", "scenario": "long-session-security"},
        title="Blaxel CMA: long session and containment",
    )
    session_id = as_dict(session)["id"]
    print(f"session: {session_id}")
    print("expecting at least 90 seconds of sustained work")

    async def dispatch():
        return await dispatch_until_session_work(session_id, label="long-session-worker")

    run = await runtime.run_turn(
        session_id,
        args.message,
        timeout_seconds=args.max_min * 60,
        stall_timeout_seconds=args.stall_seconds,
        on_started=dispatch if args.direct_dispatch else None,
    )
    final = final_agent_text(run.events)
    checks = long_run_checks(run.events, final)
    checks["end_turn"] = run.stop_reason == "end_turn"
    print(f"\nFinal agent message:\n{final[:1000]}")
    print("\nVerification:")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print_receipt(await runtime.receipt(session_id))
    if not all(checks.values()):
        raise SystemExit("LONG SESSION: FAIL")
    print("\nLONG SESSION: PASS")


if __name__ == "__main__":
    asyncio.run(main())
