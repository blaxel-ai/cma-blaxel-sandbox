# Run Claude Managed Agent tools with Blaxel Sandboxes

Claude Managed Agents (CMA) lets Anthropic run the agent loop (the model, tool-calling, skills, and memory) while **tool execution happens on infrastructure you control**. When Claude decides to run a tool, your infrastructure executes it and posts the result back. This guide shows how to use **Blaxel sandboxes** for that tool execution.

This is the **self-hosted** path for teams that want to own the webhook control plane themselves.

## Architecture

Anthropic hosts the agent loop. Blaxel sandboxes run the tools. There are two sandbox roles:

```
Anthropic (orchestration)
   │  session.status_run_started  (webhook)
   ▼
Orchestrator sandbox  -- FastAPI on a public preview URL (the webhook target)
   │  spawns one worker per session, returns 200 (no polling, no babysitting)
   ▼
Worker sandbox  -- `ant beta:worker poll`
   • self-claims the session from the environment's work queue
   • downloads skills, runs tool calls (bash/read/write/edit/glob/grep) in /workspace
   • posts results back to Anthropic
   • exits when idle; a TTL auto-deletes the sandbox
```

The full lifecycle of one session:

```mermaid
sequenceDiagram
    actor User
    participant A as Anthropic (brain)
    participant O as Orchestrator sandbox
    participant W as Worker sandbox

    User->>A: create session + send message
    A->>A: enqueue work on the environment work queue
    A->>O: POST /webhook — session.status_run_started
    O->>O: verify webhook signature (whsec_…)
    O->>W: create_if_not_exists worker (one per session id)
    O->>W: start poller (keep_alive, unique process name)
    O-->>A: 200 ok (no polling, no babysitting)
    W->>A: poll work queue (environment key)
    A-->>W: hand over the session's work
    W->>W: run tool calls in /workspace (bash/read/write/edit/glob/grep)
    W->>A: post tool results
    Note over W: idle past --max-idle, poller exits. TTL deletes the sandbox
    Note over O: standbys after replying. Next webhook resumes it<br/>with process + memory intact (no cold reboot)
```

- **Orchestrator:** a Blaxel sandbox running a small webhook server, exposed on a public **preview URL** that you register as the Anthropic webhook. An inbound webhook resumes the sandbox from standby (process and memory survive resume), so it costs nothing while idle and has no execution-time limit. It does one thing per event: spawn a worker and return.
- **Worker:** a Blaxel sandbox that runs Anthropic's `ant` worker in `poll` mode. It claims the queued session itself, executes the tool calls, posts results, and shuts down. It is launched with **process keep-alive** so the sandbox stays active for the whole session instead of standbying after ~15s of no inbound connection (the poller only makes outbound calls). A TTL cleans it up once the poller exits, so nothing has to supervise it.

This is the standard control-plane plus per-session compute-plane split, with no changes to the Blaxel platform, just sandboxes.

## Prerequisites

- A Blaxel workspace, the `bl` CLI, and a **service-account API key** (`BL_API_KEY`). Create one under Service Accounts. The orchestrator needs it to spawn worker sandboxes.
- Access to Claude Managed Agents (the `managed-agents-2026-04-01` beta).
- Three Anthropic values, used in different places:
  - **`ANTHROPIC_API_KEY`:** your standard API key, for the control-plane calls in steps 1, 3, and 5 (create environment, agent, session).
  - **`ANTHROPIC_ENVIRONMENT_KEY`** (`sk-ant-oat01-...`): generated per environment in step 1; this is what the worker uses to authenticate to the work queue.
  - **`ANTHROPIC_WEBHOOK_SIGNING_KEY`** (`whsec_...`): generated when you create the webhook in step 4; this is what the orchestrator uses to verify webhook deliveries.
- The `ant` CLI is baked into the worker image (step 2), so there is nothing to install locally.
- **Docker** (for `bl push` and the worker smoke test).

## Preflight: verify your API key has CMA beta access

A 200 response with an array body means the key is valid and in the right org. A 401 means the key is invalid or lacks CMA beta access; a wrong org will return an empty list instead of your resources.

```bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  https://api.anthropic.com/v1/environments \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01"
```

## 1. Create the self-hosted environment

```bash
export ANTHROPIC_ENVIRONMENT_ID=$(curl -sS https://api.anthropic.com/v1/environments \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  -H "content-type: application/json" \
  -d '{"name": "blaxel-selfhosted", "config": {"type": "self_hosted"}}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id') or sys.exit(json.dumps(d)))")
echo "environment: $ANTHROPIC_ENVIRONMENT_ID"
```

Then in the Anthropic Console (console.anthropic.com), go to **Manage > Environments**, open the environment by name, and click **Generate environment key**. Export the `sk-ant-oat01-...` key; the orchestrator forwards it to each worker:

```bash
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

## 2. Build the worker image

The worker image (see `worker/Dockerfile`) is built on `node:22-bookworm-slim` -- a glibc base so the agent's `pip` installs get real manylinux wheels (numpy, pandas, and so on) instead of musl source builds. Key decisions: the Blaxel sandbox API binary is copied in at build time; `ant` 1.10.0 is downloaded for the right arch; `python3`, `bash`, `git`, `curl`, `tar`, and `unzip` are installed (bash is required by the agent toolset, unzip and tar by skill downloads); `EXTERNALLY-MANAGED` is removed so the agent can `pip install` freely inside this disposable box; `/workspace` and `/mnt/session/outputs` are created; `HOME` is set to `/workspace`.

The entrypoint (`worker/entrypoint.sh`) starts the sandbox API in the background and waits for it on `:8080` before the sandbox accepts commands. The orchestrator then starts the `ant` poller via the process API after the sandbox is ready.

Push it from inside the worker dir: `bl push --type sandbox`, which publishes `sandbox/cma-worker:latest`.

## 3. Deploy the orchestrator

The orchestrator is a Blaxel sandbox running this webhook server. It verifies the signature, and on `session.status_run_started` spawns a worker and returns immediately:

```python
@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY"):
        # Not configured yet (re-run setup.py with the whsec_ key): say so plainly
        # instead of masking it as a signature failure.
        return JSONResponse({"error": "webhook signing key not configured"}, status_code=503)
    try:
        event = client.beta.webhooks.unwrap(raw.decode(), headers=dict(request.headers))
    except Exception:
        return JSONResponse({"error": "signature verification failed"}, status_code=401)
    if event.data.type == "session.status_run_started":
        # Await the spawn: holding the inbound webhook connection open keeps the
        # orchestrator sandbox active during the spawn (no standby mid-flight). The
        # full app also serializes starts per session and skips duplicate webhook
        # retries while the previous poller is still alive (see orchestrator/app.py).
        if not await _spawn_worker_once(event.data.id):  # event.data.id is the session id
            return JSONResponse({"error": "worker poller did not start"}, status_code=503)
    return {"status": "ok"}

async def _spawn_worker(session_id: str):
    # Blaxel sandbox names allow only lowercase alphanumerics + hyphens.
    # See _worker_name() in orchestrator/app.py for the full sanitization.
    name = _worker_name(session_id)
    worker = await SandboxInstance.create_if_not_exists({
        "name": name, "image": "sandbox/cma-worker:latest",
        "memory": 4096, "ttl": "2h",            # max-age cleanup backstop (m/h/d/w)
        "envs": [{"name": "ANTHROPIC_ENVIRONMENT_ID", "value": ENV_ID},
                 {"name": "ANTHROPIC_ENVIRONMENT_KEY", "value": ENV_KEY}],
    })
    await worker.process.exec({
        "name": f"ant-poll-{uuid4().hex[:8]}",  # unique per restart; records persist
        "command": "ant beta:worker poll --workdir /workspace --max-idle 60s",
        "wait_for_completion": False,
        "keep_alive": True,   # hold the sandbox active for the whole session
        "timeout": 3600,      # safety cap; 0 = until the poller exits naturally
    })
```

The handler `await`s the spawn so the inbound webhook connection stays open while the worker is created and the poller starts; that keeps the orchestrator sandbox active during the spawn instead of standbying mid-flight. Once the worker is polling, it holds *itself* active via `keep_alive`. `session.status_run_started` fires once per turn and may be retried, so the full handler serializes starts per session, skips duplicate starts during the poller's idle window, and uses a fresh process name for each real restart. Later turns reuse the same per-session sandbox (`create_if_not_exists` is idempotent) and restart the poller cleanly after the previous one idles out. See `orchestrator/app.py` for the full handler. Build and bring it up (creates the sandbox, starts uvicorn, exposes a public preview):

```bash
(cd orchestrator && bl push --type sandbox)   # build + push the orchestrator image
python3 setup.py                              # run from the repo root: creates the sandbox, prints the webhook URL
```

The orchestrator runs with `BL_API_KEY` and `BL_WORKSPACE` in its env so the in-sandbox SDK can spawn workers. Unlike a Blaxel Agent, a sandbox does not inherit a workspace identity, so you provide the service-account key.

## 4. Register the webhook

In the Anthropic Console, go to **Manage > Webhooks** and create an endpoint at the printed preview URL (`https://<id>.preview.bl.run/webhook`), subscribed to `session.status_run_started`. Copy the one-time `whsec_...` signing secret, then re-run setup with it exported so the orchestrator can verify deliveries (until then, deliveries are rejected with 503):

```bash
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
python3 setup.py
```

## 5. Create an agent and run a session

```bash
export ANTHROPIC_AGENT_ID=$(curl -sS https://api.anthropic.com/v1/agents \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d '{"name":"Coding Assistant","model":"claude-opus-4-8","system":"You are a coding agent. Your working directory is /workspace. The file tools (write/read/edit/glob/grep) are sandboxed to /workspace; absolute paths like /workspace/hello.txt are REJECTED with \"absolute path not permitted\". Always pass bare relative paths to file tools (\"hello.txt\", not \"/workspace/hello.txt\"). Shell (bash) commands are unrestricted and use /workspace/... paths. Every tool call must produce non-empty output: if a shell command would print nothing (for example output redirected to a file), append a status echo such as && echo ok, because an empty tool result is rejected by the API.","tools":[{"type":"agent_toolset_20260401"}]}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id') or sys.exit(json.dumps(d)))")
echo "agent: $ANTHROPIC_AGENT_ID"

python3 example/run_session.py
```

The environment is selected per session (`environment_id` on session create), not on the agent. `run_session.py` creates a session, sends a message, and watches the transcript: the webhook fires, the orchestrator spawns a worker, the worker runs the tools in the sandbox and posts results back, and the agent completes its turn. Pass `--local-worker` to spawn the worker directly and validate the path before the webhook is wired.

## Troubleshooting

| Symptom or decision | What to do |
| --- | --- |
| Worker freezes mid-session | Launch the poller with `keep_alive: True` and a timeout cap. The poller only makes outbound calls, so the sandbox can standby without keep-alive. |
| Webhook returns 503 before signature verification | Re-run `python3 setup.py` after exporting `ANTHROPIC_WEBHOOK_SIGNING_KEY`. |
| Webhook returns 401 | Confirm the `whsec_...` secret and keep `anthropic[webhooks]` in `orchestrator/requirements.txt`. |
| Agent needs Python packages, CLIs, or compilers | Add them to `worker/Dockerfile`. The worker image is the agent runtime. |
| File tools reject `/workspace/...` paths | Pass bare relative paths like `hello.txt`. File tools are already contained to `/workspace`. |
| Shell tool result is empty | Make every command print something, for example append `&& echo ok` after redirects. |
| Later turns or webhook retries create conflicts | Keep per-session duplicate suppression and unique poller process names. Completed process records persist. |
| Worker sandbox name is invalid | Sanitize Anthropic session ids to lowercase alphanumerics and hyphens before using them as Blaxel sandbox names. |
| Worker cannot spawn other sandboxes | Pass a service-account `BL_API_KEY`; a sandbox does not receive an automatic workspace identity. |
| Outputs are missing | Read files back from `/workspace` or `/mnt/session/outputs`; nothing is auto-exported. |
| Credential boundary is unclear | The worker holds the scoped `ANTHROPIC_ENVIRONMENT_KEY`, and the agent's shell can read worker env vars. Never put the org `ANTHROPIC_API_KEY` on the worker. |
| Anthropic API returns 401 | The standard `ANTHROPIC_API_KEY` is invalid or lacks CMA beta access. Run the preflight check. |
| Resources are not visible in Console | The API key likely belongs to a different Anthropic org/workspace than the Console tab. |

## Teardown

The worker self-deletes via its TTL once idle. The orchestrator sandbox and its public preview URL persist until you remove them. Delete the orchestrator when done:

```bash
bl delete sandbox cma-orchestrator-app
```

Or via the Blaxel Console, or with the SDK: `await SandboxInstance.delete("cma-orchestrator-app")`.

## Links

- Anthropic: self-hosted sandboxes, platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes
- Blaxel sandboxes and preview URLs, docs.blaxel.ai
