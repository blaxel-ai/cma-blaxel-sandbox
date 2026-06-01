# AGENTS.md

Orientation for an AI coding agent (or a human) working in this repo. Read this first; it is the fastest path to a correct first run.

## What this is

Run Claude Managed Agents (CMA) tool execution on Blaxel sandboxes. Anthropic runs the agent loop (the "brain"); Blaxel runs tool execution (the "hands"). Two roles, both Blaxel sandboxes:

- `orchestrator/` — a sandbox running a FastAPI webhook on a public **preview URL**. On `session.status_run_started` it spawns one worker and returns. It never polls or babysits.
- `worker/` — a sandbox running `ant beta:worker poll`. It self-claims the session from the environment **work queue**, runs tools in `/workspace`, posts results back, and exits on idle.

Full prose guide: `GUIDE.md`. Quickstart: `README.md`. Machine summary: `llms.txt`.

## Prerequisites

- A Blaxel workspace, the `bl` CLI, and a service-account `BL_API_KEY` (Service Accounts) — the orchestrator needs it to spawn workers.
- Claude Managed Agents beta access (`managed-agents-2026-04-01`) and an `ANTHROPIC_API_KEY`.
- Local: `python3` and `pip install "blaxel>=0.2.54"`. For the test suite: `pip install -r requirements-dev.txt`.
- Copy `.env.example` → `.env`, fill it in, then `set -a; source .env; set +a`.

## Setup, in order (each step produces a value you capture)

1. **Create the self-hosted environment** (`curl .../v1/environments`, `config.type=self_hosted`) → `ANTHROPIC_ENVIRONMENT_ID`. Then in the Console, **Generate environment key** → `ANTHROPIC_ENVIRONMENT_KEY`. *(Console step — not scriptable.)*
2. **Build + push the worker image**: `(cd worker && bl push --type sandbox)` → `sandbox/cma-worker:latest`.
3. **Create an agent** (`curl .../v1/agents`, tools `agent_toolset_20260401`) → `ANTHROPIC_AGENT_ID`.
4. **Validate the worker without a webhook**: `python example/run_session.py --local-worker`.
5. **Bring up the orchestrator**: `(cd orchestrator && bl push --type sandbox)` then `python setup.py` → prints the public webhook URL.
6. **Register the webhook** in the Console (Manage > Webhooks) for `session.status_run_started`, copy the `whsec_` secret → `ANTHROPIC_WEBHOOK_SIGNING_KEY`, re-run `python setup.py`. *(Console step — not scriptable.)*
7. **Run a full session**: `python example/run_session.py`.

## Environment variables

| Variable | When | What it is |
| -- | -- | -- |
| `ANTHROPIC_API_KEY` | always | Control-plane key (`sk-ant-api03-…`). Create env/agent/session. **Never on the worker.** |
| `BL_API_KEY`, `BL_WORKSPACE` | always | Blaxel service account + workspace; lets the orchestrator spawn workers. |
| `ANTHROPIC_ENVIRONMENT_ID` | after step 1 | `env_…` — the self-hosted environment. |
| `ANTHROPIC_ENVIRONMENT_KEY` | after step 1 | `sk-ant-oat01-…` — worker work-queue auth. Scoped + revocable. The only Anthropic secret on the worker. |
| `ANTHROPIC_AGENT_ID` | after step 3 | `agent_…` — the agent to run. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | after step 6 | `whsec_…` — webhook signature verification. |
| `BL_REGION`, `BLAXEL_WORKER_IMAGE`, `BLAXEL_WORKER_TTL`, `ANT_MAX_IDLE`, `ANT_RESTART_COOLDOWN`, `ANT_KEEPALIVE_TIMEOUT` | optional | Tuning; see `.env.example` for defaults. |

## Commands

| Command | What it does | Side effects |
| -- | -- | -- |
| `pytest` | orchestrator unit tests | **local, safe** |
| `docker build -t cma-worker:smoke worker && docker run --rm --entrypoint /worker/smoke.sh cma-worker:smoke` | worker runtime smoke test | **local, safe** |
| `python example/run_session.py --local-worker` | create session, spawn worker directly, watch it run | creates a real Anthropic session + Blaxel sandbox (cost) |
| `python example/run_session.py` | full webhook flow | real session; needs the orchestrator live |
| `python example/demo_preview_resume.py` | preview + standby/resume demo (same pid survives standby) | real resources |
| `python example/validate_long_session.py` | long keep-alive run + filesystem-containment probe | real resources |
| `python setup.py` | create the orchestrator sandbox, start uvicorn, expose a public preview URL | creates a persistent sandbox + public URL |
| `bl push --type sandbox` (in `worker/` or `orchestrator/`) | build + publish the sandbox image | publishes to your workspace |

## Where to look

| Path | Responsibility |
| -- | -- |
| `orchestrator/app.py` | webhook signature verify, per-session spawn + duplicate suppression, poller launch |
| `orchestrator/blaxel.toml`, `orchestrator/Dockerfile` | orchestrator sandbox spec + image |
| `worker/Dockerfile` | the agent's runtime: `ant` 1.10.0 + node 22 + python3 + bash + git/curl/tar/unzip |
| `worker/entrypoint.sh` | starts `sandbox-api`, waits on `:8080` |
| `worker/smoke.sh` | proves the worker image has the runtime the agent needs |
| `setup.py` | create orchestrator sandbox, start the webhook server, expose the preview URL |
| `example/run_session.py` | primary end-to-end example (`--local-worker` skips the webhook) |
| `example/demo_preview_resume.py` | standby/resume demo (Blaxel's differentiator) |
| `example/validate_long_session.py` | long-session keep-alive + containment probe |
| `tests/` | pytest suite for the orchestrator (`_worker_name`, cooldown, `/webhook` branches) |

## Invariants to respect (breaking these stalls sessions)

- **File tools use relative paths only.** Passing an absolute path like `/workspace/hello.txt` to a file tool is REJECTED with "absolute path not permitted". Use bare relative paths (`hello.txt`, not `/workspace/hello.txt`). Shell (bash) commands are unrestricted and use `/workspace/...` paths.
- **Every tool call must produce non-empty output** — an empty tool result is rejected by the API (400). For a silent command, append `&& echo ok`.
- **Sandbox names** are lowercase alphanumerics + hyphens only; sanitize session ids before naming a worker (see `_worker_name` in `orchestrator/app.py`).
- **Launch the poller with `keep_alive: True` + a `timeout` cap**, or the sandbox standbys ~15s after spawn (the poller only makes outbound calls) and the poll loop freezes mid-session.
- **`ttl` is max-age from creation** (units `m/h/d/w`, not idle, not seconds). Keep it well above a session's length.
- **`ANTHROPIC_ENVIRONMENT_KEY`** may live on the worker (scoped, revocable). The org **`ANTHROPIC_API_KEY` must never** be on the worker.

## Safe vs. company-facing

- **Local + safe:** `pytest`, `docker run --rm --entrypoint /worker/smoke.sh cma-worker:smoke`, `python -m py_compile`, reading code.
- **Creates real cloud resources / costs money:** the `example/*.py` scripts, `setup.py`, `bl push`.
- **Do not publish** (push to GitHub, change repo visibility, register a public webhook, open a PR) without explicit human approval. This cookbook stays private until the external package is approved.
