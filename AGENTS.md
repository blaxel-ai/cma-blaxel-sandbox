# AGENTS.md

Orientation for an AI coding agent, or a human, working in this repo. Read this first for the fastest correct run.

## What this is

Run Claude Managed Agents (CMA) tool execution on Blaxel sandboxes. Anthropic hosts the agent loop and environment work queue; Blaxel provides the self-hosted execution layer.

Two Blaxel sandbox roles:

- `orchestrator/`: FastAPI webhook on a public preview URL. On `session.status_run_started`, it verifies the webhook, starts one worker, and returns.
- `worker/`: `ant beta:worker poll` runtime. It self-claims work from the Anthropic environment queue, runs tools in `/workspace`, posts results back, and exits after queue idle.

Public quickstart: `README.md`. Narrative guide source: `GUIDE.md`. Machine summary: `llms.txt`.

## Prerequisites

- Blaxel workspace, `bl` CLI, Docker, `BL_WORKSPACE`, and a service-account `BL_API_KEY`.
- Claude Managed Agents beta access and an `ANTHROPIC_API_KEY`.
- `python3`; install local deps with `python3 -m pip install -r requirements-dev.txt`.
- Copy `.env.example` to `.env`, fill it in, then load it with `set -a; source .env; set +a`.

## Setup, in order

1. `python3 scripts/preflight.py` checks local tooling and CMA access.
2. `python3 scripts/create_environment.py` prints `export ANTHROPIC_ENVIRONMENT_ID=env_...`.
3. Generate `ANTHROPIC_ENVIRONMENT_KEY` in the Anthropic Console environment page.
4. `(cd worker && bl push --workspace "$BL_WORKSPACE" --type sandbox)` publishes `sandbox/cma-worker:latest`.
5. `python3 scripts/create_agent.py` prints `export ANTHROPIC_AGENT_ID=agent_...`.
6. `python3 example/run_session.py --local-worker` validates the worker path without a webhook.
7. `(cd orchestrator && bl push --workspace "$BL_WORKSPACE" --type sandbox)` publishes `sandbox/cma-orchestrator:latest`.
8. `python3 setup.py` creates or updates the orchestrator and prints the webhook URL.
9. Register the Anthropic webhook for `session.status_run_started`, copy `whsec_...`, export `ANTHROPIC_WEBHOOK_SIGNING_KEY`, then rerun `python3 setup.py`.
10. `python3 example/run_session.py` runs the full webhook path.

## Environment variables

| Variable | Where it lives | What it is |
| -- | -- | -- |
| `ANTHROPIC_API_KEY` | local shell only | Control-plane key. Creates environments, agents, sessions, and reads events. Never put it on the worker. |
| `BL_API_KEY`, `BL_WORKSPACE` | local shell and orchestrator | Blaxel service-account auth so the orchestrator can spawn workers. |
| `ANTHROPIC_ENVIRONMENT_ID` | local shell, orchestrator, worker | The self-hosted environment id. |
| `ANTHROPIC_ENVIRONMENT_KEY` | orchestrator and worker | Scoped, revocable worker work-queue auth. Agent-run shell can read worker env vars. |
| `ANTHROPIC_AGENT_ID` | local shell | Agent to run for example sessions. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | orchestrator | Webhook signature verification secret from the Anthropic Console. |
| `BL_REGION`, `BLAXEL_WORKER_IMAGE`, `BLAXEL_WORKER_TTL`, `ANT_MAX_IDLE`, `ANT_RESTART_COOLDOWN`, `ANT_KEEPALIVE_TIMEOUT` | optional | Runtime tuning; see `.env.example`. |

## Commands

| Command | What it does | Side effects |
| -- | -- | -- |
| `python3 -m py_compile setup.py orchestrator/app.py example/*.py scripts/*.py` | syntax check | local, safe |
| `python3 -m pytest` | setup, script, and orchestrator tests | local, safe |
| `docker build -t cma-worker:smoke worker && docker run --rm --entrypoint /worker/smoke.sh cma-worker:smoke` | worker runtime smoke test | local Docker only |
| `python3 scripts/preflight.py` | checks local tooling and Anthropic access | read-only external API call |
| `python3 example/run_session.py --local-worker` | real session, direct worker spawn | creates Anthropic session + Blaxel sandbox |
| `python3 setup.py` | create/update orchestrator and preview URL | creates persistent Blaxel sandbox |
| `python3 example/run_session.py` | full webhook flow | creates real session; needs webhook/orchestrator |
| `python3 example/demo_preview_resume.py` | preview URL + standby/resume behavior demo | creates real resources |
| `python3 example/validate_long_session.py` | long keep-alive + filesystem-containment probe | creates real resources |
| `bl push --workspace "$BL_WORKSPACE" --type sandbox` | builds and publishes sandbox image | publishes to the workspace loaded from `.env` |

## Where to look

| Path | Responsibility |
| -- | -- |
| `scripts/` | local setup helpers; create scripts print exports and never mutate `.env` |
| `orchestrator/app.py` | webhook verification, worker spawn, duplicate suppression, poller launch |
| `worker/Dockerfile` | the agent runtime: `ant`, cloud-sandbox-style language runtimes, database clients, and utilities |
| `setup.py` | create/update orchestrator, restart webhook server with current env, print preview URL |
| `example/run_session.py` | primary E2E example; `--local-worker` skips webhook |
| `tests/` | local behavior tests |

## Invariants

- File tools use relative paths only. Use `hello.txt`, not `/workspace/hello.txt`. Bash commands can still use absolute paths inside the container.
- Every tool call must produce non-empty output. For silent shell commands, append `&& echo ok`.
- Launch the poller with `keep_alive: True` plus a timeout cap, or the sandbox can standby while the poller is waiting on outbound calls.
- `--max-idle` controls when the poller exits after queue idle.
- `BLAXEL_WORKER_TTL` is max age from sandbox creation. It is not idle deletion and should be longer than expected sessions.
- Worker sandbox names must be lowercase alphanumerics and hyphens; sanitize Anthropic session ids.
- Duplicate webhook suppression is in-process best effort. `create_if_not_exists` and Anthropic queue claiming are the durable safety layer.

## Safe vs. company-facing

- Local edits and local checks are allowed.
- Creating real Anthropic sessions, registering webhooks, pushing sandbox images, and creating Blaxel sandboxes need the user to understand the side effect.
- Do not push, open PRs, merge, change repo visibility, update Linear/GitHub/Slack, or publish docs without explicit human approval.
