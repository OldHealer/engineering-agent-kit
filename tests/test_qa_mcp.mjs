import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rename, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { defaultAiBox, dependencies, kitRoot } from '../scripts/mcp-runtime.mjs';

async function connect() {
  const root = await mkdtemp(path.join(tmpdir(), 'kit-qa-mcp-'));
  const project = path.join(root, 'project');
  await mkdir(project);
  const deps = await dependencies(defaultAiBox);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StdioClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/stdio.js');
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [path.join(kitRoot, 'scripts/mcp-server.mjs'), 'qa', '--ai-box', defaultAiBox,
      '--connections-file', path.join(root, 'DOES-NOT-EXIST')],
    env: { KIT_QA_PROJECT: project, KIT_QA_STORE: path.join(root, 'ledger') }, stderr: 'pipe' });
  transport.stderr.on('data', () => {});
  const client = new Client({ name: 'qa-fixture', version: '1' });
  await client.connect(transport);
  const call = async (action, input) => {
    const result = await client.callTool({ name: `kit_qa_${action}`, arguments: input }, undefined, { timeout: 145000 });
    return { error: result.isError === true, data: JSON.parse(result.content[0].text) };
  };
  return { root, client, call };
}

const fixture = (image) => ({ id: 'qa-fixture', image, env: { POSTGRES_HOST_AUTH_METHOD: 'trust' }, command: [],
  storage_target: '/var/lib/postgresql/data', ttl_seconds: 3600,
  phases: { test: [['pg_isready', '-U', 'postgres', '-h', '127.0.0.1']] } });

test('QA stdio catalog works without credentials and refuses operations without grant', async () => {
  const { root, client, call } = await connect();
  try {
    const tools = (await client.listTools()).tools;
    assert.equal(tools.length, 10);
    assert.ok(!tools.some((item) => /exec|shell|push|publish/.test(item.name)));
    const contract = fixture('sha256:' + 'a'.repeat(64));
    const rejected = await call('grant', { contract, confirmation_token: 'a'.repeat(64) });
    assert.equal(rejected.data.code, 'CONFIRMATION_REQUIRED');
    assert.equal((await call('start', { id: contract.id })).error, true);
  } finally {
    await client.close();
    await rm(root, { recursive: true, force: true });
  }
});

test('QA lifecycle crosses real MCP stdio after one confirmed grant', {
  skip: process.env.KIT_QA_INTEGRATION !== '1',
}, async () => {
  const { root, client, call } = await connect();
  let granted = false;
  let cleaned = false;
  const contract = fixture(process.env.KIT_QA_IMAGE);
  try {
    const preview = await call('preview', { contract });
    assert.equal(preview.error, false, JSON.stringify(preview.data));
    const grant = await call('grant', { contract, confirmation_token: preview.data.confirmation_token, confirm: true });
    assert.equal(grant.error, false, JSON.stringify(grant.data));
    granted = true;
    const start = await call('start', { id: contract.id });
    assert.equal(start.error, false, JSON.stringify(start.data));
    let ready = false;
    for (let i = 0; i < 50; i++) {
      const result = await call('check', { id: contract.id, phase: 'test', attempt: `ready-${i}` });
      assert.equal(result.error, false, JSON.stringify(result.data));
      if (result.data.passed) { ready = true; break; }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    assert.ok(ready);
    assert.equal((await call('stop', { id: contract.id })).error, false);
    await rename(path.join(root, 'project'), path.join(root, 'moved-project'));
    const missingRoot = await call('status', { id: contract.id });
    assert.equal(missingRoot.data.project_matches, false);
    const cleanup = await call('cleanup', { id: contract.id });
    assert.equal(cleanup.error, false, JSON.stringify(cleanup.data));
    cleaned = true;
    assert.equal(cleanup.data.phase, 'cleaned');
  } finally {
    if (granted && !cleaned) {
      const outcome = await call('cleanup', { id: contract.id });
      cleaned = !outcome.error;
    }
    await client.close();
    if (cleaned || !granted) await rm(root, { recursive: true, force: true });
    else process.stderr.write(`QA ledger retained for inspection: ${root}\n`);
  }
});
