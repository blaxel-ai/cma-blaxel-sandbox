#!/usr/bin/env python3
"""Long-conversation validation for the CMA-on-Blaxel cookbook.

Purpose: prove the keep-alive fix. A Blaxel sandbox standbys ~15s after its last
inbound connection; the CMA worker only makes outbound calls, so without
process keep-alive the worker freezes mid-session. This script forces a session
whose work lasts WELL past that 15s window (three 30s sleeps + reads = ~90s+ of
sustained tool execution in one turn) and checks it actually completes.

It also probes the security fix: it asks the agent to use its *write file tool*
(not bash) to write outside /workspace, and reports whether that was refused
(expected, since the worker now runs without --unrestricted-paths).

Runs WITHOUT the Anthropic webhook by default (--direct-dispatch spawns the worker
directly after claiming its exact work item). Drop --direct-dispatch once the
webhook is registered to exercise the orchestrator path end to end instead.

Set the same env as run_session.py first (ANTHROPIC_API_KEY, ANTHROPIC_ENVIRONMENT_ID,
ANTHROPIC_ENVIRONMENT_KEY, ANTHROPIC_AGENT_ID, BL_API_KEY, BL_WORKSPACE, [BL_REGION]):

    python3 example/validate_long_session.py                     # direct-dispatch (no webhook)
    python3 example/validate_long_session.py --no-direct-dispatch   # rely on webhook + orchestrator
"""
import argparse, asyncio, json, os, time, urllib.request, urllib.error

from direct_dispatch import dispatch_until_session_work
from run_session import require_quiet_proof_environment

BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

# One turn that forces ~90s+ of sustained work across several tool calls, plus a
# containment probe. The long sleeps are what would trip the 15s standby freeze.
DEFAULT_MESSAGE = (
    "Do the following steps in order, each as its own tool call, reporting briefly after each. "
    "Every bash command must print to stdout (the API rejects an empty tool result):\n"
    "1. bash: `sleep 30 && echo A > /workspace/a.txt && cat /workspace/a.txt`\n"
    "2. bash: `sleep 30 && echo B > /workspace/b.txt && cat /workspace/b.txt`\n"
    "3. bash: `sleep 30 && echo C > /workspace/c.txt && cat /workspace/c.txt`\n"
    "4. bash: `cat /workspace/a.txt /workspace/b.txt /workspace/c.txt`\n"
    "5. Using your WRITE FILE tool (not bash), attempt to write the text 'x' to the "
    "absolute path /tmp/cma-escape-probe.txt, and tell me verbatim whether it "
    "succeeded or was refused.\n"
    "Finish with a single line: COMBINED=<the concatenation of a, b, c>."
)


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


def queue_pending():
    d = require_api("GET", f"/v1/environments/{os.environ['ANTHROPIC_ENVIRONMENT_ID']}/work/stats")
    return (d.get("depth") or 0) + (d.get("pending") or 0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--direct-dispatch", dest="direct_dispatch", action="store_true", default=True,
                    help="spawn the worker directly (default; no webhook needed)")
    ap.add_argument("--no-direct-dispatch", dest="direct_dispatch", action="store_false",
                    help="rely on the webhook + orchestrator instead")
    ap.add_argument("--max-min", type=float, default=10.0, help="overall timeout in minutes")
    args = ap.parse_args()

    for req in ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_AGENT_ID"):
        if not os.environ.get(req):
            raise SystemExit(f"missing required env: {req}")
    if args.direct_dispatch:
        for req in ("ANTHROPIC_ENVIRONMENT_KEY", "BL_API_KEY", "BL_WORKSPACE"):
            if not os.environ.get(req):
                raise SystemExit(f"missing required env: {req}")
    require_quiet_proof_environment()

    sess = require_api("POST", "/v1/sessions",
                       {"agent": os.environ["ANTHROPIC_AGENT_ID"], "environment_id": os.environ["ANTHROPIC_ENVIRONMENT_ID"]})
    sid = sess.get("id")
    if not sid:
        raise SystemExit(f"session create failed: {sess}")
    print("session:", sid)
    require_api("POST", f"/v1/sessions/{sid}/events",
                {"events": [{"type": "user.message", "content": [{"type": "text", "text": args.message}]}]})
    print("message sent; expecting ~90s+ of sustained work\n")

    if args.direct_dispatch:
        await dispatch_until_session_work(sid, label="long-session-worker")

    t0 = time.monotonic()
    deadline = t0 + args.max_min * 60
    started = False
    tool_results = 0
    last_n = -1
    last_change = t0   # wall time of the last events-count change
    final_msg = ""
    froze = False
    while time.monotonic() < deadline:
        s = require_api("GET", f"/v1/sessions/{sid}")
        st = s.get("status")
        items = events(sid)
        n = len(items)
        pending = queue_pending()
        tool_results = sum(1 for e in items if e.get("type") in ("user.tool_result", "agent.tool_result"))
        if pending > 0 or tool_results > 0:
            started = True
        if n != last_n:
            last_change = time.monotonic()
        last_n = n
        msgs_now = [e for e in items if e.get("type") == "agent.message"]
        final_msg = json.dumps(msgs_now[-1].get("content")) if msgs_now else ""
        el = int(time.monotonic() - t0)
        idle = int(time.monotonic() - last_change)
        print(f"  t={el:3d}s status={st} events={n} queue={pending} tool_results={tool_results} idle={idle}s")
        # Done: session terminated, or the agent emitted the finish marker.
        if st == "terminated" or "COMBINED" in final_msg:
            break
        # Real freeze: started, but no new event for far longer than the 30s sleeps.
        if started and idle >= 150:
            froze = True
            print("\nFAIL: no session progress for 150s -- stalled worker (standby-freeze, a "
                  "4xx on tool-result post, or worker exit). Check the worker's ant-run logs.")
            break
        await asyncio.sleep(5)

    items = events(sid)
    for e in items:
        if e.get("type") == "agent.tool_use":
            print("  tool:", e.get("name"), json.dumps(e.get("input"))[:90])
        if e.get("type") in ("user.tool_result", "agent.tool_result") and e.get("is_error"):
            print("  tool_error:", json.dumps(e.get("content"))[:160])
    msgs = [e for e in items if e.get("type") == "agent.message"]
    final = json.dumps(msgs[-1].get("content")) if msgs else ""
    if final:
        print("\nfinal agent message:", final[:600])

    # Verdict
    el = int(time.monotonic() - t0)
    pending = queue_pending()
    has_abc = all(x in final for x in ("A", "B", "C")) or "COMBINED=ABC" in final.replace(" ", "")
    ok = bool(msgs) and pending == 0 and started and has_abc
    print("\n" + "=" * 60)
    print(f"keep-alive long-run: {'PASS' if ok else 'FAIL'}  "
          f"(elapsed={el}s, tool_results={tool_results}, queue={pending})")
    if not ok:
        print("  - if it froze early (~15-20s) the keep_alive fix is not taking effect")
    print("security containment probe: read the step-5 result above -- the write-file "
          "tool write to /tmp should be REFUSED (no --unrestricted-paths). bash writes "
          "outside /workspace are NOT restricted by this flag; only the file tools are.")
    print("=" * 60)
    if not ok or froze:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
