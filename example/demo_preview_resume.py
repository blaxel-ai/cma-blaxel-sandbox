#!/usr/bin/env python3
"""Prove agent-authored code, a private preview, and standby resume."""
from __future__ import annotations

import argparse
import asyncio
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from direct_dispatch import dispatch_until_session_work
from session_runtime import (
    DEFAULT_BUDGET_CENTS,
    ManagedSessionRuntime,
    as_dict,
    final_agent_text,
    print_receipt,
    tool_errors,
)

PORT = 3000
APP_CODE = (
    "import os\n"
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers()\n"
    "        self.wfile.write(('hello from Blaxel CMA, server pid=%d' % os.getpid()).encode())\n"
    "    def log_message(self, *args): pass\n"
    "HTTPServer(('0.0.0.0', 3000), H).serve_forever()\n"
)
MESSAGE = (
    "Use the write tool with relative path app.py and the exact content below. "
    "Then use bash to run `python3 -m py_compile /workspace/app.py && echo APP_READY`. "
    "Do not start the server. Finish with exactly BLAXEL_PREVIEW_APP_OK.\n```python\n"
    f"{APP_CODE}```"
)


def normalize_preview_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    elif url and not url.startswith("http"):
        url = "https://" + url
    return url.rstrip("/") + "/"


def _preview_token_headers(token: str | None) -> dict[str, str]:
    return {"X-Blaxel-Preview-Token": token} if token else {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--public-preview",
        action="store_true",
        help="make the demo URL public; private token access is the default",
    )
    parser.add_argument("--preview-token-ttl-minutes", type=int, default=10)
    parser.add_argument(
        "--print-preview-token",
        action="store_true",
        help="print the short-lived token for manual browser testing",
    )
    parser.add_argument("--budget-cents", type=int, default=DEFAULT_BUDGET_CENTS)
    parser.add_argument("--timeout-seconds", type=float, default=360)
    args = parser.parse_args(argv)
    if args.preview_token_ttl_minutes <= 0:
        parser.error("--preview-token-ttl-minutes must be greater than 0")
    if args.budget_cents <= 0:
        parser.error("--budget-cents must be greater than 0")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than 0")
    return args


def hit(url: str, timeout: float = 30, headers: dict[str, str] | None = None):
    started = time.monotonic()
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()[:200], (time.monotonic() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200], (time.monotonic() - started) * 1000
    except Exception as exc:
        return None, str(exc)[:200], (time.monotonic() - started) * 1000


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    required = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ENVIRONMENT_ID",
        "ANTHROPIC_ENVIRONMENT_KEY",
        "ANTHROPIC_AGENT_ID",
        "ANTHROPIC_AGENT_VERSION",
        "BL_API_KEY",
        "BL_WORKSPACE",
    )
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
        agent_version=int(os.environ["ANTHROPIC_AGENT_VERSION"]),
        environment_id=environment_id,
        budget_cents=args.budget_cents,
        metadata={"cookbook": "blaxel-cma", "scenario": "preview-resume"},
        title="Blaxel CMA: preview and resume",
    )
    session_id = as_dict(session)["id"]
    print(f"session: {session_id}")

    async def dispatch():
        return await dispatch_until_session_work(session_id, label="preview-worker")

    run = await runtime.run_turn(
        session_id,
        MESSAGE,
        timeout_seconds=args.timeout_seconds,
        on_started=dispatch,
    )
    dispatch_result = run.dispatch_result
    worker = dispatch_result.worker
    final = final_agent_text(run.events)
    print(f"worker {dispatch_result.sandbox_name} ran {dispatch_result.process_name}")

    authored = False
    for _ in range(12):
        check = await worker.process.exec({
            "name": f"check-app-{uuid4().hex[:8]}",
            "command": "test -f /workspace/app.py && python3 -m py_compile /workspace/app.py && echo FILE_OK",
            "wait_for_completion": True,
        })
        authored = "FILE_OK" in (getattr(check, "logs", "") or "")
        if authored:
            break
        await asyncio.sleep(1)
    agent_proof = authored and "BLAXEL_PREVIEW_APP_OK" in final and not tool_errors(run.events)
    if not agent_proof:
        raise SystemExit(
            "DEMO: FAIL. The agent did not author a valid app.py; no fallback file was written."
        )

    app_process_name = f"appsrv-{uuid4().hex[:8]}"
    try:
        await worker.process.exec({
            "name": app_process_name,
            "command": "python3 /workspace/app.py",
            "wait_for_completion": False,
            "wait_for_ports": [PORT],
        })
    except Exception:
        await worker.process.exec({
            "name": app_process_name,
            "command": "python3 /workspace/app.py",
            "wait_for_completion": False,
        })
        await asyncio.sleep(3)
    inside = await worker.process.exec({
        "name": f"inside-{uuid4().hex[:8]}",
        "command": f"curl -fsS -m 5 http://localhost:{PORT}/ && echo INSIDE_OK",
        "wait_for_completion": True,
    })
    inside_ok = "INSIDE_OK" in (getattr(inside, "logs", "") or "")
    app_process = await worker.process.get(app_process_name)
    logs_available = await worker.process.logs(app_process_name, "all") is not None

    private_preview = not args.public_preview
    preview = await worker.previews.create_if_not_exists({
        "metadata": {"name": "agent-app-private" if private_preview else "agent-app-public"},
        "spec": {"port": PORT, "public": not private_preview},
    })
    app_url = normalize_preview_url(getattr(preview.spec, "url", None) or "")
    preview_token = None
    if private_preview:
        expires_at = datetime.now(UTC) + timedelta(minutes=args.preview_token_ttl_minutes)
        preview_token = await preview.tokens.create(expires_at)
        if args.print_preview_token:
            print(f"preview token: {preview_token.value}")

    headers = _preview_token_headers(preview_token.value if preview_token else None)
    warm_status, warm_body, warm_ms = None, "", 0.0
    for _ in range(12):
        warm_status, warm_body, warm_ms = hit(app_url, headers=headers)
        if warm_status == 200:
            break
        await asyncio.sleep(3)
    without_token_blocked = True
    if private_preview:
        without_token_status, _, _ = hit(app_url, timeout=10)
        without_token_blocked = without_token_status != 200
    warm_pid = warm_body.split("pid=")[-1].strip() if "pid=" in warm_body else "?"

    try:
        await worker.process.kill(dispatch_result.process_name)
    except Exception as exc:
        print(f"worker release warning: {exc!r}")
    idle_seconds = int(os.environ.get("DEMO_STANDBY_IDLE", "30"))
    print(f"waiting {idle_seconds}s for standby")
    await asyncio.sleep(idle_seconds)
    cold_status, cold_body, cold_ms = hit(app_url, headers=headers)
    cold_pid = cold_body.split("pid=")[-1].strip() if "pid=" in cold_body else "?"

    checks = {
        "agent_authored_app": agent_proof,
        "inside_sandbox_http": inside_ok,
        "supervised_process": getattr(app_process, "status", None) is not None,
        "process_logs": logs_available,
        "preview_http": warm_status == 200,
        "private_without_token_blocked": without_token_blocked,
        "resume_http": cold_status == 200,
        "same_pid_after_resume": warm_pid == cold_pid and warm_pid != "?",
    }
    print("\nVerification:")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"  INFO  warm={warm_ms:.0f}ms resume={cold_ms:.0f}ms url={app_url}")
    print_receipt(await runtime.receipt(session_id))
    print(
        f"\nCleanup plan: python3 cookbook.py cleanup --session {session_id} "
        f"--sandbox {dispatch_result.sandbox_name}"
    )
    if not all(checks.values()):
        raise SystemExit("DEMO: FAIL")
    print("\nDEMO: PASS")


if __name__ == "__main__":
    asyncio.run(main())
