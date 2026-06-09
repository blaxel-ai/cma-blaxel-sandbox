#!/usr/bin/env python3
"""Run an example Claude Managed Agents session against the Blaxel self-hosted worker.

Normal flow (webhook wired): create a session + send a message; the orchestrator
receives the `session.status_run_started` webhook and spawns a worker automatically.

For testing WITHOUT a webhook, pass --direct-dispatch to spawn the worker directly
from here after claiming its exact work item with the SDK.

Set these in your shell first:
    ANTHROPIC_API_KEY          your Anthropic API key (control-plane calls)
    ANTHROPIC_ENVIRONMENT_ID   env_...   (from step 1)
    ANTHROPIC_AGENT_ID         agent_... (the agent you created)
For --direct-dispatch, also:
    ANTHROPIC_ENVIRONMENT_KEY  sk-ant-oat01-...  (the worker's queue auth)
    BL_API_KEY, BL_WORKSPACE   so the Blaxel SDK can spawn the worker sandbox
    BL_REGION                  optional, e.g. us-pdx-1
"""
import argparse, asyncio, json, os, urllib.request, urllib.error

from blaxel.core import SandboxInstance
from direct_dispatch import BlaxelFeatureSetupError, dispatch_until_session_work, worker_name

BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")


def _headers():
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "managed-agents-2026-04-01",
        "content-type": "application/json",
    }


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode()[:300]}


def require_api(method, path, body=None):
    status, payload = api(method, path, body)
    if not (200 <= status < 300):
        raise SystemExit(f"{method} {path} failed ({status}): {json.dumps(payload)[:500]}")
    return payload


def events(sid):
    return require_api("GET", f"/v1/sessions/{sid}/events").get("data") or []


def queue_stats():
    """Current work-queue stats for the configured self-hosted environment."""
    return require_api("GET", f"/v1/environments/{os.environ['ANTHROPIC_ENVIRONMENT_ID']}/work/stats")


def queue_pending():
    """depth + pending in the environment's work queue (0 == nothing waiting)."""
    d = queue_stats()
    return (d.get("depth") or 0) + (d.get("pending") or 0)


def require_quiet_proof_environment():
    """Fail before creating a proof session if another worker can steal it."""
    stats = queue_stats()
    depth = stats.get("depth") or 0
    pending = stats.get("pending") or 0
    workers_polling = stats.get("workers_polling") or 0
    if depth or pending or workers_polling:
        raise SystemExit(
            "example proof requires a quiet self-hosted environment "
            f"(depth={depth}, pending={pending}, workers_polling={workers_polling}). "
            "Stop any environment-polling worker, webhook dispatcher, or other cookbook "
            "worker using this environment, or create a fresh environment, then rerun."
        )


async def worker_sandbox_lookup(sandbox_name: str) -> str:
    """Look up the worker sandbox with this shell's BL credentials.

    Returns "found", "missing" (a definitive not-found, so another claimant ran
    the work), or "unknown" (auth/network failure: the lookup proves nothing).
    Works with BL_API_KEY or an existing `bl login` session.
    """
    try:
        await SandboxInstance.get(sandbox_name)
        return "found"
    except Exception as exc:
        message = str(exc).lower()
        if "404" in message or "not found" in message:
            return "missing"
        return "unknown"


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
        "The transcript passed, so another claimant on this Anthropic environment",
        "(a registered webhook orchestrator or another dispatcher) ran the worker in",
        "its own Blaxel workspace. BL_WORKSPACE does not pin where shared-environment",
        "work lands: use one Anthropic environment per Blaxel workspace, or rerun with",
        "--direct-dispatch on an environment that only this workspace claims.",
    ]


def has_end_turn(items):
    for event in items:
        stop_reason = event.get("stop_reason") or {}
        if event.get("type") == "session.status_idle" and stop_reason.get("type") == "end_turn":
            return True
    return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default=(
        "Important: do not use any absolute path with the write tool. First, call the write tool "
        "with file_path exactly hello.txt and content exactly 'hello from blaxel'. Then run the "
        "shell command 'cat /workspace/hello.txt' and report its output."))
    ap.add_argument("--direct-dispatch", action="store_true",
                    help="spawn the worker directly instead of relying on the webhook + orchestrator")
    args = ap.parse_args()

    for _req in ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_AGENT_ID"):
        if not os.environ.get(_req):
            raise SystemExit(f"missing required env: {_req}")
    if args.direct_dispatch:
        for _req in ("ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY", "BL_WORKSPACE"):
            if not os.environ.get(_req):
                raise SystemExit(f"missing required env: {_req}")
    require_quiet_proof_environment()

    sess = require_api("POST", "/v1/sessions",
                       {"agent": os.environ["ANTHROPIC_AGENT_ID"], "environment_id": os.environ["ANTHROPIC_ENVIRONMENT_ID"]})
    sid = sess.get("id")
    if not sid:
        raise SystemExit(f"session create failed: {sess}")
    print("session:", sid)
    require_api("POST", f"/v1/sessions/{sid}/events",
                {"events": [{"type": "user.message", "content": [{"type": "text", "text": args.message}]}]})
    print("message sent")

    dispatched = None
    if args.direct_dispatch:
        dispatched = await dispatch_until_session_work(sid)

    # A CMA session reports status "idle" even while a turn is mid-flight, so we
    # can't watch status alone. Watch the work queue + transcript: the turn is done
    # once the worker has actually picked up work (queue went non-empty, or a tool
    # result posted), the queue has drained, and the transcript stops growing. This
    # also keeps us from declaring "done" before a cold worker even claims.
    started = False; prev = -1; stable = 0
    for i in range(72):  # up to ~6 min
        s = require_api("GET", f"/v1/sessions/{sid}")
        st = s.get("status")
        items = events(sid)
        n = len(items)
        pending = queue_pending()
        if pending > 0 or any(e.get("type") in ("agent.tool_use", "user.tool_result", "agent.tool_result") for e in items):
            started = True
        stable = stable + 1 if n == prev else 0
        prev = n
        print(f"  t={i*5:3d}s status={st} events={n} queue={pending}")
        if st == "terminated":
            break
        if started and pending == 0 and has_end_turn(items) and stable >= 1:
            break
        await asyncio.sleep(5)

    items = events(sid)
    tool_errors = []
    has_write = False
    has_bash = False
    for e in items:
        if e.get("type") == "agent.tool_use":
            print("  tool:", e.get("name"), json.dumps(e.get("input"))[:80])
            payload = json.dumps(e.get("input"))
            has_write = has_write or (e.get("name") == "write" and "hello.txt" in payload)
            has_bash = has_bash or (e.get("name") == "bash" and "/workspace/hello.txt" in payload)
        if e.get("type") in ("user.tool_result", "agent.tool_result") and e.get("is_error"):
            print("  ERROR:", json.dumps(e.get("content"))[:140])
            tool_errors.append(e)
    msgs = [e for e in items if e.get("type") == "agent.message"]
    final = json.dumps(msgs[-1].get("content")) if msgs else ""
    if msgs:
        print("\nfinal agent message:", final[:400])

    ok = (
        started
        and not tool_errors
        and has_write
        and has_bash
        and "hello from blaxel" in final.lower()
    )
    if not ok:
        raise SystemExit(
            "EXAMPLE: FAIL "
            f"(started={started}, write={has_write}, bash={has_bash}, "
            f"errors={len(tool_errors)}, final_contains_hello={'hello from blaxel' in final.lower()})"
        )
    print("\nEXAMPLE: PASS")
    workspace = os.environ.get("BL_WORKSPACE")
    if not workspace:
        return
    if dispatched:
        # Direct dispatch holds the actual worker instance; the proof is real.
        print("\n".join(proof_lines(dispatched.sandbox_name, dispatched.process_name, workspace)))
        return
    lookup = await worker_sandbox_lookup(worker_name(sid))
    if lookup == "missing":
        print("\n".join(claimed_elsewhere_lines(worker_name(sid), workspace)))
        return
    print("\n".join(proof_lines(worker_name(sid), "ant-run-...", workspace)))
    if lookup == "unknown":
        print("  (unverified: the sandbox lookup failed; check BL_API_KEY or `bl login`)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BlaxelFeatureSetupError as exc:
        raise SystemExit(f"setup error: {exc}") from None
