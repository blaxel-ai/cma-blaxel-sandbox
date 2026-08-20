# AGENTS.md

Orientation for an AI coding agent, or a human, working in this repo. Read this first for the fastest correct run.

## What this is

Run Claude Managed Agents (CMA) tool execution on Blaxel sandboxes. Anthropic hosts the agent loop and environment work queue; Blaxel provides the self-hosted execution layer.

Two Blaxel sandbox roles:

- `orchestrator/`: FastAPI webhook dispatcher with bounded worker starts, claim retries, and queued-work recovery.
- `worker/`: Quickstart or full SDK `EnvironmentWorker` runtime. It runs tools in `/workspace`, synchronizes attached memory stores, and owns the work heartbeat.

Public quickstart: `README.md`. Narrative guide source: `GUIDE.md`. Machine summary: `llms.txt`.

## Prerequisites

- Blaxel workspace, `bl` CLI logged in with `bl login`, Docker running locally, `BL_WORKSPACE`, and a service-account `BL_API_KEY`. `BL_API_KEY` does not replace CLI login for `bl push`.
- Claude Managed Agents beta access and an `ANTHROPIC_API_KEY`.
- `python3`; create a venv and install locked deps with `python3 -m venv .venv && source .venv/bin/activate && python -m pip install --require-hashes -r requirements-dev.lock`.
- Copy `.env.example` to `.env`, fill it in, then load it with `set -a; source .env; set +a`.

## Setup, in order

Fast path: `python3 bootstrap.py --plan` shows the next action, and `python3 bootstrap.py` runs deterministic setup until the two Anthropic Console gates. Bootstrap reads `.env` directly and uses the default image publish names; use the manual commands below if you need custom `bl push --name ...` image names in a shared workspace.

1. `python3 scripts/preflight.py` checks local tooling and CMA access.
2. `python3 scripts/create_environment.py` prints `export ANTHROPIC_ENVIRONMENT_ID=env_...`.
3. Generate `ANTHROPIC_ENVIRONMENT_KEY` in the Anthropic Console environment page.
4. `(cd worker && bl push --workspace "$BL_WORKSPACE" --type sandbox)` publishes `sandbox/cma-worker:latest`; with a custom `BLAXEL_WORKER_IMAGE`, publish with the matching `bl push --name ...`.
5. `python3 scripts/create_agent.py` prints `export ANTHROPIC_AGENT_ID=agent_...`; set `ANTHROPIC_AGENT_MODEL` first only if the default model is unavailable in the org.
6. `python3 example/run_session.py --direct-dispatch` validates the worker path before webhook registration.
7. `(cd orchestrator && bl push --workspace "$BL_WORKSPACE" --type sandbox)` publishes `sandbox/cma-orchestrator:latest`; with a custom `ORCHESTRATOR_IMAGE`, publish with the matching `bl push --name ...`.
8. `python3 setup.py` creates or reuses the orchestrator, restarts the webhook server, and prints the webhook URL.
9. Register the Anthropic webhook for `session.status_run_started`, copy `whsec_...`, export `ANTHROPIC_WEBHOOK_SIGNING_KEY`, then rerun `python3 setup.py`.
10. `python3 example/run_session.py` runs the full webhook path.

## Environment variables

| Variable | Where it lives | What it is |
| -- | -- | -- |
| `ANTHROPIC_API_KEY` | local shell only | Control-plane key. Creates environments, agents, sessions, and reads events. Never put it on the worker. |
| `BL_API_KEY`, `BL_WORKSPACE` | local shell and orchestrator | Blaxel service-account auth so the orchestrator can spawn workers. |
| `ANTHROPIC_ENVIRONMENT_ID` | local shell, orchestrator, worker process | The self-hosted environment id. |
| `ANTHROPIC_ENVIRONMENT_KEY` | orchestrator and worker fallback | Scoped work-queue auth required by the SDK worker; agent-run bash receives a scrubbed environment. |
| `ANTHROPIC_AGENT_ID`, `ANTHROPIC_AGENT_VERSION` | local shell | Agent and exact version used by examples and the AG-UI adapter. |
| `ANTHROPIC_AGENT_MODEL` | local shell | Optional override used by `scripts/create_agent.py`; default is `claude-sonnet-5`. |
| `ANTHROPIC_INFERENCE_GEO` | local shell | Optional `global` or `us` model inference geography used during agent creation. |
| `ANTHROPIC_ADVISOR_MODEL` | local shell | Optional advisor model added to a coordinator roster during agent creation. |
| `ANTHROPIC_AGENT_SKILLS` | local shell | Optional comma-separated Anthropic skill names or custom `skill_*` ids. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | orchestrator | Webhook signature verification secret from the Anthropic Console. |
| `AG_UI_BUDGET_CENTS`, `AG_UI_TURN_TIMEOUT_MS` | optional application settings | Per-session spend ceiling and cold-start-aware turn timeout for `example/ag-ui/`. |
| `BL_REGION`, `BLAXEL_WORKER_IMAGE`, `BLAXEL_WORKER_TTL`, `ANT_MAX_IDLE`, `ANT_KEEPALIVE_TIMEOUT`, `ANT_DISPATCHER_*`, `ANT_MAX_CONCURRENT_WORKER_STARTS`, `ANTHROPIC_*_WORKER_ID`, `ANT_RUN_START_ATTEMPTS`, `BLAXEL_WORKER_READY_*`, `ORCHESTRATOR_*`, `BLAXEL_WORKER_VOLUME_*`, `BLAXEL_WORKER_PROXY_*` | optional | Runtime tuning and optional Volume/public-preview Proxy paths; see `.env.example`. |

## Commands

| Command | What it does | Side effects |
| -- | -- | -- |
| `.venv/bin/python -B -m compileall -q bootstrap.py cookbook.py setup.py orchestrator example scripts` | syntax check | local, safe |
| `.venv/bin/python -m pytest` | setup, script, and orchestrator tests | local, safe |
| `docker build --platform linux/amd64 --build-arg WORKER_PROFILE=quickstart -t cma-worker:quickstart worker && docker run --platform linux/amd64 --rm --entrypoint /worker/smoke.sh cma-worker:quickstart` | quickstart worker smoke test | local Docker only |
| `python3 bootstrap.py --plan` | shows the next setup action without mutation | local, safe |
| `python3 bootstrap.py` | guided setup; stops at Anthropic Console gates | creates real Anthropic/Blaxel resources after preflight |
| `python3 scripts/preflight.py` | checks local tooling and Anthropic access | read-only external API call |
| `python3 example/run_session.py --direct-dispatch` | real session, direct worker spawn | creates a budgeted Anthropic session + Blaxel sandbox |
| `python3 setup.py` | create/reuse orchestrator, restart webhook server, and print preview URL | creates persistent Blaxel sandbox if missing |
| `python3 example/run_session.py` | full webhook flow | creates real session; needs webhook/orchestrator |
| `(cd example/ag-ui && npm ci && npm run dev)` | local CopilotKit/AG-UI chat over the configured environment | each new thread creates a budgeted session; needs webhook/orchestrator |
| `python3 example/demo_preview_resume.py` | preview URL + standby/resume behavior demo | creates real resources |
| `python3 example/validate_long_session.py` | long keep-alive + filesystem-containment probe | creates real resources |
| `python3 cookbook.py status` | queue, agent, session receipt, worker, and Volume status | read-only external API calls |
| `python3 cookbook.py cleanup --session sesn_...` | exact cleanup plan | local, safe |
| `bl push --workspace "$BL_WORKSPACE" --type sandbox` | builds and publishes sandbox image | publishes to the workspace loaded from `.env` |
| `bl get sandbox <worker> process --workspace "$BL_WORKSPACE" -o json` | inspect exact worker processes | read-only Blaxel check |
| `bl logs sandbox <worker> <cma-run-process> --workspace "$BL_WORKSPACE" --period 1h` | inspect exact CMA worker process logs | read-only Blaxel check |

## Where to look

| Path | Responsibility |
| -- | -- |
| `scripts/` | local setup helpers; create scripts print exports and never mutate `.env` |
| `orchestrator/app.py` | webhook verification, fast dispatch scheduling, SDK work claiming, worker sandbox/process launch |
| `worker/Dockerfile` | quickstart and full agent runtime profiles |
| `setup.py` | create/reuse orchestrator, restart webhook server with current env, print preview URL |
| `example/run_session.py` | primary E2E example; `--direct-dispatch` proves the worker before webhook registration |
| `example/ag-ui/` | one-process CopilotKit/AG-UI chat; one UI thread maps to one managed session |
| `example/session_runtime.py` | SDK streaming, pagination, terminal errors, budgets, timeouts, and receipts |
| `cookbook.py` | read-only status and exact per-session cleanup |
| `tests/` | local behavior tests |

## Invariants

- File tools may use relative paths or absolute paths that resolve inside `/workspace`; paths outside the workdir remain rejected. Bash can access other paths inside the container.
- Every tool call must produce non-empty output. For silent shell commands, append `&& echo ok`.
- Launch `/worker/worker.py` with `keep_alive: True` plus a timeout cap, or the sandbox can standby while the worker is making outbound calls.
- Launch the orchestrator webhook server with `keep_alive: True`, or background dispatch can freeze after the fast webhook response returns.
- The SDK EnvironmentWorker owns the work heartbeat. Do not send a dispatcher heartbeat before starting it; the worker's first heartbeat must own the lease handoff.
- The dispatcher readies the session sandbox before claiming work and bounds process-start retries so the ack-to-run gap stays short.
- `ANT_MAX_IDLE` controls when the SDK worker exits after the session goes idle with `stop_reason=end_turn`.
- A hard session budget stops new model calls with `stop_reason=budget_reached`. Raise the ceiling above consumed list cost to resume; do not treat it as `end_turn`.
- Self-hosted sessions support memory-store resources through the SDK worker. Use metadata plus explicit staging for file and repository inputs.
- `BLAXEL_WORKER_TTL` is max age from sandbox creation. It is not idle deletion and should be longer than expected sessions.
- Worker sandbox names must be lowercase alphanumerics and hyphens; sanitize Anthropic session ids.
- Use one active work-claiming path per self-hosted environment during proof runs. Environment-polling workers, `--direct-dispatch`, webhook dispatchers, and other cookbook workers all compete for the same Anthropic queue; a transcript only proves this path when the matching Blaxel worker sandbox shows the expected `cma-run-*` process.
- Use one Anthropic environment per Blaxel workspace. The winning claimant creates the worker with its own Blaxel credentials, so `BL_WORKSPACE` does not pin where a shared environment's work lands; in webhook mode `example/run_session.py` verifies the worker sandbox and reports when another claimant ran it.
- `example/run_session.py` refuses to create a proof session only while queue depth or pending claims are nonzero. `workers_polling` is recent activity, not proof of contention.
- Duplicate webhook deliveries are safe because SDK work claiming is durable. A recovery loop handles delayed webhooks and standby resume.

## Safe vs. company-facing

- Local edits and local checks are allowed.
- Before running commands that create real Anthropic sessions/environments/agents, register webhooks, push sandbox images, create/delete Blaxel sandboxes or Volumes, or otherwise mutate live Anthropic/Blaxel resources, get explicit human approval and name the side effect.
- Do not push, open PRs, merge, change repo visibility, update Linear/GitHub/Slack, or publish docs without explicit human approval.
