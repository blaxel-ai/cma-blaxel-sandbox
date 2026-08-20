import {createCopilotNodeListener} from '@copilotkit/runtime/v2/node';
import react from '@vitejs/plugin-react';
import {defineConfig, type PluginOption} from 'vite';

const isLoopback = (host: unknown): boolean =>
  host === undefined || host === false || host === 'localhost' || host === '127.0.0.1' || host === '::1';

export default defineConfig(async ({command}) => {
  const plugins: PluginOption[] = [react()];
  if (command === 'serve') {
    const {runtime} = await import('./runtime');
    const copilot = createCopilotNodeListener({runtime, basePath: '/api/copilotkit'});
    plugins.push({
      name: 'copilot-runtime',
      configureServer(server) {
        if (!isLoopback(server.config.server.host)) {
          throw new Error('The unauthenticated AG-UI example may only bind to localhost.');
        }
        server.middlewares.use((req, res, next) =>
          req.url?.startsWith('/api/copilotkit') ? void copilot(req, res) : next());
      },
    });
  }
  return {plugins};
});
