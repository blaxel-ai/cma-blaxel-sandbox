# Run Claude Managed Agent tools on Blaxel Sandboxes

Anthropic runs the Claude Managed Agents loop. Blaxel runs the self-hosted tool execution in sandboxes.

In one pass, you can see Claude write a file and run a shell command inside a Blaxel worker sandbox before you touch webhooks.

## Before You Start

You need:

- A Blaxel workspace, `bl` CLI, Docker, `BL_WORKSPACE`, and a service-account `BL_API_KEY`.
- [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) beta access and an `ANTHROPIC_API_KEY`.
- `python3`.

Install local deps and create your env file:

```bash
python3 -m pip install -r requirements-dev.txt
cp .env.example .env
```

Fill `.env` with:

```text
ANTHROPIC_API_KEY=sk-ant-api03-...
BL_WORKSPACE=...
BL_API_KEY=...
```

Load it:

```bash
set -a; source .env; set +a
```

## System Shape

```text
Claude Managed Agents
  session + environment queue
        |
        | worker polls and claims work
        v
Blaxel worker sandbox
  built-in tools run in /workspace
```

- Prove the worker first with `example/run_session.py --local-worker`.
- Add the webhook only after the worker path works.
- Customize the agent runtime by editing `worker/Dockerfile`.

## Quickstart: Prove The Worker

This phase creates a self-hosted CMA environment, publishes the worker image, creates an agent, then runs one real session by spawning the worker directly from your machine.

### 1. Check access

```bash
python3 scripts/preflight.py
```

Checkpoint:

```text
preflight passed
```

### 2. Create the CMA environment

```bash
python3 scripts/create_environment.py
```

Copy the printed export into `.env`:

```text
ANTHROPIC_ENVIRONMENT_ID=env_...
```

Pause here: open the environment in the Anthropic Console, click **Generate environment key**, and add it to `.env`:

```text
ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

Reload env:

```bash
set -a; source .env; set +a
```

### 3. Publish the worker and create the agent

Creates real Blaxel image state.
The first worker publish is the heaviest step: the transformed image is roughly 3 GB, then later pushes reuse cache where possible.

```bash
( cd worker && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 scripts/create_agent.py
```

Copy the printed export into `.env`:

```text
ANTHROPIC_AGENT_ID=agent_...
```

Reload env:

```bash
set -a; source .env; set +a
```

The default model is `claude-opus-4-8`. Override it with `ANTHROPIC_AGENT_MODEL` before creating the agent if needed.

### 4. Run the worker-only session

Creates a real Anthropic session and a real Blaxel worker sandbox.

```bash
python3 example/run_session.py --local-worker
```

Checkpoint:

```text
session: sesn_...
message sent
[local-worker] cma-worker-... is polling the queue as ant-poll-...
  tool: write {"content": "hello from blaxel", "file_path": "hello.txt"}
  tool: bash {"command": "cat /workspace/hello.txt && echo"}

final agent message: ... hello from blaxel ...
```

If you get this far, the agent session, environment key, worker image, Blaxel sandbox creation, `ant` poller, file tool, bash tool, and result posting are working. The webhook is just automation around worker startup.

## Quickstart: Add The Webhook

The orchestrator is a small FastAPI webhook server on a Blaxel preview URL. It receives `session.status_run_started`, verifies the Anthropic signature, starts one worker sandbox for the session, and returns.

### 5. Publish and start the orchestrator

Creates real Blaxel image state, a persistent orchestrator sandbox, and a public preview URL.
This image is small compared with the worker; expect the worker cold start to be the slower first-run step.

```bash
( cd orchestrator && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 setup.py
```

Checkpoint:

```text
=== Register this as the Anthropic webhook URL ===
  https://<id>.preview.bl.run/webhook
```

### 6. Register the webhook

Pause here: in the Anthropic Console, create a webhook subscribed to `session.status_run_started` at the URL printed by `setup.py`.

Copy the one-time `whsec_...` signing secret into `.env`:

```text
ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
```

Reload env and restart the orchestrator process with the signing key:

```bash
set -a; source .env; set +a
python3 setup.py
```

Checkpoint:

```text
signing key: configured
```

### 7. Run the webhook path

Creates a real Anthropic session. The webhook starts the worker.

```bash
python3 example/run_session.py
```

Checkpoint: the transcript should look like the worker-only run: session id, tool calls, and a final message containing `hello from blaxel`.

## Keys You Need

| Value | Comes from | Lives where | Used for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic Console | local shell only | Create environments, agents, sessions, and read session events. |
| `ANTHROPIC_ENVIRONMENT_ID` | `scripts/create_environment.py` | local shell, orchestrator, worker | Selects the self-hosted environment. |
| `ANTHROPIC_ENVIRONMENT_KEY` | Anthropic Console environment page | orchestrator and worker | Scoped, revocable worker work-queue auth. |
| `ANTHROPIC_AGENT_ID` | `scripts/create_agent.py` | local shell | Chooses the agent for example sessions. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | Anthropic Console webhook page | orchestrator | Verifies inbound webhooks. |
| `BL_API_KEY`, `BL_WORKSPACE` | Blaxel service account / workspace | local shell and orchestrator | Lets the orchestrator spawn worker sandboxes. |

Never put the org `ANTHROPIC_API_KEY` on the worker. The worker receives only the scoped `ANTHROPIC_ENVIRONMENT_KEY`, and agent-run shell commands can read worker env vars.

## Debug Fast

| Symptom | Check |
| --- | --- |
| Preflight fails on Anthropic | `ANTHROPIC_API_KEY` is missing, invalid, in the wrong org, or lacks CMA beta access. |
| Worker freezes mid-session | The poller must run with `keep_alive: True`; it only makes outbound calls, so inbound-idle standby can freeze it without keep-alive. |
| File tool rejects `/workspace/...` | File tools are scoped to `/workspace` but require relative paths like `hello.txt`. Bash commands can still use absolute paths inside the container. |
| Worker-only run uses `/workspace/hello.txt` with the write tool | You are probably using an old agent. Rerun `python3 scripts/create_agent.py`, replace `ANTHROPIC_AGENT_ID` in `.env`, reload env, and rerun. |
| Tool result is rejected as empty | Shell commands must print something. Append `&& echo ok` after silent redirects. |
| Webhook returns 503 | Rerun `python3 setup.py` after exporting `ANTHROPIC_WEBHOOK_SIGNING_KEY`. |
| Webhook returns 401 | Confirm the `whsec_...` secret and that `anthropic[webhooks]` is installed in the orchestrator image. |
| Later turns do not start | Keep unique poller process names and duplicate suppression. Completed process records persist. |
| Output files are missing | Read files from `/workspace` or `/mnt/session/outputs`; nothing is auto-exported. |

## How It Works

The integration uses two Blaxel sandbox roles:

```text
Anthropic CMA
  agent loop, session state, event history, environment work queue
        |
        | session.status_run_started webhook
        v
Blaxel control plane
  orchestrator sandbox
  FastAPI webhook on a public preview URL
  verifies whsec_... and returns quickly
        |
        | creates or reuses one worker sandbox per session
        v
Blaxel compute plane
  worker sandbox
  ant beta:worker poll --workdir /workspace
  built-in CMA tools run in /workspace
        |
        | tool results
        v
Anthropic CMA
  transcript updates, next model step, final response
```

- `orchestrator/`: FastAPI webhook on a public preview URL. It starts workers and does not poll or supervise the queue.
- `worker/`: the agent runtime image. It runs `ant beta:worker poll`, claims work from Anthropic, executes tools in `/workspace`, posts results back, and exits after queue idle.
- The worker image includes the language runtimes, package managers, database clients, and utilities listed in Anthropic's [cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference), meets the documented runtime minimums, and remains a Blaxel-built self-hosted image rather than Anthropic's managed cloud image.
- One session gets one worker sandbox name derived from the Anthropic session id.
- Public preview URLs let the orchestrator receive webhooks and let generated apps be reachable during demos.
- Blaxel process logs show the poller and any supervised app processes inside the sandbox.

For the full integration-guide explanation, read [GUIDE.md](./GUIDE.md).

## Layout

```text
orchestrator/   FastAPI webhook -> spawn worker, Dockerfile, Blaxel sandbox config
worker/         agent runtime image: sandbox-api + ant + cloud-sandbox-compatible tools
scripts/        setup helpers for preflight, environment creation, and agent creation
example/        run sessions, validate long runs, demo preview/resume behavior
tests/          local unit tests for setup, scripts, and orchestrator behavior
setup.py        bring up the orchestrator sandbox and print its webhook URL
GUIDE.md        narrative source material for the public integration guide
AGENTS.md       fast orientation for AI coding agents
llms.txt        compact machine-readable summary
```

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

Worker sandboxes have a TTL max age from creation. The poller exits after `--max-idle`; TTL is a cleanup backstop, not idle deletion.

The orchestrator sandbox and its public preview URL persist until you remove them:

```bash
bl delete sandbox cma-orchestrator-app --workspace "$BL_WORKSPACE"
```

If a worker is still around while testing, delete the matching worker sandbox after the session finishes:

```bash
bl delete sandbox cma-worker-sesn-... --workspace "$BL_WORKSPACE"
```

## Links

- [Anthropic: self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Anthropic: cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference)
- [Anthropic: built-in agent tools](https://platform.claude.com/docs/en/managed-agents/tools)
- [Blaxel docs](https://docs.blaxel.ai)
