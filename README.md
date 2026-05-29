# Claude Managed Agents on Blaxel: self-hosted sandbox cookbook

Run Claude Managed Agents (CMA) tool execution on **Blaxel sandboxes**. Both roles in the integration are Blaxel sandboxes, with no Blaxel Agent and no platform changes.

- **Orchestrator** (`orchestrator/`): a sandbox running a FastAPI webhook server on a public **preview URL** (the Anthropic webhook target). On `session.status_run_started` it spawns a worker and returns. It never polls, claims, or babysits.
- **Worker** (`worker/`): a sandbox running `ant beta:worker poll`. It self-claims the queued session, runs the tool calls in `/workspace`, posts results back to Anthropic, and exits on idle; a TTL auto-cleans it.

The narrative walkthrough is in **[GUIDE.md](./GUIDE.md)**.

> Validated end-to-end against a live CMA session: a real agent's `write` and `bash` tool calls executed inside a Blaxel sandbox and posted results back, clean.

## Layout

```
orchestrator/   app.py (webhook -> spawn worker), Dockerfile, requirements.txt, blaxel.toml
worker/         Dockerfile (sandbox-api + ant + node/python3), entrypoint.sh, blaxel.toml
example/        run_session.py: create a session + watch it run
setup.py        bring up the orchestrator sandbox and print its public webhook URL
GUIDE.md        the integration guide (prose)
```

## Prerequisites

- A Blaxel workspace, the **`bl` CLI** (for `bl push`), and a **service-account `BL_API_KEY`**. The orchestrator uses it to spawn workers, because a sandbox does not inherit a workspace identity the way a Blaxel Agent does. Create one under Service Accounts.
- Access to Claude Managed Agents (`managed-agents-2026-04-01` beta) and an `ANTHROPIC_API_KEY`.
- `python3`, and `pip install "blaxel>=0.2.54"` locally (used by `setup.py` and the `--local-worker` test). The `ant` CLI is baked into the worker image, so there is nothing to install.

## Quickstart

Set your credentials once:

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # control-plane calls below
export BL_WORKSPACE=<your-workspace>
export BL_API_KEY=<service-account-key>
# export BL_REGION=us-pdx-1                    # optional; silences the SDK region warning
```

**1. Create the self-hosted environment** (captures the id into your shell):

```bash
export ANTHROPIC_ENVIRONMENT_ID=$(curl -sS https://api.anthropic.com/v1/environments \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d '{"name":"blaxel-selfhosted","config":{"type":"self_hosted"}}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "environment: $ANTHROPIC_ENVIRONMENT_ID"
```

Then open that environment in the Anthropic Console, click **Generate environment key**, and export the `sk-ant-oat01-…` key the worker authenticates with:

```bash
export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-...
```

**2. Build + push both sandbox images** (run from inside each dir):

```bash
( cd worker && bl push --type sandbox )        # -> sandbox/cma-worker:latest
( cd orchestrator && bl push --type sandbox )  # -> sandbox/cma-orchestrator:latest
```

**3. Create an agent** (captures the id into your shell):

```bash
export ANTHROPIC_AGENT_ID=$(curl -sS https://api.anthropic.com/v1/agents \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d '{"name":"Coding Assistant","model":"claude-opus-4-8","system":"You are a coding agent. Your working directory is /workspace; use absolute /workspace paths.","tools":[{"type":"agent_toolset_20260401"}]}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "agent: $ANTHROPIC_AGENT_ID"
```

**4. Validate the worker without a webhook** (recommended before wiring the orchestrator):

```bash
python example/run_session.py --local-worker
```

This creates a session, sends a message, spawns a worker sandbox itself, and watches the agent run the tools and finish. It needs the vars from steps 1 to 3 plus `ANTHROPIC_ENVIRONMENT_KEY` and `BL_API_KEY`/`BL_WORKSPACE`. It does not need the webhook or any console step.

**5. Bring up the orchestrator**, which prints the public preview webhook URL:

```bash
python setup.py
```

**6. Register the webhook.** In the Anthropic Console (**Manage > Webhooks**), add the printed `https://<id>.preview.bl.run/webhook` for `session.status_run_started`, copy the `whsec_` secret, then re-run setup so the orchestrator can verify deliveries:

```bash
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_...
python setup.py
```

**7. Run a session.** The webhook now auto-triggers the worker:

```bash
python example/run_session.py
```

## Gotchas (validated)

- **The worker image is the agent's runtime.** Whatever the agent executes (python, node, compilers, CLIs) must be installed in the worker image. The default ships `node` (from the base) and `python3`; extend the worker Dockerfile with the languages and tools your agents need.
- **`bash` needs `/bin/bash`:** the Debian base includes it (Alpine would not); skill download also needs `unzip` and `tar`.
- **Working directory:** run the worker with `--unrestricted-paths` and have the agent use absolute `/workspace` paths, so the `write` tool's base and `bash`'s cwd agree.
- **Sandbox names:** must be lowercase alphanumerics and hyphens, so session ids (`sesn_01Ab…`) are sanitized before use as a worker name.
- **Orchestrator credential:** pass a service-account `BL_API_KEY`; sandboxes do not get an auto-injected Blaxel identity.
- **`--max-idle`** (default `60s`, override with `ANT_MAX_IDLE`): keep it generous enough to span the agent's reasoning between tool calls.

## Links

- [Anthropic: self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Blaxel docs](https://docs.blaxel.ai)
