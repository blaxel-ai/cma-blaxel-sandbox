import {CopilotChat, CopilotKitProvider, useRenderTool} from '@copilotkit/react-core/v2';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import '@copilotkit/react-core/v2/styles.css';

const ToolActivity = () => {
  useRenderTool({
    name: '*',
    render: ({name, status, parameters, result}) => (
      <details className="tool">
        <summary>{status === 'complete' ? 'ran' : 'running'} {name}</summary>
        <pre>{JSON.stringify(parameters, null, 2)}</pre>
        {typeof result === 'string' ? <pre>{result}</pre> : null}
      </details>
    ),
  }, []);
  return null;
};

const App = () => (
  <CopilotKitProvider runtimeUrl="/api/copilotkit">
    <ToolActivity />
    <main>
      <p className="eyebrow">Claude Managed Agents + AG-UI</p>
      <h1>Blaxel CMA</h1>
      <p>One chat thread is one managed session. Tools run in that session's Blaxel worker sandbox.</p>
      <section><CopilotChat agentId="blaxel-cma" labels={{chatInputPlaceholder: 'Ask the agent to write and run something…'}} /></section>
    </main>
  </CopilotKitProvider>
);

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
