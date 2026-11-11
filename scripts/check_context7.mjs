#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { dependencies, defaultAiBox, PublicError, safeError } from './mcp-runtime.mjs';

const { values } = parseArgs({ options: {
  'ai-box': { type: 'string', default: defaultAiBox },
  library: { type: 'string' }, 'library-id': { type: 'string' }, query: { type: 'string' },
} });
let client;
try {
  if ((values.library && values['library-id'])
    || (Boolean(values.library || values['library-id']) !== Boolean(values.query))) {
    throw new PublicError('INVALID_ARGUMENTS', 'Передайте library либо library-id вместе с query; без аргументов проверяется только подключение');
  }
  const deps = await dependencies(values['ai-box']);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StreamableHTTPClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/streamableHttp.js');
  client = new Client({ name: 'engineering-kit-context7-check', version: '1' });
  // Проверка публичной документации без чтения токенов, env-файлов и конфигурации пользователя.
  const transport = new StreamableHTTPClientTransport(new URL('https://mcp.context7.com/mcp'));
  await client.connect(transport, { timeout: 20000 });
  const catalog = await client.listTools(undefined, { timeout: 20000 });
  const tools = catalog.tools.map((tool) => tool.name);
  if (!['resolve-library-id', 'query-docs'].every((name) => tools.includes(name))) {
    throw new PublicError('UNEXPECTED_TOOLS', 'Нет ожидаемых инструментов Context7');
  }
  console.log(JSON.stringify({ protocol: 'ok', tools }));
  const action = values['library-id'] ? 'query-docs' : values.library ? 'resolve-library-id' : null;
  if (action) {
    const response = await client.callTool({ name: action, arguments: {
      ...(values['library-id'] ? { libraryId: values['library-id'] } : { libraryName: values.library }),
      query: values.query,
    } }, undefined, { timeout: 30000 });
    const content = response.content.filter((item) => item.type === 'text').map((item) => item.text).join('\n');
    console.log(JSON.stringify({ action, ok: !response.isError, content: content.slice(0, 12000),
      truncated: content.length > 12000 }));
    if (response.isError) process.exitCode = 1;
  }
} catch (error) {
  const httpStatus = Number.isInteger(error.code) && error.code >= 100 && error.code <= 599 ? error.code : undefined;
  console.error(JSON.stringify({ ok: false, ...safeError(error), ...(httpStatus ? { httpStatus } : {}) }));
  process.exitCode = 1;
} finally {
  if (client) await client.close();
}
