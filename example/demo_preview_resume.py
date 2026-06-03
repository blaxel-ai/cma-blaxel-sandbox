#!/usr/bin/env python3
"""Demo: a Claude Managed Agent authors an app, serves it on a Blaxel public
preview URL, and checks the server process before and after sandbox standby.

Flow:
  1. Create a session; the agent uses its bash tool to author app.py in /workspace.
  2. The harness starts that app as a long-lived sandbox process on :3000 (CMA
     tool calls are request-scoped, so a server must be supervised, not
     backgrounded inside one tool call) and exposes it on a public preview URL.
  3. Drop the CMA worker keep-alive process, idle until the sandbox standbys, then hit the preview
     again and compare the server pid before and after resume.

Env (same as run_session.py): ANTHROPIC_API_KEY, ANTHROPIC_ENVIRONMENT_ID,
ANTHROPIC_ENVIRONMENT_KEY, ANTHROPIC_AGENT_ID, BL_API_KEY, BL_WORKSPACE, [BL_REGION].
Run: python3 example/demo_preview_resume.py
"""
import asyncio, json, os, time, urllib.request, urllib.error
from uuid import uuid4

from local_worker import dispatch_until_session_work

BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
PORT = 3000  # NOT 8080 -- that's the in-sandbox sandbox-api port.

APP_CODE = (
    "import os\n"
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers()\n"
    "        self.wfile.write(('hello from Blaxel CMA, server pid=%d' % os.getpid()).encode())\n"
    "    def log_message(self, *a): pass\n"
    "HTTPServer(('0.0.0.0', 3000), H).serve_forever()\n"
)

MESSAGE = (
    "Use your bash tool to create /workspace/app.py with EXACTLY the content below, "
    "then run `test -f /workspace/app.py && echo DONE`. Do not run the app server. "
    "The shell command must print DONE.\n```python\n"
    f"{APP_CODE}```"
)


def _headers():
    return {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "anthropic-beta": "managed-agents-2026-04-01", "content-type": "application/json"}


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


def hit(url, timeout=30):
    t = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()[:200], (time.monotonic() - t) * 1000
    except Exception as e:
        return None, str(e)[:200], (time.monotonic() - t) * 1000


async def main():
    for req in ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY", "ANTHROPIC_AGENT_ID", "BL_API_KEY", "BL_WORKSPACE"):
        if not os.environ.get(req):
            raise SystemExit(f"missing required env: {req}")

    sess = require_api("POST", "/v1/sessions",
                       {"agent": os.environ["ANTHROPIC_AGENT_ID"], "environment_id": os.environ["ANTHROPIC_ENVIRONMENT_ID"]})
    sid = sess.get("id")
    if not sid:
        raise SystemExit(f"session create failed: {sess}")
    print("session:", sid)
    require_api("POST", f"/v1/sessions/{sid}/events",
                {"events": [{"type": "user.message", "content": [{"type": "text", "text": MESSAGE}]}]})

    dispatch = await dispatch_until_session_work(sid, label="preview-worker")
    worker = dispatch.worker
    print(f"worker {dispatch.sandbox_name} running claimed work {dispatch.work_id}")

    print("\n[1/3] waiting for the agent to author /workspace/app.py ...")
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        msgs = [e for e in events(sid) if e.get("type") == "agent.message"]
        if msgs and "DONE" in json.dumps(msgs[-1].get("content")):
            break
        await asyncio.sleep(5)
    # Confirm the file actually landed at /workspace/app.py; if the contained write
    # tool put it elsewhere, the harness writes it (the demo still shows the wow).
    authored = False
    for _ in range(12):
        chk = await worker.process.exec({"name": f"chk-{uuid4().hex[:8]}", "command": "test -f /workspace/app.py && echo FILEOK || echo MISSING",
                                         "wait_for_completion": True})
        authored = "FILEOK" in getattr(chk, "logs", "")
        if authored:
            break
        await asyncio.sleep(1)
    if not authored:
        await worker.process.exec({"name": f"writefile-{uuid4().hex[:8]}",
                                   "command": "cat > /workspace/app.py <<'PYEOF'\n" + APP_CODE + "PYEOF\necho WROTE",
                                   "wait_for_completion": True})
    print("  app.py:", "agent-authored" if authored else "harness fallback (agent write landed elsewhere)")

    print("\n[2/3] harness starts the agent's app as a supervised server on :3000, expose preview ...")
    try:
        app_process_name = f"appsrv-{uuid4().hex[:8]}"
        await worker.process.exec({"name": app_process_name, "command": "python3 /workspace/app.py",
                                   "wait_for_completion": False, "wait_for_ports": [PORT]})
    except Exception:
        app_process_name = f"appsrv-{uuid4().hex[:8]}"
        await worker.process.exec({"name": app_process_name, "command": "python3 /workspace/app.py",
                                   "wait_for_completion": False})
        await asyncio.sleep(3)
    inside = await worker.process.exec({"name": f"insidecurl-{uuid4().hex[:8]}",
                                        "command": f"curl -s -m 5 http://localhost:{PORT}/ && echo ' INSIDE_OK'",
                                        "wait_for_completion": True})
    print("  inside-sandbox check:", (getattr(inside, "logs", "") or "").strip()[:120])
    preview = await worker.previews.create_if_not_exists({"metadata": {"name": "app"}, "spec": {"port": PORT, "public": True}})
    url = getattr(preview.spec, "url", None) or ""
    if url.startswith("//"):
        url = "https:" + url
    elif url and not url.startswith("http"):
        url = "https://" + url
    app_url = url.rstrip("/") + "/"
    st = None; body = ""; ms = 0
    for _ in range(12):
        st, body, ms = hit(app_url)
        if st == 200:
            break
        await asyncio.sleep(3)
    print(f"  PREVIEW URL: {app_url}")
    print(f"  warm GET -> status={st} body={body!r} ({ms:.0f} ms)")
    pid_warm = body.split("pid=")[-1].strip() if body and "pid=" in body else "?"

    print("\n[3/3] resume-from-standby: release keep-alive, idle to force standby, hit again ...")
    try:
        await worker.process.kill(dispatch.process_name)
    except Exception as e:
        print("  (kill worker:", repr(e), ")")
    idle_s = int(os.environ.get("DEMO_STANDBY_IDLE", "30"))
    print(f"  idling {idle_s}s with no connection so the sandbox snapshots to standby ...")
    await asyncio.sleep(idle_s)
    st2, body2, ms2 = hit(app_url)
    pid_cold = body2.split("pid=")[-1].strip() if body2 and "pid=" in body2 else "?"
    print(f"  resume GET -> status={st2} body={body2!r} ({ms2:.0f} ms incl. network)")

    print("\n" + "=" * 64)
    ok = (st == 200 and st2 == 200 and pid_warm == pid_cold and pid_warm != "?")
    print(f"DEMO: {'PASS' if ok else 'PARTIAL/FAIL'}")
    print(f"  agent-authored app.py                    : {authored}")
    print(f"  app reachable on preview URL             : {st == 200}")
    print(f"  same server pid across standby/resume       : {pid_warm} == {pid_cold}")
    print(f"  resume round-trip after standby             : {ms2:.0f} ms incl. network")
    print(f"\n  Click it: {app_url}")
    print(f"  Cleanup:  delete sandbox '{dispatch.sandbox_name}' when done.")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
