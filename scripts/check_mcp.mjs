#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { dependencies, defaultAiBox, kitRoot, allowedHosts } from './mcp-runtime.mjs';
import path from 'node:path';

// Проверка вызывает только фиксированные инструменты чтения. Ответы и stderr не печатаются.
const { values } = parseArgs({ options: {
  'ai-box': { type: 'string', default: defaultAiBox }, 'connections-file': { type: 'string' },
  'restricted': { type: 'boolean', default: false },
} });
try {
  const deps = await dependencies(values['ai-box']);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StdioClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/stdio.js');
  const checks = {
    bitbucket: 'bb_connection_check', github: 'github_connection_check',
    jira: 'jira_connection_check', confluence: 'confluence_connection_check',
  };
  for (const [service, tool] of Object.entries(checks)) {
    const args = [path.join(kitRoot, 'scripts/mcp-server.mjs'), service, '--ai-box', values['ai-box']];
    if (values['connections-file']) args.push('--connections-file', values['connections-file']);
    const env = values.restricted ? { KIT_MCP_ALLOWED_HOSTS: allowedHosts(service) } : {};
    const transport = new StdioClientTransport({ command: process.execPath, args, env, stderr: 'pipe' });
    transport.stderr.on('data', () => {});
    const client = new Client({ name: 'engineering-kit-read-check', version: '0.1.0' });
    const report = { service, protocol: 'not_connected', readCheck: 'not_run' };
    try {
      await client.connect(transport, { timeout: 15000 });
      const catalog = await client.listTools();
      report.protocol = 'ok';
      report.tools = catalog.tools.map((item) => item.name);
      const response = await client.callTool({ name: tool, arguments: {} }, undefined, { timeout: 30000 });
      const result = JSON.parse(response.content.find((item) => item.type === 'text').text);
      if (response.isError) {
        const knownCodes = new Set(['MISSING_CONFIGURATION', 'AUTHENTICATION_FAILED', 'ACCESS_DENIED',
          'NETWORK_BLOCKED', 'NETWORK_ERROR', 'DNS_ERROR', 'TIMEOUT', 'HTTP_ERROR', 'UNEXPECTED_RESPONSE', 'REDIRECT_REFUSED', 'HOST_DENIED']);
        report.readCheck = knownCodes.has(result.code) ? result.code : 'INTEGRATION_ERROR';
        if (Number.isInteger(result.httpStatus)) report.httpStatus = result.httpStatus;
      } else {
        report.readCheck = result.reachable || result.authenticated ? 'ok' : 'not_authenticated';
      }
    } catch {
      report.readCheck = report.protocol === 'ok' ? 'PROTOCOL_OR_TIMEOUT_ERROR' : 'STARTUP_ERROR';
    } finally {
      await client.close();
    }
    console.log(JSON.stringify(report));
    if (report.readCheck !== 'ok') process.exitCode = 1;
  }
} catch {
  console.error('Проверка MCP не запущена: недоступны зависимости. Секретные данные не выводились.');
  process.exitCode = 1;
}
