# Claude Managed Agents on Blaxel Sandboxes

This cookbook proves the self-hosted Claude Managed Agents path on Blaxel. Anthropic runs the agent loop, session state, event history, and self-hosted environment queue; Blaxel runs the execution layer in sandboxes, with one orchestrator sandbox for webhooks and one worker sandbox per session.

The first proof is intentionally small: create one real CMA session, run its claimed work inside a Blaxel worker sandbox, then inspect the matching Blaxel process record.

```bash
python3 example/run_session.py --local-worker
```

```text
tool: write {"content": "hello from blaxel", "file_path": "hello.txt"}
tool: bash {"command": "cat /workspace/hello.txt"}
final agent message: ... hello from blaxel ...
EXAMPLE: PASS

Blaxel process proof:
  sandbox: cma-worker-sesn-...
  process: ant-run-...
  inspect: bl get sandbox cma-worker-sesn-... process --workspace <workspace> -o json
```

`EXAMPLE: PASS` proves the transcript completed. The matching `cma-worker-<session>` sandbox and `ant-run-*` process prove the work ran in the expected Blaxel sandbox.

## How It Works

- Anthropic CMA owns Claude, session state, event history, and the self-hosted environment queue.
- A Blaxel orchestrator sandbox receives `session.status_run_started`, readies the session sandbox, claims `work_...`, and starts `ant beta:worker run`.
- A Blaxel worker sandbox runs the built-in CMA tools in that session's `/workspace`, heartbeats the work lease, posts tool results, and stops the work item.
- Prove the worker first with `example/run_session.py --local-worker`; add the webhook only after that exact-work path works.
- Read [GUIDE.md](./GUIDE.md) for deeper architecture, security boundaries, feature recipes, troubleshooting, and teardown.

## Before You Start

You need:

- Claude Managed Agents beta access and an `ANTHROPIC_API_KEY`.
- A Blaxel workspace name for `BL_WORKSPACE`.
- The `bl` CLI logged in with `bl login`.
- A Blaxel service-account key for that workspace as `BL_API_KEY`.
- Docker running locally.
- `python3`.

`BL_API_KEY` lets SDK code and the orchestrator spawn workers. It does not replace CLI login for `bl push`.

Install local tooling and log in:

```bash
brew tap blaxel-ai/blaxel
brew install blaxel
bl login
```

For Linux or WSL, use the [Blaxel CLI install docs](https://docs.blaxel.ai/cli-reference/introduction).

Create the local Python environment and `.env`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Fill `.env` with the starting values:

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

Prefer a guided setup? `python3 bootstrap.py` runs every deterministic step and stops only at the two Anthropic Console gates. Use `python3 bootstrap.py --plan` to see the next action without mutating anything. Bootstrap reads `.env` directly and uses the default image names; use the manual commands below when you need custom names in a shared workspace.

### 1. Check access

```bash
python3 scripts/preflight.py
```

Expected checkpoint:

```text
preflight passed
```

### 2. Create the self-hosted CMA environment

```bash
python3 scripts/create_environment.py
```

Add the printed `ANTHROPIC_ENVIRONMENT_ID=env_...` to `.env`. Then open the Anthropic Console, select the workspace/project for your API key, open Managed Agents > Environments, choose that environment, click **Generate environment key**, and add:

```text
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

Reload env:

```bash
set -a; source .env; set +a
```

### 3. Publish the worker image and create the agent

```bash
( cd worker && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 scripts/create_agent.py
```

Add the printed `ANTHROPIC_AGENT_ID=agent_...` to `.env`, then reload env again:

```bash
set -a; source .env; set +a
```

The default model is `claude-opus-4-8`. If that model is unavailable in your Anthropic org, set `ANTHROPIC_AGENT_MODEL` before rerunning `scripts/create_agent.py`.

### 4. Run the worker proof

```bash
python3 example/run_session.py --local-worker
```

Pass criteria:

- The transcript ends with `EXAMPLE: PASS`.
- The output prints a `cma-worker-sesn-...` sandbox and an `ant-run-...` process.
- `bl get sandbox <worker> process --workspace "$BL_WORKSPACE" -o json` shows the matching `ant beta:worker run --workdir /workspace` process.
- `bl logs sandbox <worker> <ant-run-process> --workspace "$BL_WORKSPACE" --period 1h` shows the same session running the `write` and `bash` tools.

Use one active claimant per self-hosted environment while proving the path. A local worker, webhook dispatcher, environment-polling worker, or another cookbook worker can all claim the same queued work.

## Add The Webhook

After the worker proof passes, publish the orchestrator image and start the webhook server:

```bash
( cd orchestrator && bl push --workspace "$BL_WORKSPACE" --type sandbox )
python3 setup.py
```

`setup.py` prints a Blaxel preview URL:

```text
https://<id>.preview.bl.run/webhook
```

In the Anthropic Console, create a Managed Agents webhook for the same workspace/project, subscribe only to `session.status_run_started`, and set the destination to that exact URL. Copy the one-time `whsec_...` signing secret into `.env`:

```text
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
```

Reload env and rerun setup so the orchestrator verifies deliveries:

```bash
set -a; source .env; set +a
python3 setup.py
```

Run the webhook path:

```bash
python3 example/run_session.py
```

The transcript should again end with `EXAMPLE: PASS`. For attribution, inspect the matching `cma-worker-<session>` sandbox and `ant-run-*` process the same way as the local-worker proof.

## What Blaxel Adds

- **Process proof:** Blaxel process records and logs show which sandbox ran the exact CMA work item.
- **Preview URLs:** `python3 example/demo_preview_resume.py` lets the agent build an app in `/workspace`, serve it through a Blaxel preview URL, and prove standby/resume. Add `--private-preview` for a token-protected preview.
- **Volumes:** set `BLAXEL_WORKER_VOLUME_ENABLED=true` and `BL_REGION` to mount a per-session Blaxel Volume at `/workspace` when the workspace has Volume quota.
- **Proxy secret injection:** set `BLAXEL_WORKER_PROXY_DESTINATIONS` and `BLAXEL_WORKER_PROXY_SECRET_VALUE` to use public-preview, region-dependent Blaxel Proxy routing for outbound worker requests. This is not a replacement for `ANTHROPIC_ENVIRONMENT_KEY`.

See [GUIDE.md](./GUIDE.md) for setup details and caveats for each optional path.

## Debug Fast

| Symptom | Check |
| --- | --- |
| `bl push` is not authenticated | Run `bl login` and confirm `bl workspaces --current`. |
| Preflight fails on Anthropic | `ANTHROPIC_API_KEY` is missing, wrong-org, or lacks CMA beta access. |
| Agent creation fails | Set `ANTHROPIC_AGENT_MODEL` to a model enabled for your Anthropic org. |
| Example proof reports `workers_polling` | Stop the other claimant or use a fresh Anthropic environment. |
| Transcript passes but no matching `ant-run-*` exists | Another worker handled the item; do not use the transcript as attribution proof. |
| Volume or Proxy setup fails | Check Volume quota, `BL_REGION`, and `proxyAvailable`; keep optional features disabled for the base quickstart. |

## Docs And Files

- [GUIDE.md](./GUIDE.md): full architecture, feature recipes, troubleshooting, tests, and teardown.
- [AGENTS.md](./AGENTS.md) and [llms.txt](./llms.txt): agent-facing setup paths.
- [.env.example](./.env.example): complete environment variable template.
- `orchestrator/`, `worker/`, and `example/`: webhook dispatcher, runtime image, and proof/demo scripts.

## Tests

```bash
.venv/bin/python -B -m py_compile bootstrap.py setup.py orchestrator/app.py orchestrator/blaxel_features.py example/*.py scripts/*.py
.venv/bin/python -m pytest -q
```

The GitHub workflow also runs Docker image smoke checks for the worker and orchestrator.

## Cleanup

Delete the orchestrator sandbox with `bl delete sandbox "${ORCHESTRATOR_NAME:-cma-orchestrator-app}" --workspace "$BL_WORKSPACE"`. Also remove inactive `cma-worker-*` test sandboxes, any per-session `cma-workspace-*` Volumes you created, and the Anthropic webhook pointing at the deleted preview URL. See [GUIDE.md](./GUIDE.md#teardown) for the full checklist.
