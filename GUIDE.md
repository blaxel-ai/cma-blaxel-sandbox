# Run Claude Managed Agent tools with Blaxel Sandboxes

Claude Managed Agents (CMA) lets Anthropic run the agent loop (the model, tool-calling, skills, and memory) while **tool execution happens on infrastructure you control**. When Claude decides to run a tool, something on your side has to execute it and post the result back. This guide shows how to make that "something" a **Blaxel sandbox**: a secure microVM that scales to zero when idle and resumes in milliseconds.

## Architecture

Anthropic hosts the brain; Blaxel provides the hands. Two roles, both Blaxel sandboxes:

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

- **Orchestrator:** a Blaxel sandbox running a small webhook server, exposed on a public **preview URL** that you register as the Anthropic webhook. An inbound webhook resumes the sandbox from standby (process and memory survive resume), so it costs nothing while idle and has no execution-time limit. It does one thing per event: spawn a worker and return.
- **Worker:** a Blaxel sandbox that runs Anthropic's `ant` worker in `poll` mode. It claims the queued session itself, executes the tool calls, posts results, and shuts down. A TTL cleans it up, so nothing has to supervise it.

This is the standard control-plane plus per-session compute-plane split, with no changes to the Blaxel platform, just sandboxes.

## Prerequisites

- A Blaxel workspace, the `bl` CLI, and a **service-account API key** (`BL_API_KEY`). Create one under Service Accounts. The orchestrator needs it to spawn worker sandboxes.
- Access to Claude Managed Agents (the `managed-agents-2026-04-01` beta).
- Two Anthropic credentials, used in different places:
  - **`ANTHROPIC_API_KEY`:** your standard API key, for the control-plane calls in steps 1, 3, and 5 (create environment, agent, session).
  - **`ANTHROPIC_ENVIRONMENT_KEY`** (`sk-ant-oat01-…`): generated per environment in step 1; this is what the worker uses to authenticate to the work queue.
- The `ant` CLI is baked into the worker image (step 2), so there is nothing to install locally.

## 1. Create the self-hosted environment

```bash
export ANTHROPIC_ENVIRONMENT_ID=$(curl -sS https://api.anthropic.com/v1/environments \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  -H "content-type: application/json" \
  -d '{"name": "blaxel-selfhosted", "config": {"type": "self_hosted"}}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "environment: $ANTHROPIC_ENVIRONMENT_ID"
```

Then open the environment in the Anthropic Console, click **Generate environment key**, and export it. The orchestrator forwards it to each worker:

```bash
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

## 2. Build the worker image

The worker is a Blaxel sandbox image with the Blaxel sandbox API and the `ant` CLI. **It must include `/bin/bash`**, because the agent toolset's `bash` tool requires it at that exact path, plus `unzip` and `tar` for skill download.

```dockerfile
FROM node:22-alpine
COPY --from=ghcr.io/blaxel-ai/sandbox:latest /sandbox-api /usr/local/bin/sandbox-api
RUN apk add --no-cache git curl tar bash unzip

ARG ANT_VERSION=1.10.0
ARG TARGETARCH=amd64
RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo amd64); \
    curl -fsSL "https://github.com/anthropics/anthropic-cli/releases/download/v${ANT_VERSION}/ant_${ANT_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin ant && chmod +x /usr/local/bin/ant

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /workspace /mnt/session/outputs
WORKDIR /workspace
ENV HOME=/workspace
ENTRYPOINT ["/entrypoint.sh"]
```

`entrypoint.sh` starts the sandbox API and waits for it before the sandbox accepts commands; the orchestrator starts the `ant` poller later via the process API:

```sh
#!/bin/sh
/usr/local/bin/sandbox-api &
until nc -z 127.0.0.1 8080; do sleep 1; done   # wait for the sandbox API
wait
```

Push it from inside the worker dir: `bl push --type sandbox`, which publishes `sandbox/cma-worker:latest`.

## 3. Deploy the orchestrator

The orchestrator is a Blaxel sandbox running this webhook server. It verifies the signature, and on `session.status_run_started` spawns a worker and returns immediately:

```python
@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    try:
        event = client.beta.webhooks.unwrap(raw.decode(), headers=dict(request.headers))
    except Exception:
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    if event.data.type == "session.status_run_started":
        _track(spawn_worker(event.data.id))   # event.data.id is the session id
    return {"status": "ok"}

async def spawn_worker(session_id: str):
    # Blaxel sandbox names allow only lowercase alphanumerics + hyphens.
    name = "cma-worker-" + session_id.replace("_", "-").lower()[:40]
    worker = await SandboxInstance.create_if_not_exists({
        "name": name, "image": "sandbox/cma-worker:latest",
        "memory": 4096, "ttl": "600s",          # self-cleaning
        "envs": [{"name": "ANTHROPIC_ENVIRONMENT_ID", "value": ENV_ID},
                 {"name": "ANTHROPIC_ENVIRONMENT_KEY", "value": ENV_KEY}],
    })
    await worker.process.exec({
        "command": "ant beta:worker poll --workdir /workspace --unrestricted-paths --max-idle 60s",
        "wait_for_completion": False,
    })
```

`_track` keeps a reference to the spawn task so it isn't garbage-collected mid-flight and so failures are logged. `session.status_run_started` fires once per turn, so later turns reuse the same per-session sandbox and just restart the poller. See `orchestrator/app.py` for the full handler. Build and bring it up (creates the sandbox, starts uvicorn, exposes a public preview):

```bash
cd orchestrator && bl push --type sandbox
python setup.py   # prints the public preview webhook URL
```

The orchestrator runs with `BL_API_KEY` and `BL_WORKSPACE` in its env so the in-sandbox SDK can spawn workers. Unlike a Blaxel Agent, a sandbox does not inherit a workspace identity, so you provide the service-account key.

## 4. Register the webhook

In the Anthropic Console, go to **Manage > Webhooks** and create an endpoint at the printed preview URL (`https://<id>.preview.bl.run/webhook`), subscribed to `session.status_run_started`. Copy the one-time `whsec_…` signing secret, then re-run setup with it exported so the orchestrator can verify deliveries (until then, deliveries are rejected with 401):

```bash
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
python setup.py
```

## 5. Create an agent and run a session

```bash
export ANTHROPIC_AGENT_ID=$(curl -sS https://api.anthropic.com/v1/agents \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d '{"name":"Coding Assistant","model":"claude-opus-4-8","system":"You are a coding agent. Your working directory is /workspace; use absolute /workspace paths.","tools":[{"type":"agent_toolset_20260401"}]}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "agent: $ANTHROPIC_AGENT_ID"

python example/run_session.py
```

The environment is selected per session (`environment_id` on session create), not on the agent. `run_session.py` creates a session, sends a message, and watches the transcript: the webhook fires, the orchestrator spawns a worker, the worker runs the tools in the sandbox and posts results back, and the agent completes its turn. Pass `--local-worker` to spawn the worker directly and validate the path before the webhook is wired.

## Gotchas (validated)

- **`bash` needs `/bin/bash`.** The Alpine base lacks it; install `bash` (and `unzip`, `tar`).
- **Working directory.** Run the worker with `--unrestricted-paths` and have the agent use absolute `/workspace` paths, otherwise the `write` tool's base (`/`) and `bash`'s cwd (`/workspace`) disagree and the agent wastes turns reconciling them.
- **Sandbox names.** Blaxel sandbox names must be lowercase alphanumerics and hyphens; session ids (`sesn_01Ab…`) contain underscores and mixed case, so sanitize before naming a worker.
- **Orchestrator credential.** A sandbox does not get an auto-injected Blaxel identity (an Agent does), so pass a service-account `BL_API_KEY`.
- **Worker `--max-idle`** (default `60s`, override with `ANT_MAX_IDLE`) should be generous enough to span the agent's reasoning between tool calls.

## Links

- Anthropic: self-hosted sandboxes, platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes
- Blaxel sandboxes and preview URLs, docs.blaxel.ai
