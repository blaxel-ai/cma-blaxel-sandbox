#!/usr/bin/env python3
"""Run an example Claude Managed Agents session against the Blaxel self-hosted worker.

Normal flow (webhook wired): create a session + send a message; the orchestrator
receives the `session.status_run_started` webhook and spawns a worker automatically.

For testing WITHOUT a webhook, pass --local-worker to spawn the worker directly
from here (handy for validating the worker before wiring the Anthropic webhook).

Set these in your shell first:
    ANTHROPIC_API_KEY          your Anthropic API key (control-plane calls)
    ANTHROPIC_ENVIRONMENT_ID   env_...   (from step 1)
    ANTHROPIC_AGENT_ID         agent_... (the agent you created)
For --local-worker, also:
    ANTHROPIC_ENVIRONMENT_KEY  sk-ant-oat01-...  (the worker's queue auth)
    BL_API_KEY, BL_WORKSPACE   so the Blaxel SDK can spawn the worker sandbox
    BL_REGION                  optional, e.g. us-pdx-1
"""
import argparse, asyncio, json, os, urllib.request, urllib.error

BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
WORKER_IMAGE = os.environ.get("BLAXEL_WORKER_IMAGE", "sandbox/cma-worker:latest")
WORKER_MAX_IDLE = os.environ.get("ANT_MAX_IDLE", "60s")


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


def events(sid):
    return api("GET", f"/v1/sessions/{sid}/events")[1].get("data") or []


def queue_pending():
    """depth + pending in the environment's work queue (0 == nothing waiting)."""
    _, d = api("GET", f"/v1/environments/{os.environ['ANTHROPIC_ENVIRONMENT_ID']}/work/stats")
    return (d.get("depth") or 0) + (d.get("pending") or 0)


async def spawn_local_worker(session_id):
    from blaxel.core import SandboxInstance
    # Blaxel sandbox names allow only lowercase alphanumerics + hyphens, but
    # session ids look like `sesn_01AbC...` (underscore + mixed case), so
    # sanitize before using it as a name (matches orchestrator/app.py).
    safe_id = session_id.replace("_", "-").lower()
    spec = {
        "name": f"cma-worker-{safe_id[:40]}",
        "image": WORKER_IMAGE,
        "memory": 4096,
        "ttl": "600s",
        "envs": [{"name": "ANTHROPIC_ENVIRONMENT_ID", "value": os.environ["ANTHROPIC_ENVIRONMENT_ID"]},
                 {"name": "ANTHROPIC_ENVIRONMENT_KEY", "value": os.environ["ANTHROPIC_ENVIRONMENT_KEY"]}],
    }
    if region := os.environ.get("BL_REGION"):
        spec["region"] = region
    worker = await SandboxInstance.create_if_not_exists(spec)
    # Wait for the in-sandbox API to accept commands (a fresh sandbox is cold).
    for i in range(45):
        try:
            await worker.process.exec({"name": f"probe{i}", "command": "node -v", "wait_for_completion": True})
            break
        except Exception:
            await asyncio.sleep(2)
    await worker.process.exec({
        "name": "ant-poll",
        "command": f"ant beta:worker poll --workdir /workspace --unrestricted-paths --max-idle {WORKER_MAX_IDLE}",
        "wait_for_completion": False,
    })
    print(f"[local-worker] {spec['name']} is polling the queue")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default=(
        "Write the file /workspace/hello.txt containing exactly 'hello from blaxel', "
        "then run 'cat /workspace/hello.txt' and report its contents."))
    ap.add_argument("--local-worker", action="store_true",
                    help="spawn the worker directly instead of relying on the webhook + orchestrator")
    args = ap.parse_args()

    _, sess = api("POST", "/v1/sessions",
                  {"agent": os.environ["ANTHROPIC_AGENT_ID"], "environment_id": os.environ["ANTHROPIC_ENVIRONMENT_ID"]})
    sid = sess.get("id")
    if not sid:
        raise SystemExit(f"session create failed: {sess}")
    print("session:", sid)
    api("POST", f"/v1/sessions/{sid}/events",
        {"events": [{"type": "user.message", "content": [{"type": "text", "text": args.message}]}]})
    print("message sent")

    if args.local_worker:
        await spawn_local_worker(sid)

    # A CMA session reports status "idle" even while a turn is mid-flight, so we
    # can't watch status alone. Watch the work queue + transcript: the turn is done
    # once the worker has actually picked up work (queue went non-empty, or a tool
    # result posted), the queue has drained, and the transcript stops growing. This
    # also keeps us from declaring "done" before a cold worker even claims.
    started = False; prev = -1; stable = 0
    for i in range(72):  # up to ~6 min
        _, s = api("GET", f"/v1/sessions/{sid}")
        st = s.get("status")
        items = events(sid)
        n = len(items)
        pending = queue_pending()
        if pending > 0 or any(e.get("type") in ("user.tool_result", "agent.tool_result") for e in items):
            started = True
        stable = stable + 1 if n == prev else 0
        prev = n
        print(f"  t={i*5:3d}s status={st} events={n} queue={pending}")
        if st == "terminated":
            break
        if started and pending == 0 and stable >= 3:  # worked, drained, and quiet
            break
        await asyncio.sleep(5)

    items = events(sid)
    for e in items:
        if e.get("type") == "agent.tool_use":
            print("  tool:", e.get("name"), json.dumps(e.get("input"))[:80])
        if e.get("type") in ("user.tool_result", "agent.tool_result") and e.get("is_error"):
            print("  ERROR:", json.dumps(e.get("content"))[:140])
    msgs = [e for e in items if e.get("type") == "agent.message"]
    if msgs:
        print("\nfinal agent message:", json.dumps(msgs[-1].get("content"))[:400])


if __name__ == "__main__":
    asyncio.run(main())
