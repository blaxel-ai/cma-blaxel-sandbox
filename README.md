# Run Claude Managed Agent tools on Blaxel Sandboxes

Anthropic runs the Claude Managed Agents loop, session state, event history, and self-hosted environment queue. Blaxel runs the self-hosted execution layer: an orchestrator sandbox claims exact CMA work items, then a per-session worker sandbox runs those tool calls.

The first proof is small on purpose. After setup, this command creates one real session and runs its claimed work inside a Blaxel worker sandbox:

```bash
python3 example/run_session.py --local-worker
```

```text
tool: write {"content": "hello from blaxel", "file_path": "hello.txt"}
tool: bash {"command": "cat /workspace/hello.txt && echo"}
final agent message: ... hello from blaxel ...
EXAMPLE: PASS
```

## System Shape

```text
Anthropic CMA
  agent loop, session state, event history, self-hosted environment queue
        |
        | session.status_run_started webhook
        v
Blaxel orchestrator sandbox
  FastAPI webhook dispatcher on a public preview URL
        |
        | schedules dispatch, readies session sandbox, then claims work_...
        |
        | starts/reuses cma-worker-<session> with WORK_ID + SESSION_ID
        v
Blaxel worker sandbox
  ant beta:worker run --workdir /workspace
  built-in CMA tools run in that session's /workspace
        |
        | tool results + work heartbeat/stop
        v
Anthropic CMA
```

- Prove the worker first with `example/run_session.py --local-worker`.
- Add the webhook only after the exact-work worker path works.
- Use one active work-claiming path per Anthropic self-hosted environment while proving a run: local worker, webhook dispatcher, or another cookbook worker. The first claimant owns queued work.
- Customize the agent runtime by editing `worker/Dockerfile`.

## Before You Start

You need:

- A [Blaxel workspace](https://docs.blaxel.ai/Get-started) and workspace name for `BL_WORKSPACE`. [Open workspaces](https://app.blaxel.ai/account/workspaces) in the Blaxel Console.
- The [`bl` CLI](https://docs.blaxel.ai/cli-reference/introduction), logged in to that workspace.
- [Docker](https://docs.docker.com/get-started/get-docker/) running locally.
- A [Blaxel API key](https://docs.blaxel.ai/api-reference/introduction) for a service account with access to the workspace; use it as `BL_API_KEY`. [Create one here](https://app.blaxel.ai/workspace/settings/service-accounts) after selecting your workspace.
- [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) beta access and an `ANTHROPIC_API_KEY`. [Create an Anthropic API key](https://platform.claude.com/settings/keys).
- `python3`.

Set up local tooling. The [Blaxel skills](https://docs.blaxel.ai/skills-mcp) step is optional; use it if you want your AI coding agent to understand Blaxel deployments and sandboxes.

```bash
# macOS. For Linux or Windows, use the bl CLI install docs linked above.
brew tap blaxel-ai/blaxel
brew install blaxel
bl login

# Optional, requires npm: give your AI coding agent Blaxel deployment and sandbox skills.
npx skills add blaxel-ai/agent-skills
```

Install repo deps and create your env file:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
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

## Quickstart: Prove The Worker

This phase creates a self-hosted CMA environment, publishes the worker image, creates an agent, then runs one real session by claiming its work item locally and launching `ant beta:worker run` in the matching Blaxel worker sandbox.

Prefer it hands-off? `python3 bootstrap.py` walks the whole flow for you: it reads `.env` directly (no re-`source` between steps), creates the environment and agent, publishes the images, runs the proof, and stops only at the two Anthropic Console actions below (generate the environment key, register the webhook). Run `python3 bootstrap.py --plan` to see the next step without doing anything. The numbered steps below are the same flow done by hand.

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

Creates real Blaxel image state. The worker publish is the heaviest step: expect a roughly 3 GB transformed rootfs and roughly 3.2 GB upload. Later builds can reuse Docker cache, but the transformed image can still be uploaded again.

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

Creates a real Anthropic session and a real Blaxel worker sandbox. Run this before registering the webhook.

```bash
python3 example/run_session.py --local-worker
```

The script refuses to start if the self-hosted environment already has queued work or active pollers. That is intentional: the first claimant owns the work item, so an existing webhook dispatcher, environment-polling worker, or another cookbook worker would make this proof ambiguous.

Checkpoint:

```text
session: sesn_...
message sent
[local-worker] cma-worker-... is running work_... as ant-run-...
  t=  0s status=idle events=3 queue=1
  t= 10s status=running events=24 queue=0
  tool: write {"content": "hello from blaxel", "file_path": "hello.txt"}
  tool: bash {"command": "cat /workspace/hello.txt && echo"}

final agent message: ... hello from blaxel ...
EXAMPLE: PASS
```

The repeating `t=...s` poll lines are expected while the worker cold-starts and runs the turn; the tool lines and `EXAMPLE: PASS` follow once it finishes.

If you get this far, the agent session, environment key, worker image, Blaxel sandbox creation, exact work claiming, `ant beta:worker run`, file tool, bash tool, and result posting are working. The webhook is automation around the same exact-work dispatch path.

Optional wow path: once the worker proof passes, run `python3 example/demo_preview_resume.py`. The agent authors a small web app in `/workspace`, the harness serves it on a public Blaxel preview URL, and the demo confirms the server survives sandbox standby and resume -- a reachable URL you can open in a browser.

After a webhook or another worker has been registered for the same self-hosted environment, do not use `--local-worker` as an isolated proof unless that other claimant is stopped. A passing transcript only proves this Blaxel path when the matching `cma-worker-<session>` sandbox shows the expected `ant-run-*` process for the session's `work_...` item.

## Quickstart: Add The Webhook

The orchestrator is a small FastAPI webhook dispatcher on a Blaxel preview URL. It receives `session.status_run_started`, verifies the Anthropic signature, schedules dispatch, and returns 200 quickly. The background dispatcher readies the session sandbox before claiming work, drains queued work with the SDK, and starts one worker process for each claimed session work item.

### 5. Publish and start the orchestrator

Creates real Blaxel image state, a persistent orchestrator sandbox, and a public preview URL. This image is small compared with the worker; expect the worker cold start to be the slower first-run step.

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

Checkpoint: the transcript should look like the worker-only run: session id, tool calls, final message containing `hello from blaxel`, and `EXAMPLE: PASS`.

`EXAMPLE: PASS` here confirms the session completed, not that this orchestrator's worker served it. If another claimant shares the environment, the transcript can pass while a different worker did the work. To prove this path, check that the matching `cma-worker-<session>` sandbox shows the expected `ant-run-*` process, or read the orchestrator logs for the claimed `work_...` id.

This command also expects a quiet self-hosted environment before it creates the session. If it reports `workers_polling`, stop other workers using the same `ANTHROPIC_ENVIRONMENT_ID` or create a fresh environment for the proof.

## Keys You Need

| Value | Comes from | Lives where | Used for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic Console | local shell only | Create environments, agents, sessions, and read session events. |
| `ANTHROPIC_ENVIRONMENT_ID` | `scripts/create_environment.py` | local shell, orchestrator, worker process | Selects the self-hosted environment. |
| `ANTHROPIC_ENVIRONMENT_KEY` | Anthropic Console environment page | orchestrator and worker process | Scoped, revocable work-queue and session-tool auth. |
| `ANTHROPIC_AGENT_ID` | `scripts/create_agent.py` | local shell | Chooses the agent for example sessions. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | Anthropic Console webhook page | orchestrator | Verifies inbound webhooks. |
| `BL_API_KEY`, `BL_WORKSPACE` | Blaxel service account / workspace | local shell and orchestrator | Lets the orchestrator spawn worker sandboxes. |
| `ORCHESTRATOR_TTL` | optional `.env` tuning | setup process | Sets the orchestrator sandbox max-age/lifecycle backstop. |
| `ORCHESTRATOR_KEEPALIVE_TIMEOUT` | optional `.env` tuning | setup process | Keeps the webhook server active while background dispatch continues after a fast 200 response. |

Never put the org `ANTHROPIC_API_KEY` on the worker. The worker receives only the scoped `ANTHROPIC_ENVIRONMENT_KEY`, and agent-run shell commands can read worker process env vars.

## Debug Fast

| Symptom | Check |
| --- | --- |
| Preflight fails on Anthropic | `ANTHROPIC_API_KEY` is missing, invalid, in the wrong org, or lacks CMA beta access. |
| Worker freezes mid-session | The `ant run` process must start with `keep_alive: True`; outbound-only worker traffic does not by itself keep a Blaxel sandbox active. |
| File tool rejects `/workspace/...` | File tools are scoped to `/workspace` but require relative paths like `hello.txt`. Bash commands can still use absolute paths inside the container. |
| Worker-only run uses `/workspace/hello.txt` with the write tool | You are probably using an old agent. Rerun `python3 scripts/create_agent.py`, replace `ANTHROPIC_AGENT_ID` in `.env`, reload env, and rerun. |
| Example proof reports `workers_polling` | Another worker is polling this self-hosted environment. Stop the environment-polling worker, webhook dispatcher, or other cookbook worker using the same `ANTHROPIC_ENVIRONMENT_ID`, or create a fresh environment for the proof. A worker that just finished can keep `workers_polling` nonzero for up to ~30s, so wait that long and retry before assuming a competing claimant. |
| Tool result is rejected as empty | Shell commands must print something. Append `&& echo ok` after silent redirects. |
| Webhook returns 503 | Rerun `python3 setup.py` after exporting `ANTHROPIC_WEBHOOK_SIGNING_KEY`; if the key is present, inspect the event payload for a missing session id. Worker-start failures happen after the webhook 200 and show up in orchestrator logs. |
| Webhook returns 401 | Confirm the `whsec_...` secret and that `anthropic[webhooks]` is installed in the orchestrator image. |
| Later turns or reclaim retries do not start | Work process names must be derived from `work_...` ids and include a unique suffix. Completed process records persist. |
| `run_session.py --local-worker` exits with `no claimed work appeared` | Another active claimer took the work first: a webhook dispatcher, an always-on `ant beta:worker poll` worker, or a second `--local-worker` polling the same environment. A per-session `ant beta:worker run` worker does not re-claim new work -- it handles its one `work_...` item and exits -- so a leftover `cma-worker-*` only matters if it still holds a lease or trips the quiet-environment check. Stop the other claimant (or use a fresh environment), and clear leftover `cma-worker-*` sandboxes, before an isolated local-worker proof. |
| Transcript passes but the matching Blaxel sandbox has no `ant-run-*` process | Another work claimant handled the item. Stop any other local worker, webhook dispatcher, or cookbook worker using the same self-hosted environment before using the run as proof of this path. |
| Output files are missing | File tools write under `/workspace`; nothing is auto-exported. Bash can also write `/mnt/session/outputs`, but that path is not exposed to contained file tools without `--unrestricted-paths`. |

## How It Works

The integration uses two Blaxel sandbox roles:

- `orchestrator/`: FastAPI webhook dispatcher on a public preview URL. It verifies `whsec_...`, schedules background dispatch, readies session worker sandboxes, claims queued CMA work with the Anthropic SDK, and starts exact worker processes.
- `worker/`: the agent runtime image. For each claimed `work_...`, the orchestrator starts `ant beta:worker run` with `ANTHROPIC_WORK_ID` and `ANTHROPIC_SESSION_ID`; the CLI executes tools in `/workspace`, heartbeats the lease, posts results, and stops the work item.
- One session gets one worker sandbox name derived from the Anthropic session id, so `/workspace` can persist across later turns while the worker sandbox TTL allows.
- `ant beta:worker run` owns the work heartbeat. The dispatcher waits briefly to collect near-simultaneous sessions, readies scheduled and still-queued session sandboxes before claiming work, and bounds process-start retries so the ack-to-run handoff stays short.
- `--max-idle` is passed to `ant beta:worker run` and stops after the session goes idle with `stop_reason=end_turn`. It is not queue-idle cleanup.
- `BLAXEL_WORKER_TTL` is max age from sandbox creation. It is a cleanup backstop, not idle deletion.
- Use one active work-claiming path per self-hosted environment during proof runs. Environment-polling workers, `--local-worker`, webhook dispatchers, and other cookbook workers all compete for the same Anthropic queue.
- Public preview URLs let the orchestrator receive webhooks and let generated apps be reachable during demos.
- Blaxel process logs show the `ant-run-*` process and any supervised app processes inside the sandbox.

For the full integration-guide explanation, read [GUIDE.md](./GUIDE.md).

## Demo Vs Production Security

This cookbook is intentionally broad so the first demo works across many stacks. Before production use, harden the worker image and environment for your trust boundary: avoid broad credentials in worker env, rotate environment keys, restrict egress, review log retention, decide whether Docker and other broad tools belong in the runtime, and treat public preview URLs as public endpoints. `.env` is a local demo secret path, not a production secret manager.

## Layout

```text
orchestrator/   FastAPI webhook -> schedule dispatch -> ready worker -> claim work -> start exact worker process
worker/         agent runtime image: sandbox-api + ant + cloud-sandbox-compatible tools
scripts/        setup helpers for preflight, environment creation, and agent creation
example/        run sessions, validate long runs, demo preview/resume behavior
tests/          local unit tests for setup, scripts, and orchestrator behavior
setup.py        create/reuse the orchestrator sandbox, restart the webhook server, and print its webhook URL
GUIDE.md        narrative source material for the public integration guide
AGENTS.md       fast orientation for AI coding agents
llms.txt        compact machine-readable summary
```

## Tests

Local checks:

```bash
.venv/bin/python -B -m py_compile setup.py orchestrator/app.py example/*.py scripts/*.py
.venv/bin/python -m pytest
```

Worker image smoke:

```bash
docker build --platform linux/amd64 -t cma-worker:smoke worker
docker run --platform linux/amd64 --rm --entrypoint /worker/smoke.sh cma-worker:smoke
```

Real end-to-end checks create Anthropic sessions and Blaxel sandboxes:

```bash
python3 example/run_session.py --local-worker
python3 example/run_session.py
python3 example/demo_preview_resume.py   # optional: agent builds + serves an app on a public preview URL
```

## Teardown

Worker sandboxes have a TTL max age from creation. The worker process exits after the session goes idle for `--max-idle`; TTL is a cleanup backstop, not idle deletion.

The orchestrator sandbox and its public preview URL persist until you remove them:

```bash
bl delete sandbox cma-orchestrator-app --workspace "$BL_WORKSPACE"
```

If a worker is still around while testing, delete the matching worker sandbox after the session finishes:

```bash
bl delete sandbox cma-worker-sesn-... --workspace "$BL_WORKSPACE"
```

If you delete the orchestrator, also remove or disable the Anthropic webhook in the Console. The webhook was registered against the orchestrator's preview URL, so once that sandbox is gone the deliveries silently fail against a dead endpoint. Revoke the environment key and delete throwaway environments or agents when you are done testing.

## Links

- [Anthropic: self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Anthropic: cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference)
- [Anthropic: built-in agent tools](https://platform.claude.com/docs/en/managed-agents/tools)
- [Blaxel docs](https://docs.blaxel.ai)
