#!/usr/bin/env node
import { parseArgs } from 'node:util';
import path from 'node:path';
import { dependencies, defaultAiBox, kitRoot, allowedHosts } from './mcp-runtime.mjs';

const { values } = parseArgs({ options: {
  project: { type: 'string', default: process.cwd() }, repo: { type: 'string' },
  'ai-box': { type: 'string', default: defaultAiBox },
} });
let client;
try {
  const deps = await dependencies(values['ai-box']);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StdioClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/stdio.js');
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [path.join(kitRoot, 'scripts/mcp-server.mjs'), 'git', '--ai-box', values['ai-box']],
    env: { KIT_GIT_PROJECT: path.resolve(values.project),
      KIT_MCP_ALLOWED_HOSTS: allowedHosts('git') }, stderr: 'pipe' });
  transport.stderr.on('data', () => {});
  client = new Client({ name: 'engineering-kit-git-read-check', version: '1' });
  await client.connect(transport, { timeout: 15000 });
  const catalog = await client.listTools();
  console.log(JSON.stringify({ protocol: 'ok', tools: catalog.tools.map((tool) => tool.name) }));
  const action = values.repo ? 'kit_git_remote_check' : 'kit_git_discover';
  const response = await client.callTool({ name: action, arguments: values.repo ? { repo: values.repo } : {} },
    undefined, { timeout: 145000 });
  const data = JSON.parse(response.content[0].text);
  console.log(JSON.stringify({ action, ok: !response.isError, result: data }));
  if (response.isError) process.exitCode = 1;
} catch {
  console.error('Git MCP не прошёл проверку; подробности скрыты. Подключения читает только MCP, push не выполнялся.');
  process.exitCode = 1;
} finally {
  if (client) await client.close();
}
