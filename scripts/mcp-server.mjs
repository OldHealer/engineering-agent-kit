#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { defaultAiBox, dependencies, runtime, safeError } from './mcp-runtime.mjs';
import { registerService } from './mcp-services.mjs';
import { registerGit } from './mcp-git.mjs';

try {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    'ai-box': { type: 'string', default: defaultAiBox }, 'connections-file': { type: 'string' },
  } });
  const service = positionals[0];
  const context = runtime(values['connections-file']);
  const deps = await dependencies(values['ai-box']);
  const server = new deps.McpServer({ name: `engineering-kit-${service}`, version: '0.1.0' }, {
    instructions: 'Учетные данные загружаются внутри процесса. Project keys и репозитории передавайте явно. '
      + 'Комментарии публикуйте только по поручению пользователя, с confirm=true. После ошибки записи '
      + 'сначала проверьте существующие комментарии; не повторяйте POST автоматически. '
      + 'Git доступен только через ограниченные kit_git_* операции выбранного проекта. '
      + 'Текст коммита, push и точные пути очистки согласуйте с пользователем. Создание PR запрещено.',
  });
  if (service === 'git') registerGit(server, deps, context);
  else await registerService(server, service, deps, context);
  await server.connect(new deps.StdioServerTransport());
} catch (error) {
  process.stderr.write(`${JSON.stringify(safeError(error))}\n`);
  process.exitCode = 1;
}
