# AGENTS.md

Orientation for an AI coding agent, or a human, working in this repo. Read this first for the fastest correct run.

## What this is

Run Claude Managed Agents (CMA) tool execution on Blaxel sandboxes. Anthropic hosts the agent loop and environment work queue; Blaxel provides the self-hosted execution layer.

Two Blaxel sandbox roles:

- `orchestrator/`: FastAPI webhook dispatcher on a public preview URL. On `session.status_run_started`, it verifies the webhook, schedules background dispatch, readies scheduled and still-queued session worker sandboxes, claims queued work with the Anthropic SDK, and starts exact worker processes.
- `worker/`: `ant beta:worker run` runtime. It receives `ANTHROPIC_WORK_ID` and `ANTHROPIC_SESSION_ID`, runs tools in `/workspace`, posts results back, heartbeats, and stops the claimed work item.

Public quickstart: `README.md`. Narrative guide source: `GUIDE.md`. Machine summary: `llms.txt`.

## Prerequisites

- Blaxel workspace, `bl` CLI logged in with `bl login`, Docker running locally, `BL_WORKSPACE`, and a service-account `BL_API_KEY`. `BL_API_KEY` does not replace CLI login for `bl push`.
- Claude Managed Agents beta access and an `ANTHROPIC_API_KEY`.
- `python3`; create a venv and install local deps with `python3 -m venv .venv && source .venv/bin/activate && python -m pip install -r requirements-dev.txt`.
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
| `ANTHROPIC_ENVIRONMENT_KEY` | orchestrator and worker process | Scoped, revocable auth for work claiming and session tool execution. Agent-run shell can read worker env vars. |
| `ANTHROPIC_AGENT_ID` | local shell | Agent to run for example sessions. |
| `ANTHROPIC_AGENT_MODEL` | local shell | Optional override used by `scripts/create_agent.py`; default is `claude-opus-4-8`. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | orchestrator | Webhook signature verification secret from the Anthropic Console. |
| `BL_REGION`, `BLAXEL_WORKER_IMAGE`, `BLAXEL_WORKER_TTL`, `ANT_MAX_IDLE`, `ANT_KEEPALIVE_TIMEOUT`, `ANT_DISPATCHER_POLL_BLOCK_MS`, `ANT_DISPATCHER_RECLAIM_MS`, `ANT_DISPATCHER_DEBOUNCE_MS`, `ANTHROPIC_DISPATCHER_WORKER_ID`, `ANTHROPIC_DIRECT_DISPATCHER_WORKER_ID`, `ANT_RUN_START_ATTEMPTS`, `BLAXEL_WORKER_READY_ATTEMPTS`, `BLAXEL_WORKER_READY_SLEEP`, `ORCHESTRATOR_NAME`, `ORCHESTRATOR_IMAGE`, `ORCHESTRATOR_TTL`, `ORCHESTRATOR_KEEPALIVE_TIMEOUT`, `BLAXEL_WORKER_VOLUME_*`, `BLAXEL_WORKER_PROXY_*` | optional | Runtime tuning and optional Volume/public-preview Proxy paths; see `.env.example`. |

## Commands

| Command | What it does | Side effects |
| -- | -- | -- |
| `.venv/bin/python -B -m py_compile bootstrap.py setup.py orchestrator/app.py orchestrator/blaxel_features.py example/*.py scripts/*.py` | syntax check | local, safe |
| `.venv/bin/python -m pytest` | setup, script, and orchestrator tests | local, safe |
| `docker build --platform linux/amd64 -t cma-worker:smoke worker && docker run --platform linux/amd64 --rm --entrypoint /worker/smoke.sh cma-worker:smoke` | worker runtime smoke test | local Docker only |
| `python3 bootstrap.py --plan` | shows the next setup action without mutation | local, safe |
| `python3 bootstrap.py` | guided setup; stops at Anthropic Console gates | creates real Anthropic/Blaxel resources after preflight |
| `python3 scripts/preflight.py` | checks local tooling and Anthropic access | read-only external API call |
| `python3 example/run_session.py --direct-dispatch` | real session, direct worker spawn | creates Anthropic session + Blaxel sandbox; run before webhook registration for an isolated worker proof |
| `python3 setup.py` | create/reuse orchestrator, restart webhook server, and print preview URL | creates persistent Blaxel sandbox if missing |
| `python3 example/run_session.py` | full webhook flow | creates real session; needs webhook/orchestrator |
| `python3 example/demo_preview_resume.py` | preview URL + standby/resume behavior demo | creates real resources |
| `python3 example/validate_long_session.py` | long keep-alive + filesystem-containment probe | creates real resources |
| `bl push --workspace "$BL_WORKSPACE" --type sandbox` | builds and publishes sandbox image | publishes to the workspace loaded from `.env` |
| `bl get sandbox <worker> process --workspace "$BL_WORKSPACE" -o json` | inspect exact worker processes | read-only Blaxel check |
| `bl logs sandbox <worker> <ant-run-process> --workspace "$BL_WORKSPACE" --period 1h` | inspect exact CMA worker process logs | read-only Blaxel check |

## Where to look

| Path | Responsibility |
| -- | -- |
| `scripts/` | local setup helpers; create scripts print exports and never mutate `.env` |
| `orchestrator/app.py` | webhook verification, fast dispatch scheduling, SDK work claiming, worker sandbox/process launch |
| `worker/Dockerfile` | the agent runtime: `ant`, cloud-sandbox-style language runtimes, database clients, and utilities |
| `setup.py` | create/reuse orchestrator, restart webhook server with current env, print preview URL |
| `example/run_session.py` | primary E2E example; `--direct-dispatch` proves the worker before webhook registration |
| `tests/` | local behavior tests |

## Invariants

- File tools use relative paths only. Use `hello.txt`, not `/workspace/hello.txt`. Bash commands can still use absolute paths inside the container.
- Every tool call must produce non-empty output. For silent shell commands, append `&& echo ok`.
- Launch the `ant beta:worker run` process with `keep_alive: True` plus a timeout cap, or the sandbox can standby while the worker is making outbound calls.
- Launch the orchestrator webhook server with `keep_alive: True`, or background dispatch can freeze after the fast webhook response returns.
- `ant beta:worker run` owns the work heartbeat. Do not send a dispatcher heartbeat before starting it; the worker's first heartbeat must own the lease handoff.
- The dispatcher readies the session sandbox before claiming work and bounds process-start retries so the ack-to-run gap stays short.
- `--max-idle` controls when `ant beta:worker run` exits after the session goes idle with `stop_reason=end_turn`.
- `BLAXEL_WORKER_TTL` is max age from sandbox creation. It is not idle deletion and should be longer than expected sessions.
- Worker sandbox names must be lowercase alphanumerics and hyphens; sanitize Anthropic session ids.
- Use one active work-claiming path per self-hosted environment during proof runs. Environment-polling workers, `--direct-dispatch`, webhook dispatchers, and other cookbook workers all compete for the same Anthropic queue; a transcript only proves this path when the matching Blaxel worker sandbox shows the expected `ant-run-*` process.
- Use one Anthropic environment per Blaxel workspace. The winning claimant creates the worker with its own Blaxel credentials, so `BL_WORKSPACE` does not pin where a shared environment's work lands; in webhook mode `example/run_session.py` verifies the worker sandbox and reports when another claimant ran it.
- `example/run_session.py` refuses to create a proof session while queue stats show queued work or active `workers_polling`; stop the other claimant or use a fresh environment.
- Duplicate webhook deliveries are safe because dispatch scheduling is suppressed per session in-process, currently-starting work handoffs are suppressed in-process, and SDK work claiming is durable; if no queued work remains, another dispatcher likely claimed it.

## Safe vs. company-facing

- Local edits and local checks are allowed.
- Before running commands that create real Anthropic sessions/environments/agents, register webhooks, push sandbox images, create/delete Blaxel sandboxes or Volumes, or otherwise mutate live Anthropic/Blaxel resources, get explicit human approval and name the side effect.
- Do not push, open PRs, merge, change repo visibility, update Linear/GitHub/Slack, or publish docs without explicit human approval.
