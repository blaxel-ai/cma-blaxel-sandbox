# Claude Managed Agents on Blaxel Sandboxes

This cookbook proves the self-hosted Claude Managed Agents path on Blaxel. Anthropic runs the agent loop, session state, event history, and self-hosted environment queue; Blaxel runs the execution layer, where an orchestrator sandbox claims exact CMA work items and a per-session worker sandbox runs the tool calls.

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
- Customize the agent runtime by editing `worker/Dockerfile`.

## Starting State And Human Gates

The quickstart is self-contained after a human supplies accounts/access and completes two Anthropic Console-only actions. The cookbook cannot create a Blaxel workspace, grant Claude Managed Agents beta access, or generate Console-only secrets for you. Everything else in the main path is driven by the commands below or by `python3 bootstrap.py`.

Human-provided starting values:

- `ANTHROPIC_API_KEY`: Anthropic control-plane key with Managed Agents beta access.
- `BL_WORKSPACE`: Blaxel workspace name to create test sandboxes in.
- `BL_API_KEY`: Blaxel service-account key for that workspace. It must be able to create, read, update, and delete sandboxes, start/read/kill sandbox processes, and read logs/previews. Optional Volume and Proxy paths also need matching Volume/Proxy permissions and quota. If your workspace uses named service-account roles, choose or request a role that grants those capabilities; the cookbook cannot infer or elevate them.

Two later Console gates are unavoidable:

- Environment key: after `scripts/create_environment.py`, open the Anthropic Console, select the workspace/project for your `ANTHROPIC_API_KEY`, open Managed Agents > Environments, choose the created `env_...`, click **Generate environment key**, copy the one-time `sk-ant-oat01-...` value, and add it as `ANTHROPIC_ENVIRONMENT_KEY`.
- Webhook signing secret: after `setup.py` prints the Blaxel preview URL, create a Managed Agents webhook in the same Anthropic workspace/project, subscribe only to `session.status_run_started`, copy the one-time `whsec_...` signing secret, and add it as `ANTHROPIC_WEBHOOK_SIGNING_KEY`.

The `.env.example` file uses `export NAME=value` lines, and setup scripts print exports in the same shape. Paste those lines as-is. If the key already exists with an empty value, replace that line instead of appending a duplicate. `bootstrap.py` reads `.env` directly and can append generated ids for you, with a one-time `.env.bak` backup.

## Before You Start

You need:

- A [Blaxel workspace](https://docs.blaxel.ai/Get-started) and workspace name for `BL_WORKSPACE`. [Open workspaces](https://app.blaxel.ai/account/workspaces) in the Blaxel Console.
- The [`bl` CLI](https://docs.blaxel.ai/cli-reference/introduction), logged in to that workspace.
- [Docker](https://docs.docker.com/get-started/get-docker/) running locally.
- A [Blaxel API key](https://docs.blaxel.ai/api-reference/introduction) for a service account with access to the workspace; use it as `BL_API_KEY`. [Create one here](https://app.blaxel.ai/workspace/settings/service-accounts) after selecting your workspace.
- [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) beta access and an `ANTHROPIC_API_KEY`. [Create an Anthropic API key](https://platform.claude.com/settings/keys).
- `python3`.

The links above are references; the cookbook path itself is below. Set up local tooling. The [Blaxel skills](https://docs.blaxel.ai/skills-mcp) step is optional; use it if you want your AI coding agent to understand Blaxel deployments and sandboxes.

macOS:

```bash
brew tap blaxel-ai/blaxel
brew install blaxel
bl login
```

Linux or WSL:

```bash
curl -fsSL https://raw.githubusercontent.com/blaxel-ai/toolkit/main/install.sh | sh
bl login
```

Optional, requires npm: give your AI coding agent Blaxel deployment and sandbox skills.

```bash
npx skills add blaxel-ai/agent-skills
```

`bl login` may open a browser; a human may need to complete that auth step. `BL_API_KEY` is still required because the orchestrator sandbox uses it to spawn workers, but it does not replace CLI login for `bl push`.

Docker must be running before the worker image can build:

```bash
docker info
```

Verify the local shell can see the expected tools and workspace:

```bash
python3 --version
bl version
bl workspaces
bl workspaces --current
docker info
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
export ANTHROPIC_API_KEY=sk-ant-api03-...
export BL_WORKSPACE=...
export BL_API_KEY=...
```

Load it:

```bash
set -a; source .env; set +a
```

## Quickstart: Prove The Worker

This phase creates a self-hosted CMA environment, publishes the worker image, creates an agent, then runs one real session by claiming its work item locally and launching `ant beta:worker run` in the matching Blaxel worker sandbox.

Prefer it hands-off? `python3 bootstrap.py` walks the whole flow for you: it reads `.env` directly (no re-`source` between steps), creates the environment and agent, publishes the images, runs the proof, and stops only at the two Anthropic Console actions below (generate the environment key, register the webhook). Run `python3 bootstrap.py --plan` to see the next step without doing anything. Bootstrap uses the default image publish names, so use the manual commands if you need custom image names in a shared workspace. The numbered steps below are the same flow done by hand.

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
export ANTHROPIC_ENVIRONMENT_ID=env_...
```

Pause here: open the environment in the Anthropic Console, click **Generate environment key**, and add it to `.env`:

```text
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

Reload env:

```bash
set -a; source .env; set +a
```

### Optional: isolate shared-workspace resources

If you are testing in a shared workspace, the simplest isolation is a throwaway Blaxel workspace plus a throwaway Anthropic environment. If you must share a workspace, give the cookbook resources literal unique names. Do not leave shell interpolation such as `${COOKBOOK_SUFFIX}` in `.env` if you use `bootstrap.py`, because bootstrap reads `.env` directly instead of sourcing it through a shell.

Example `.env` values:

```text
export ORCHESTRATOR_NAME=cma-orchestrator-app-mst-20260605
export ORCHESTRATOR_IMAGE=sandbox/cma-orchestrator-mst-20260605:latest
export BLAXEL_WORKER_IMAGE=sandbox/cma-worker-mst-20260605:latest
```

With custom image names, publish with matching `--name` values in steps 3 and 5. The default commands publish `sandbox/cma-worker:latest` and `sandbox/cma-orchestrator:latest`.

After adding these optional values to `.env`, reload it before publishing images or running setup. `bl push --name cma-worker-mst-20260605` publishes the image referenced by `BLAXEL_WORKER_IMAGE=sandbox/cma-worker-mst-20260605:latest`; `bl push --name cma-orchestrator-mst-20260605` publishes the image referenced by `ORCHESTRATOR_IMAGE=sandbox/cma-orchestrator-mst-20260605:latest`.

### 3. Publish the worker and create the agent

Creates real Blaxel image state. The worker publish is the heaviest step: expect a roughly 3 GB transformed rootfs and roughly 3.2 GB upload. Later builds can reuse Docker cache, but the transformed image can still be uploaded again.

```bash
( cd worker && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 scripts/create_agent.py
```

If you set `BLAXEL_WORKER_IMAGE=sandbox/cma-worker-mst-20260605:latest`, publish the worker with the matching name:

```bash
( cd worker && bl push --name cma-worker-mst-20260605 --workspace "$BL_WORKSPACE" --type sandbox )
python3 scripts/create_agent.py
```

Copy the printed export into `.env`:

```text
export ANTHROPIC_AGENT_ID=agent_...
```

Reload env:

```bash
set -a; source .env; set +a
```

The default model is `claude-opus-4-8`. Override it with `ANTHROPIC_AGENT_MODEL` in `.env`, reload env, then run `python3 scripts/create_agent.py` if that model is not available in your Anthropic org.

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

Blaxel process proof:
  sandbox: cma-worker-sesn-...
  process: ant-run-...
  inspect: bl get sandbox cma-worker-sesn-... process --workspace <workspace> -o json
```

The repeating `t=...s` poll lines are expected while the worker cold-starts and runs the turn; the tool lines and `EXAMPLE: PASS` follow once it finishes.

If you get this far, the agent session, environment key, worker image, Blaxel sandbox creation, exact work claiming, `ant beta:worker run`, file tool, bash tool, and result posting are working. The webhook is automation around the same exact-work dispatch path.

Optional app preview demo: once the worker proof passes, run `python3 example/demo_preview_resume.py`. The agent authors a small web app in `/workspace`, the harness starts it as a supervised Blaxel sandbox process, serves it on a Blaxel preview URL, and confirms the server survives sandbox standby/resume. Add `--private-preview` to create a token-protected preview URL instead of a public one.

After a webhook or another worker has been registered for the same self-hosted environment, `--local-worker` is no longer isolated unless that other claimant is stopped. Quiet queue stats before the session do not prove isolation if Anthropic can still deliver `session.status_run_started` to a registered webhook. A passing transcript only proves this Blaxel path when the matching `cma-worker-<session>` sandbox shows the expected `ant-run-*` process for the session's `work_...` item.

Blaxel process records are the mechanical proof that the work ran in the expected sandbox. `run_session.py` prints the exact worker sandbox and process names; copy those values into the inspection commands:

```bash
bl get sandbox <cma-worker-sesn-...> process --workspace "$BL_WORKSPACE" -o json
bl logs sandbox <cma-worker-sesn-...> <ant-run-...> --workspace "$BL_WORKSPACE" --period 1h
```

Pass criteria: the worker sandbox name matches the session id, the process name starts with `ant-run-`, the command is `ant beta:worker run --workdir /workspace`, and the process logs show this session running the expected `write` and `bash` tool dispatches. `EXAMPLE: PASS` proves completion; the process record proves worker attribution.

Required proof bundle:

```text
session id: sesn_...
claimed work id: work_...
worker sandbox: cma-worker-sesn-...
worker process: ant-run-...
transcript: EXAMPLE: PASS
process proof: bl get sandbox ... shows ant beta:worker run --workdir /workspace
log proof: bl logs ... shows the same session/work and the write + bash tool dispatches
```

The exact log text can vary by `ant` version, but the signal should look like a single session/work id flowing through tool dispatches:

```text
... session_id=sesn_... work_id=work_... tool=write ... is_error=false
... session_id=sesn_... work_id=work_... tool=bash ... is_error=false
```

## What Blaxel Adds After The Proof

The default path keeps setup short and already gives you worker-side proof: a Blaxel sandbox process record for the exact `work_...` item. From there, opt into the Blaxel capabilities that naturally fit CMA tool execution: preview URLs for agent-built apps, per-session Volumes for longer-lived project state, and public-preview Proxy routing for outbound calls that need secret injection without putting broad secrets in the worker environment.

Proxy is public preview and region-dependent, so keep it opt-in and run it in a proxy-supported region that matches the worker sandbox.

Optional upgrade preflights:

- Volumes: run `bl get volumes --workspace "$BL_WORKSPACE" -o json` to confirm the CLI can read Volume state. An empty list is fine; `Quota exceeded: 0/0 volumes` during creation means this workspace cannot live-prove the Volume path.
- Proxy: keep `BLAXEL_WORKER_PROXY_SKIP_REGION_CHECK=false` unless you have separately confirmed regional support. The cookbook checks Blaxel platform configuration and fails fast when `BL_REGION` does not report Proxy availability.
- Private previews: use `--private-preview` for token-protected previews. For manual browser testing, rerun with `--print-preview-token` and pass the token as `?bl_preview_token=<token>` or the `X-Blaxel-Preview-Token` header.

### Real-time previews for agent-built apps

Run the preview/resume demo after the worker-only proof:

```bash
python3 example/demo_preview_resume.py
```

The agent writes an app in `/workspace`; the harness starts it as a supervised sandbox process on port 3000, creates a Blaxel preview URL, checks the process/log API, then proves the server survives standby/resume. This is the strongest demo path: CMA creates the code, and Blaxel makes the running app reachable.

For a token-protected preview, run:

```bash
python3 example/demo_preview_resume.py --private-preview
```

The private mode creates a preview token, verifies that the preview is not reachable without the token, then calls it with `X-Blaxel-Preview-Token`. By default the token is not printed; add `--print-preview-token` only when you need to open the private URL manually during a demo.

### Volume-backed `/workspace`

Use a per-session Blaxel Volume when the agent's project files should survive worker sandbox deletion or recreation. This is opt-in because it requires Volume quota in the selected workspace and each Volume can attach to one sandbox at a time, so the cookbook creates one Volume per CMA session instead of sharing a Volume across concurrent sessions.

Add this to `.env`, reload it, then run either `--local-worker` or the webhook path:

```text
export BL_REGION=us-pdx-1
export BLAXEL_WORKER_VOLUME_ENABLED=true
export BLAXEL_WORKER_VOLUME_PREFIX=cma-workspace
export BLAXEL_WORKER_VOLUME_SIZE_MB=2048
```

When enabled, the worker creation code creates/reuses `cma-workspace-<session>` in `BL_REGION` and mounts it at `/workspace`, which is exactly where the CMA file tools operate. If the workspace reports `Quota exceeded: 0/0 volumes`, leave this disabled for the quickstart or use a quota-enabled workspace before treating the Volume path as live-proved.

### Public-preview Proxy secret injection for outbound API calls

Use Blaxel Proxy secret injection when code inside the worker needs to call an external API without the agent process receiving that API key as an environment variable. The raw value is passed to the orchestrator so it can configure the worker sandbox; it is not passed to `ant beta:worker run`.

This is an advanced public-preview, region-dependent path. Use it when your demo or deployment accepts public-preview Blaxel features, and run the worker in a proxy-supported region.

```text
export BL_REGION=us-pdx-1
export BLAXEL_WORKER_PROXY_DESTINATIONS=api.example.com
export BLAXEL_WORKER_PROXY_HEADER_NAME=Authorization
export BLAXEL_WORKER_PROXY_SECRET_NAME=api-token
export BLAXEL_WORKER_PROXY_SECRET_VALUE=...
```

The cookbook checks Blaxel's platform configuration before enabling Proxy and fails fast if `BL_REGION` is missing or does not report `proxyAvailable: true`.

The worker sandbox is created with a proxy route that injects:

```text
Authorization: Bearer {{SECRET:api-token}}
```

for outbound requests to `api.example.com`. You can also set `BLAXEL_WORKER_PROXY_ALLOWED_DOMAINS`, `BLAXEL_WORKER_PROXY_FORBIDDEN_DOMAINS`, and `BLAXEL_WORKER_PROXY_BYPASS` as comma-separated domain lists.

## Quickstart: Add The Webhook

The orchestrator is a small FastAPI webhook dispatcher on a Blaxel preview URL. It receives `session.status_run_started`, verifies the Anthropic signature, schedules dispatch, and returns 200 quickly. The background dispatcher readies the session sandbox before claiming work, drains queued work with the SDK, and starts one worker process for each claimed session work item.

### 5. Publish and start the orchestrator

Creates real Blaxel image state, a persistent orchestrator sandbox, and a public preview URL. This image is small compared with the worker; expect the worker cold start to be the slower first-run step.

```bash
( cd orchestrator && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 setup.py
```

If you set `ORCHESTRATOR_IMAGE=sandbox/cma-orchestrator-mst-20260605:latest`, publish the orchestrator with the matching name:

```bash
( cd orchestrator && bl push --name cma-orchestrator-mst-20260605 --workspace "$BL_WORKSPACE" --type sandbox )
python3 setup.py
```

Checkpoint:

```text
=== Register this as the Anthropic webhook URL ===
  https://<id>.preview.bl.run/webhook
```

### 6. Register the webhook

Human gate: in the Anthropic Console, select the same workspace/project that owns your `ANTHROPIC_API_KEY`, create a Managed Agents webhook for this setup, subscribe only to `session.status_run_started`, set the destination to the exact `https://<id>.preview.bl.run/webhook` URL printed by `setup.py`, then copy the one-time `whsec_...` signing secret into `.env` as `ANTHROPIC_WEBHOOK_SIGNING_KEY`.

Copy the one-time `whsec_...` signing secret into `.env`:

```text
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
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

`EXAMPLE: PASS` here confirms the session completed, not that this orchestrator's worker served it. If another claimant shares the environment, the transcript can pass while a different worker did the work. To prove this path, check that the matching `cma-worker-<session>` sandbox shows the expected `ant-run-*` process and read that process log:

```bash
bl get sandbox <cma-worker-sesn-...> process --workspace "$BL_WORKSPACE" -o json
bl logs sandbox <cma-worker-sesn-...> <ant-run-...> --workspace "$BL_WORKSPACE" --period 1h
```

If the worker proof is still ambiguous, inspect the orchestrator logs for the same session or claimed `work_...` id:

```bash
bl logs sandbox "${ORCHESTRATOR_NAME:-cma-orchestrator-app}" --workspace "$BL_WORKSPACE" --period 1h --search "sesn_"
bl logs sandbox "${ORCHESTRATOR_NAME:-cma-orchestrator-app}" --workspace "$BL_WORKSPACE" --period 1h --search "work_"
```

This command also expects a quiet self-hosted environment before it creates the session. If it reports `workers_polling`, stop other workers using the same `ANTHROPIC_ENVIRONMENT_ID` or create a fresh environment for the proof.

## Proof Isolation Reset

Use one active work-claiming path per self-hosted environment while proving the cookbook. If `run_session.py` reports queued work, active `workers_polling`, or a transcript passes without the matching Blaxel process proof, wait about 30 seconds for recently completed workers to fall out of queue stats, then retry once.

If it is still noisy, remove the competing claimant before creating another proof session:

```bash
# Stop this cookbook's webhook dispatcher if it is registered for the same environment.
bl delete sandbox "${ORCHESTRATOR_NAME:-cma-orchestrator-app}" --workspace "$BL_WORKSPACE"

# Inspect leftover workers; delete only workers whose sessions are no longer active.
bl get sandboxes --workspace "$BL_WORKSPACE" -o json
bl delete sandbox cma-worker-sesn-... --workspace "$BL_WORKSPACE"
```

If you cannot identify the claimant, create a fresh Anthropic environment with `python3 scripts/create_environment.py`, generate a new environment key, update `.env`, reload env, and rerun the worker proof before registering any webhook for that new environment.

If you cannot prove every claimant is stopped, do not use the transcript as attribution proof. Create a fresh environment and rerun the worker-only proof before registering any webhook.

## Keys You Need

| Value | Comes from | Lives where | Used for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic Console | local shell only | Create environments, agents, sessions, and read session events. |
| `ANTHROPIC_ENVIRONMENT_ID` | `scripts/create_environment.py` | local shell, orchestrator, worker process | Selects the self-hosted environment. |
| `ANTHROPIC_ENVIRONMENT_KEY` | Anthropic Console environment page | orchestrator and worker process | Scoped, revocable work-queue and session-tool auth. |
| `ANTHROPIC_AGENT_ID` | `scripts/create_agent.py` | local shell | Chooses the agent for example sessions. |
| `ANTHROPIC_AGENT_MODEL` | optional `.env` override before `scripts/create_agent.py` | local shell | Chooses a different Claude model if the default is unavailable in your Anthropic org. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | Anthropic Console webhook page | orchestrator | Verifies inbound webhooks. |
| `BL_API_KEY`, `BL_WORKSPACE` | Blaxel service account / workspace | local shell and orchestrator | Lets the orchestrator spawn worker sandboxes. |
| `BLAXEL_WORKER_PROXY_SECRET_VALUE` | optional public-preview `.env` upgrade | orchestrator only | Configures Blaxel Proxy secret injection for worker outbound requests; not passed to the worker process env. Use a proxy-supported worker region. |
| `ORCHESTRATOR_TTL` | optional `.env` tuning | setup process | Sets the orchestrator sandbox max-age/lifecycle backstop. |
| `ORCHESTRATOR_KEEPALIVE_TIMEOUT` | optional `.env` tuning | setup process | Keeps the webhook server active while background dispatch continues after a fast 200 response. |

Never put the org `ANTHROPIC_API_KEY` on the worker. The worker receives only the scoped `ANTHROPIC_ENVIRONMENT_KEY`, and agent-run shell commands can read worker process env vars.

The orchestrator env is an explicit allowlist in `setup.py`; `ANTHROPIC_API_KEY` and `ANTHROPIC_AGENT_ID` are intentionally excluded. Worker process env is narrower still: `ANTHROPIC_WORK_ID`, `ANTHROPIC_SESSION_ID`, `ANTHROPIC_ENVIRONMENT_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, and optional `ANTHROPIC_BASE_URL`.

## Debug Fast

| Symptom | Check |
| --- | --- |
| `bl push` is not authenticated | Run `bl login` and confirm `bl workspaces --current`. `BL_API_KEY` is for SDK calls and the orchestrator; it does not replace CLI login for publishing images. |
| Preflight fails on Anthropic | `ANTHROPIC_API_KEY` is missing, invalid, in the wrong org, or lacks CMA beta access. |
| `.env` has duplicate generated values | Keep the newest intended value and remove stale duplicates. The scripts print `export NAME=value`; paste that shape directly or let `bootstrap.py` append generated ids. |
| Agent creation fails on name/model access | Set `ANTHROPIC_AGENT_MODEL` to a Managed Agents model enabled for your Anthropic org, optionally set `ANTHROPIC_AGENT_NAME`, reload env, and rerun `python3 scripts/create_agent.py`. |
| Worker freezes mid-session | The `ant run` process must start with `keep_alive: True`; outbound-only worker traffic does not by itself keep a Blaxel sandbox active. |
| File tool rejects `/workspace/...` | File tools are scoped to `/workspace` but require relative paths like `hello.txt`. Bash commands can still use absolute paths inside the container. |
| Worker-only run uses `/workspace/hello.txt` with the write tool | You are probably using an old agent. Rerun `python3 scripts/create_agent.py`, replace `ANTHROPIC_AGENT_ID` in `.env`, reload env, and rerun. |
| Example proof reports `workers_polling` | Another worker is polling this self-hosted environment. Stop the environment-polling worker, webhook dispatcher, or other cookbook worker using the same `ANTHROPIC_ENVIRONMENT_ID`, or create a fresh environment for the proof. A worker that just finished can keep `workers_polling` nonzero for up to ~30s, so wait that long and retry before assuming a competing claimant. |
| Tool result is rejected as empty | Shell commands must print something. Append `&& echo ok` after silent redirects. |
| Webhook returns 503 | Rerun `python3 setup.py` after exporting `ANTHROPIC_WEBHOOK_SIGNING_KEY`; if the key is present, inspect the event payload for a missing session id. Worker-start failures happen after the webhook 200 and show up in orchestrator logs. |
| Webhook returns 401 | Confirm the `whsec_...` secret and that `anthropic[webhooks]` is installed in the orchestrator image. |
| Later turns or reclaim retries do not start | Work process names must be derived from `work_...` ids and include a unique suffix. Completed process records persist. |
| Volume setup reports `Quota exceeded: 0/0 volumes` | This workspace cannot create Volumes. Disable `BLAXEL_WORKER_VOLUME_ENABLED` for the quickstart, or request Volume quota before using the Volume-backed `/workspace` path. |
| Proxy setup says `BL_REGION` is missing or unsupported | Set `BL_REGION` to the worker sandbox region and choose a region where Blaxel platform configuration reports `proxyAvailable: true`. |
| `run_session.py --local-worker` exits with `no claimed work appeared` | Another active claimer took the work first: a webhook dispatcher, an always-on `ant beta:worker poll` worker, or a second `--local-worker` polling the same environment. A per-session `ant beta:worker run` worker does not re-claim new work -- it handles its one `work_...` item and exits -- so a leftover `cma-worker-*` only matters if it still holds a lease or trips the quiet-environment check. Stop the other claimant (or use a fresh environment), and clear leftover `cma-worker-*` sandboxes, before an isolated local-worker proof. |
| Transcript passes but the matching Blaxel sandbox has no `ant-run-*` process | Another work claimant handled the item. Stop any other local worker, webhook dispatcher, or cookbook worker using the same self-hosted environment before using the run as proof of this path. |
| Private preview works in the script but not in a browser | Private previews require either a `bl_preview_token` query parameter or the `X-Blaxel-Preview-Token` header. Rerun the demo with `--print-preview-token` only for manual testing. |
| Output files are missing | File tools write under `/workspace`; nothing is auto-exported. Bash can also write `/mnt/session/outputs`, but that path is not exposed to contained file tools without `--unrestricted-paths`. |

## How It Works

The integration uses two Blaxel sandbox roles:

- `orchestrator/`: FastAPI webhook dispatcher on a public preview URL. It verifies `whsec_...`, schedules background dispatch, readies session worker sandboxes, claims queued CMA work with the Anthropic SDK, and starts exact worker processes.
- `worker/`: the agent runtime image. For each claimed `work_...`, the orchestrator starts `ant beta:worker run` with `ANTHROPIC_WORK_ID` and `ANTHROPIC_SESSION_ID`; the CLI executes tools in `/workspace`, heartbeats the lease, posts results, and stops the work item.
- One session gets one worker sandbox name derived from the Anthropic session id, so `/workspace` can persist across later turns while the worker sandbox TTL allows.
- If `BLAXEL_WORKER_VOLUME_ENABLED=true`, the cookbook creates/reuses a per-session Blaxel Volume and mounts it at `/workspace`, so the agent's project files are backed by persistent storage instead of only the sandbox writable layer. This requires Volume quota and `BL_REGION`.
- If `BLAXEL_WORKER_PROXY_DESTINATIONS` and `BLAXEL_WORKER_PROXY_SECRET_VALUE` are set, the worker sandbox is created with Blaxel Proxy routing for server-side header injection. The proxy feature is public preview and region-dependent, so it stays opt-in and should run in a proxy-supported region matching the worker sandbox.
- `ant beta:worker run` owns the work heartbeat. The dispatcher waits briefly to collect near-simultaneous sessions, readies scheduled and still-queued session sandboxes before claiming work, and bounds process-start retries so the ack-to-run handoff stays short.
- `--max-idle` is passed to `ant beta:worker run` and stops after the session goes idle with `stop_reason=end_turn`. It is not queue-idle cleanup.
- `BLAXEL_WORKER_TTL` is max age from sandbox creation. It is a Blaxel lifecycle backstop for abandoned workers, not idle deletion; standby/resume keeps inactive sandboxes snapshotted and reusable until TTL or an expiration policy deletes them.
- Preview URLs let the orchestrator receive webhooks and let generated apps be reachable during demos; generated apps can use public previews or token-protected private previews.
- Blaxel process records and logs show the `ant-run-*` process and any supervised app processes inside the sandbox.

For the full integration-guide explanation, read [GUIDE.md](./GUIDE.md).

## Demo Vs Production Security

This cookbook is intentionally broad so the first demo works across many stacks. Before production use, harden the worker image and environment for your trust boundary: avoid broad credentials in worker env, rotate environment keys, restrict egress, review log retention, decide whether Docker and other broad tools belong in the runtime, and treat public preview URLs as public endpoints. `.env` is a local demo secret path, not a production secret manager. If you opt into public-preview Proxy secret injection, remember that the orchestrator configures the secret-bearing route and Blaxel stores proxy secrets server-side; the worker process still must not receive broad org credentials.

## Layout

```text
orchestrator/   FastAPI webhook -> schedule dispatch -> ready worker -> claim work -> start exact worker process
worker/         agent runtime image: sandbox-api + ant + cloud-sandbox-compatible tools
scripts/        setup helpers for preflight, environment creation, and agent creation
example/        run sessions, validate long keep-alive/containment, demo preview/resume behavior
tests/          local unit tests for setup, scripts, and orchestrator behavior
setup.py        create/reuse the orchestrator sandbox, restart the webhook server, and print its webhook URL
GUIDE.md        narrative source material for the public integration guide
AGENTS.md       fast orientation for AI coding agents
llms.txt        compact machine-readable summary
```

## Tests

Local checks:

```bash
.venv/bin/python -B -m py_compile bootstrap.py setup.py orchestrator/app.py orchestrator/blaxel_features.py example/*.py scripts/*.py
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
python3 example/demo_preview_resume.py   # optional: agent builds + serves an app on a preview URL
python3 example/demo_preview_resume.py --private-preview
python3 example/validate_long_session.py # optional: long keep-alive + file-tool containment probe
```

## Teardown

Worker sandboxes have a TTL max age from creation. The worker process exits after the session goes idle for `--max-idle`; TTL is a Blaxel lifecycle backstop for abandoned workers, while standby/resume handles ordinary inactivity.

The orchestrator sandbox and its public preview URL persist until you remove them. Use your custom `ORCHESTRATOR_NAME` if you set one:

```bash
bl delete sandbox "${ORCHESTRATOR_NAME:-cma-orchestrator-app}" --workspace "$BL_WORKSPACE"
```

If a worker is still around while testing, delete the matching worker sandbox after the session finishes:

```bash
bl get sandboxes --workspace "$BL_WORKSPACE" -o json
bl delete sandbox cma-worker-sesn-... --workspace "$BL_WORKSPACE"
```

If you enabled Volumes, delete the matching per-session Volume after the worker is gone:

```bash
bl get volumes --workspace "$BL_WORKSPACE" -o json
bl delete volume cma-workspace-sesn-... --workspace "$BL_WORKSPACE"
```

If you changed Proxy destinations, domain filters, or proxy secrets during a proof run, delete old `cma-worker-*` test workers before rerunning. Existing workers may still have the previous sandbox routing config.

Proxy config for this cookbook is attached to worker sandbox creation, so deleting old `cma-worker-*` test sandboxes is the intended cleanup for changed Proxy routes, domain filters, and injected secrets. If your workspace exposes separate Proxy secret resources, delete the throwaway secret named by `BLAXEL_WORKER_PROXY_SECRET_NAME` as well.

Published sandbox images are intentionally retained for reuse by default; this teardown removes runtime resources, not registry artifacts. If you used unique throwaway image names and your workspace exposes a supported image cleanup path, delete those image artifacts too.

Also clean up Anthropic Console state when you are done testing:

- Disable or delete the webhook registered to the Blaxel preview URL.
- Revoke the environment key if it was only for this cookbook run.
- Delete throwaway environments and agents if you created them only for testing.

Final cleanup check: `bl get sandboxes --workspace "$BL_WORKSPACE" -o json` should no longer show cookbook orchestrator or worker sandboxes, `bl get volumes --workspace "$BL_WORKSPACE" -o json` should no longer show `cma-workspace-*` test Volumes, and the Anthropic webhook should no longer point at a deleted preview URL.

## Links

- [Anthropic: self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Anthropic: cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference)
- [Anthropic: built-in agent tools](https://platform.claude.com/docs/en/managed-agents/tools)
- [Blaxel docs](https://docs.blaxel.ai)
