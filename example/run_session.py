#!/usr/bin/env python3
"""Run one verified Claude Managed Agents turn on a Blaxel worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from blaxel.core import SandboxInstance

from direct_dispatch import BlaxelFeatureSetupError, dispatch_until_session_work, worker_name
from session_runtime import (
    DEFAULT_BUDGET_CENTS,
    ManagedSessionRuntime,
    SessionExecutionError,
    SessionTimeoutError,
    as_dict,
    final_agent_text,
    idle_stop_count,
    idle_stop_reason,
    print_receipt,
    session_budget,
    tool_errors,
)

HELLO_MESSAGE = (
    "Do not use an absolute path with the write tool. Call the write tool with "
    "file_path exactly hello.txt and content exactly 'hello from blaxel'. Then run "
    "`cat /workspace/hello.txt` and finish with exactly BLAXEL_CMA_OK."
)
ADVISOR_MESSAGE = (
    "Ask your configured advisor to compare two safe ways to implement a bounded "
    "Python worker pool. Use the advice, write a short decision to advisor-proof.txt, "
    "read it back, and finish with exactly BLAXEL_ADVISOR_OK."
)


def skill_message(skill_name: str) -> str:
    return (
        f"Use the configured skill {skill_name!r}. Read its SKILL.md before applying it. "
        "Write one useful result to skill-proof.txt, read it back, and finish with "
        "exactly BLAXEL_SKILL_OK."
    )


async def worker_sandbox_lookup(sandbox_name: str) -> tuple[str, object | None]:
    """Return found, missing, or unknown without turning auth errors into proof."""
    try:
        return "found", await SandboxInstance.get(sandbox_name)
    except Exception as exc:
        message = str(exc).lower()
        if "404" in message or "not found" in message:
            return "missing", None
        return "unknown", None


async def ant_run_process_name(sandbox) -> str | None:
    try:
        processes = await sandbox.process.list()
    except Exception:
        return None
    names = [
        name
        for process in processes
        if (name := getattr(process, "name", "") or "").startswith("cma-run-")
    ]
    return names[-1] if names else None


def proof_lines(sandbox_name: str, process_name: str, workspace: str) -> list[str]:
    return [
        "",
        "Blaxel process proof:",
        f"  sandbox: {sandbox_name}",
        f"  process: {process_name}",
        f"  inspect: bl get sandbox {sandbox_name} process --workspace {workspace} -o json",
    ]


def claimed_elsewhere_lines(sandbox_name: str, workspace: str) -> list[str]:
    return [
        "",
        f"NOTE: worker sandbox {sandbox_name} was NOT found in workspace {workspace}.",
        "Another claimant on this Anthropic environment ran the successful turn.",
        "Use one Anthropic environment per Blaxel workspace for attributable proof,",
        "or use --direct-dispatch in a quiet environment.",
    ]


def _tool_uses(events: list[Any]) -> list[dict[str, Any]]:
    return [as_dict(event) for event in events if as_dict(event).get("type") == "agent.tool_use"]


def _scenario_verdict(
    scenario: str,
    events: list[Any],
    final: str,
    *,
    advisor_threads: list[Any] | None = None,
) -> tuple[bool, dict[str, bool]]:
    uses = _tool_uses(events)
    errors = tool_errors(events)
    checks: dict[str, bool] = {
        "no_tool_errors": not errors,
        "end_turn": idle_stop_reason(events) == "end_turn",
    }
    if scenario == "hello":
        checks.update({
            "write_tool": any(
                item.get("name") == "write" and "hello.txt" in json.dumps(item.get("input"))
                for item in uses
            ),
            "bash_tool": any(
                item.get("name") == "bash" and "/workspace/hello.txt" in json.dumps(item.get("input"))
                for item in uses
            ),
            "final_marker": "BLAXEL_CMA_OK" in final,
        })
    elif scenario == "advisor":
        checks.update({
            "advisor_thread": any(
                (as_dict(thread).get("agent") or {}).get("type") == "advisor"
                for thread in advisor_threads or []
            ),
            "proof_file": any(
                "advisor-proof.txt" in json.dumps(item.get("input")) for item in uses
            ),
            "final_marker": "BLAXEL_ADVISOR_OK" in final,
        })
    elif scenario == "skill":
        checks.update({
            "skill_read": any(
                item.get("name") == "read" and "SKILL.md" in json.dumps(item.get("input"))
                for item in uses
            ),
            "proof_file": any(
                "skill-proof.txt" in json.dumps(item.get("input")) for item in uses
            ),
            "final_marker": "BLAXEL_SKILL_OK" in final,
        })
    return all(checks.values()), checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("hello", "advisor", "skill"),
        default="hello",
        help="verified cookbook scenario to run",
    )
    parser.add_argument("--message", help="override the scenario prompt; scenario checks still apply")
    parser.add_argument(
        "--follow-up",
        action="append",
        default=[],
        help="send another turn to the same managed session; repeatable",
    )
    parser.add_argument("--skill-name", help="skill name or purpose for --scenario skill")
    parser.add_argument(
        "--memory-store-id",
        action="append",
        default=[],
        help="attach a read-write memory store to this self-hosted session; repeatable",
    )
    parser.add_argument(
        "--direct-dispatch",
        action="store_true",
        help="spawn the worker directly instead of using the webhook orchestrator",
    )
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument(
        "--budget-cents",
        type=int,
        default=DEFAULT_BUDGET_CENTS,
        help=f"hard list-cost ceiling in USD cents (default: {DEFAULT_BUDGET_CENTS})",
    )
    budget.add_argument(
        "--no-budget",
        action="store_true",
        help="create the session without a hard budget; use only when intentional",
    )
    parser.add_argument(
        "--resume-budget-cents",
        type=int,
        help="raise a reached budget once to this larger ceiling",
    )
    parser.add_argument("--timeout-seconds", type=float, default=360)
    args = parser.parse_args(argv)
    if not args.no_budget and args.budget_cents <= 0:
        parser.error("--budget-cents must be greater than zero")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.resume_budget_cents is not None:
        if args.no_budget:
            parser.error("--resume-budget-cents cannot be used with --no-budget")
        if args.resume_budget_cents <= args.budget_cents:
            parser.error("--resume-budget-cents must exceed --budget-cents")
    if args.scenario == "skill" and not args.skill_name:
        parser.error("--scenario skill requires --skill-name")
    return args


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    required = [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ENVIRONMENT_ID",
        "ANTHROPIC_AGENT_ID",
        "ANTHROPIC_AGENT_VERSION",
    ]
    if args.direct_dispatch:
        required.extend(["ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY", "BL_WORKSPACE"])
    for name in required:
        if not os.environ.get(name):
            raise SystemExit(f"missing required env: {name}")

    messages = {
        "hello": HELLO_MESSAGE,
        "advisor": ADVISOR_MESSAGE,
        "skill": skill_message(args.skill_name or ""),
    }
    runtime = ManagedSessionRuntime()
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    try:
        await runtime.require_quiet_environment(environment_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    budget_cents = None if args.no_budget else args.budget_cents
    resources = [
        {
            "type": "memory_store",
            "memory_store_id": memory_store_id,
            "access": "read_write",
            "instructions": "Use this store for durable preferences and project context.",
        }
        for memory_store_id in args.memory_store_id
    ] or None
    session = await runtime.create_session(
        agent_id=os.environ["ANTHROPIC_AGENT_ID"],
        agent_version=int(os.environ["ANTHROPIC_AGENT_VERSION"]),
        environment_id=environment_id,
        budget_cents=budget_cents,
        metadata={"cookbook": "blaxel-cma", "scenario": args.scenario},
        title=f"Blaxel CMA: {args.scenario}",
        resources=resources,
    )
    session_id = as_dict(session).get("id")
    if not session_id:
        raise SystemExit("session create response did not include an id")
    print(f"session: {session_id}")
    print(f"budget: {'disabled' if budget_cents is None else f'{budget_cents} cents'}")

    async def dispatch():
        return await dispatch_until_session_work(session_id)

    run = await runtime.run_turn(
        session_id,
        args.message or messages[args.scenario],
        timeout_seconds=args.timeout_seconds,
        resume_budget_cents=args.resume_budget_cents,
        on_started=dispatch if args.direct_dispatch else None,
    )
    final = final_agent_text(run.events)
    if final:
        print(f"\nFinal agent message:\n{final[:1200]}")

    if run.stop_reason == "budget_reached":
        raise SystemExit(
            "EXAMPLE: BUDGET REACHED. Raise the existing budget with "
            "--resume-budget-cents, or choose a larger initial ceiling."
        )
    if run.stop_reason in {"requires_action", "retries_exhausted"}:
        raise SystemExit(f"EXAMPLE: FAIL (stop_reason={run.stop_reason})")

    advisor_threads = await runtime.all_threads(session_id) if args.scenario == "advisor" else []
    ok, checks = _scenario_verdict(
        args.scenario,
        run.events,
        final,
        advisor_threads=advisor_threads,
    )
    print("\nVerification:")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not ok:
        raise SystemExit("EXAMPLE: FAIL")
    print("\nEXAMPLE: PASS")

    for index, follow_up in enumerate(args.follow_up, start=1):
        print(f"\nFollow-up {index} on session {session_id}:")
        follow_up_run = await runtime.run_turn(
            session_id,
            follow_up,
            timeout_seconds=args.timeout_seconds,
            on_started=dispatch if args.direct_dispatch else None,
        )
        if follow_up_run.stop_reason != "end_turn":
            raise SystemExit(f"FOLLOW-UP: FAIL (stop_reason={follow_up_run.stop_reason})")
        print(final_agent_text(follow_up_run.events))

    print_receipt(await runtime.receipt(session_id))

    workspace = os.environ.get("BL_WORKSPACE")
    if not workspace:
        return
    if run.dispatch_result:
        print("\n".join(proof_lines(
            run.dispatch_result.sandbox_name,
            run.dispatch_result.process_name,
            workspace,
        )))
        return
    lookup, sandbox = await worker_sandbox_lookup(worker_name(session_id))
    if lookup == "missing":
        print("\n".join(claimed_elsewhere_lines(worker_name(session_id), workspace)))
        return
    process_name = await ant_run_process_name(sandbox) if sandbox else None
    print("\n".join(proof_lines(worker_name(session_id), process_name or "cma-run-...", workspace)))
    if lookup == "unknown":
        print("  unverified: sandbox lookup failed; check BL_API_KEY or bl login")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (BlaxelFeatureSetupError, SessionExecutionError, SessionTimeoutError) as exc:
        raise SystemExit(f"runtime error: {exc}") from None
