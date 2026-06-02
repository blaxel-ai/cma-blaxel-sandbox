# Claude Managed Agents on Blaxel: self-hosted sandbox cookbook

Anthropic runs the Claude Managed Agents loop; Blaxel runs the self-hosted tool execution in sandboxes.

This cookbook gives you a working self-hosted CMA integration with two Blaxel sandboxes:

```text
Anthropic CMA
  agent loop, session state, event history, environment work queue
        |
        | session.status_run_started webhook
        v
Blaxel control plane
  orchestrator sandbox: FastAPI webhook on a public preview URL
        |
        | creates or reuses one worker sandbox per session
        v
Blaxel compute plane
  worker sandbox: ant beta:worker poll, built-in CMA tools, /workspace runtime
```

What you can build from here:

- A coding agent that writes files, runs commands, and exposes generated apps on a Blaxel preview URL.
- An internal-tool runner that reaches services available from the Blaxel sandbox network configuration.
- A repo, data, or ML automation worker with the exact Python, Node, CLI, and compiler stack you put in the Docker image.
- A per-session isolated runtime where each agent session gets its own sandbox and process logs.

For the full integration-guide explanation, read [GUIDE.md](./GUIDE.md).

## Layout

```text
orchestrator/   FastAPI webhook -> spawn worker, Dockerfile, Blaxel sandbox config
worker/         agent runtime image: sandbox-api + ant + node + python3 + tools
scripts/        setup helpers for preflight, environment creation, and agent creation
example/        run sessions, validate long runs, demo preview/resume behavior
tests/          local unit tests for setup, scripts, and orchestrator behavior
setup.py        bring up the orchestrator sandbox and print its webhook URL
GUIDE.md        narrative source material for the public integration guide
AGENTS.md       fast orientation for AI coding agents
llms.txt        compact machine-readable summary
```

## Prerequisites

- A Blaxel workspace, the `bl` CLI, Docker, and a service-account `BL_API_KEY`.
- Claude Managed Agents beta access (`managed-agents-2026-04-01`) and an `ANTHROPIC_API_KEY`.
- `python3` and local Python deps:

```bash
python3 -m pip install -r requirements-dev.txt
```

Copy the env template and load it:

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, BL_WORKSPACE, and BL_API_KEY
set -a; source .env; set +a
```

Credential map:

| Value | Comes from | Lives where | Used for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic Console | local shell only | create environments, agents, sessions, and read session events |
| `ANTHROPIC_ENVIRONMENT_ID` | `scripts/create_environment.py` | local shell, orchestrator, worker | selects the self-hosted environment |
| `ANTHROPIC_ENVIRONMENT_KEY` | Anthropic Console environment page | orchestrator and worker | worker work-queue auth; scoped and revocable |
| `ANTHROPIC_AGENT_ID` | `scripts/create_agent.py` | local shell | chooses the agent for sessions |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | Anthropic Console webhook page | orchestrator | verifies inbound webhooks |
| `BL_API_KEY`, `BL_WORKSPACE` | Blaxel Service Accounts / workspace | local shell and orchestrator | lets the orchestrator spawn worker sandboxes |

Never put the org `ANTHROPIC_API_KEY` on the worker. The worker receives only the scoped `ANTHROPIC_ENVIRONMENT_KEY`, and agent-run shell commands can read worker env vars.

## Quickstart

### 0. Preflight

```bash
python3 scripts/preflight.py
```

You are ready when it ends with:

```text
preflight passed
```

### 1. Create the self-hosted environment

```bash
python3 scripts/create_environment.py
```

Copy the printed export into your shell or `.env`:

```text
export ANTHROPIC_ENVIRONMENT_ID=env_...
```

Then open the environment in the Anthropic Console, click **Generate environment key**, and export it:

```bash
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

### 2. Build and publish the worker image

```bash
( cd worker && bl push --type sandbox )
```

This publishes `sandbox/cma-worker:latest` in your Blaxel workspace. The worker image is the agent's runtime; add Python packages, CLIs, compilers, or system tools in `worker/Dockerfile`.

### 3. Create the agent

```bash
python3 scripts/create_agent.py
```

Copy the printed export:

```text
export ANTHROPIC_AGENT_ID=agent_...
```

The default model is `claude-opus-4-7`. Override it with `ANTHROPIC_AGENT_MODEL` before running the script if needed.

### 4. Prove the worker path before wiring webhooks

```bash
python3 example/run_session.py --local-worker
```

This creates a real CMA session and spawns the worker directly from your machine. You are done with this phase when you see a session id, a local worker polling line, tool calls, and a final agent message:

```text
session: sesn_...
message sent
[local-worker] cma-worker-... is polling the queue as ant-poll-...
  tool: write ...
  tool: bash ...
final agent message: ...
```

### 5. Build and publish the orchestrator

```bash
( cd orchestrator && bl push --type sandbox )
python3 setup.py
```

`setup.py` creates or reuses the orchestrator sandbox, starts the webhook server, and prints the public webhook URL:

```text
=== Register this as the Anthropic webhook URL ===
  https://<id>.preview.bl.run/webhook
```

### 6. Register the webhook

In the Anthropic Console, create a webhook subscribed to `session.status_run_started` at the URL printed by `setup.py`. Copy the one-time `whsec_...` signing secret, export it, then rerun setup so the orchestrator verifies deliveries:

```bash
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
python3 setup.py
```

You are done when setup prints:

```text
signing key: configured
```

### 7. Run the full webhook path

```bash
python3 example/run_session.py
```

This time Anthropic calls the orchestrator webhook, the orchestrator starts the worker, and the worker claims the session from Anthropic's environment queue. Success looks like the same tool transcript and final agent message as the local-worker run.

## Debug Fast

| Symptom | Check |
| --- | --- |
| Preflight fails on Anthropic | `ANTHROPIC_API_KEY` is missing, invalid, in the wrong org, or lacks CMA beta access. |
| Worker freezes mid-session | The poller must run with `keep_alive: True`; it only makes outbound calls, so inbound-idle standby can freeze it without keep-alive. |
| File tool rejects `/workspace/...` | File tools are scoped to `/workspace` but require relative paths like `hello.txt`. Bash commands can still use absolute paths inside the container. |
| Tool result is rejected as empty | Shell commands must print something. Append `&& echo ok` after silent redirects. |
| Webhook returns 503 | Rerun `python3 setup.py` after exporting `ANTHROPIC_WEBHOOK_SIGNING_KEY`. |
| Webhook returns 401 | Confirm the `whsec_...` secret and that `anthropic[webhooks]` is installed in the orchestrator image. |
| Later turns do not start | Keep unique poller process names and duplicate suppression. Completed process records persist. |
| Output files are missing | Read files from `/workspace` or `/mnt/session/outputs`; nothing is auto-exported. |

## Tests

Local checks:

```bash
python3 -m py_compile setup.py orchestrator/app.py example/*.py scripts/*.py
python3 -m pytest
```

Worker image smoke:

```bash
docker build -t cma-worker:smoke worker
docker run --rm --entrypoint /worker/smoke.sh cma-worker:smoke
```

Real end-to-end checks create Anthropic sessions and Blaxel sandboxes:

```bash
python3 example/run_session.py --local-worker
python3 example/run_session.py
```

## Teardown

Worker sandboxes have a TTL max age from creation. The poller exits after `--max-idle`; TTL is a cleanup backstop, not idle deletion. The orchestrator sandbox and its public preview URL persist until you remove them:

```bash
bl delete sandbox cma-orchestrator-app
```

If a worker is still around while testing, delete the matching `cma-worker-*` sandbox from the Blaxel Console or CLI after the session finishes.

## Links

- [Anthropic: self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Blaxel docs](https://docs.blaxel.ai)
