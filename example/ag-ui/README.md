# AG-UI application example

This is the application layer above the Blaxel self-hosted environment. It reuses `@ag-ui/claude-managed-agents`: one CopilotKit thread maps to one managed session, and the adapter translates streamed text, thinking, tools, results, follow-ups, and interrupts. The existing webhook orchestrator starts the corresponding Blaxel worker.

Complete the repository setup, keep the webhook orchestrator running as the only queue claimant, then:

```bash
set -a; source ../../.env; set +a
npm ci
npm run typecheck
npm run dev
```

Open `http://localhost:5173`.

Every session has the cookbook's 100-cent budget by default; override it with `AG_UI_BUDGET_CENTS`. Cold Blaxel starts can take time, so the adapter uses a 10-minute turn cap configurable with `AG_UI_TURN_TIMEOUT_MS`.

Limits: the runtime is unauthenticated and refuses non-loopback bindings; thread-to-session mappings are in memory; adapter `0.0.1` reports `budget_reached` as a run error rather than offering an in-UI budget increase; and a worker's max-age TTL can remove `/workspace` during a very long-lived thread even though Anthropic retains the session history. Add application authentication before deploying the endpoint behind another server.

Dependency audit: the exact lock currently reports five low-severity `GHSA-866g-f22w-33x8` findings in nested `@ai-sdk/*` packages selected by CopilotKit. CI rejects moderate-or-higher findings; update CopilotKit when its dependency graph provides a compatible fix.
