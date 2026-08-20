import Anthropic from '@anthropic-ai/sdk';
import {InMemorySessionStore, ManagedAgentsAgent, type SessionStore} from '@ag-ui/claude-managed-agents';
import {CopilotSseRuntime} from '@copilotkit/runtime/v2';

const required = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set. Load the repository .env first.`);
  return value;
};

const positiveInteger = (name: string, fallback?: number): number => {
  const raw = process.env[name];
  if ((raw === undefined || raw === '') && fallback !== undefined) return fallback;
  if (!raw || !/^\d+$/.test(raw) || Number(raw) <= 0 || !Number.isSafeInteger(Number(raw))) {
    throw new Error(`${name} must be a positive integer.`);
  }
  return Number(raw);
};

const budgetCents = positiveInteger('AG_UI_BUDGET_CENTS', 100);
const turnTimeoutMs = positiveInteger('AG_UI_TURN_TIMEOUT_MS', 600_000);
const agentVersion = positiveInteger('ANTHROPIC_AGENT_VERSION');

const client = new Anthropic();
const createSession = client.beta.sessions.create.bind(client.beta.sessions);
client.beta.sessions.create = (params, options) => createSession({
  ...params,
  budget: {
    type: 'limit',
    max_list_cost: {amount: String(budgetCents), currency: 'USD'},
  },
  metadata: {...params.metadata, cookbook: 'blaxel-cma', surface: 'ag-ui'},
}, options);

const sessions = new InMemorySessionStore();
const store: SessionStore = {
  get: (key) => sessions.get(key),
  set: (key, record) => {
    if (sessions.get(key)?.sessionId !== record.sessionId) {
      const safe = record.sessionId.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 40);
      console.log(`[session] ${record.sessionId}\n  worker: cma-worker-${safe}`);
      console.log(`  cleanup: python3 cookbook.py cleanup --session ${record.sessionId}`);
    }
    sessions.set(key, record);
  },
  delete: (key) => sessions.delete(key),
};

export const runtime = new CopilotSseRuntime({
  agents: {
    'blaxel-cma': new ManagedAgentsAgent({
      managedAgentId: required('ANTHROPIC_AGENT_ID'),
      environmentId: required('ANTHROPIC_ENVIRONMENT_ID'),
      agentVersion,
      client,
      sessionStore: store,
      turnTimeoutMs,
      sessionTitle: (threadId) => `AG-UI ${threadId}`,
    }),
  },
});
