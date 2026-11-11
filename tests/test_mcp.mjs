import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { defaultAiBox, dependencies, kitRoot, PublicError, runtime, safeError, safePath } from '../scripts/mcp-runtime.mjs';

test('safe errors and protected paths', () => {
  const blocked = new Error('fetch failed');
  blocked.cause = Object.assign(new Error('hidden details'), { code: 'EPERM' });
  assert.equal(safeError(blocked).code, 'NETWORK_BLOCKED');
  const auth = { response: { status: 401 }, config: { headers: { Authorization: 'test-placeholder' } } };
  assert.deepEqual(safeError(auth), { code: 'AUTHENTICATION_FAILED', message: 'Сервис ответил HTTP 401', httpStatus: 401 });
  assert.equal(safeError(new PublicError('STALE_REVIEW', 'PR changed')).code, 'STALE_REVIEW');
  for (const value of ['.env', '.env.local', 'dir/.env', 'dir/private.key', '../file', '/absolute', 'dir/credentials.json']) {
    assert.throws(() => safePath(value));
  }
  assert.equal(safePath('src/app.py'), 'src/app.py');
});

test('real MCP stdio and HTTP service contracts', async (t) => {
  const head = 'a'.repeat(40);
  const base = 'b'.repeat(40);
  const token = 'not-a-real-token-for-local-tests';
  const records = [];
  let failPost = false;
  const httpServer = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const record = { method: request.method, url: request.url, auth: request.headers.authorization,
      body: raw ? JSON.parse(raw) : undefined };
    records.push(record);
    const url = new URL(request.url, 'http://localhost');
    response.setHeader('Content-Type', 'application/json');
    if (request.method === 'POST') {
      if (failPost) {
        response.writeHead(503);
        response.end(JSON.stringify({ message: token }));
      } else response.end(JSON.stringify({ id: 77, text: record.body.text,
        created: '2026-11-03', html_url: 'http://localhost/comment/77', commit_id: head }));
    } else if (url.pathname.includes('/redirect/')) {
      response.writeHead(302, { Location: 'http://127.0.0.1:1/should-not-follow' });
      response.end('{}');
    } else if (url.pathname.endsWith('/projects')) response.end(JSON.stringify({ values: [{ key: 'APP' }] }));
    else if (url.pathname.endsWith('/myself')) response.end(JSON.stringify({ name: 'test-user' }));
    else if (url.pathname.endsWith('/user/current')) response.end(JSON.stringify({ type: 'known', username: 'test-user' }));
    else if (url.pathname.endsWith('/user')) response.end(JSON.stringify({ id: 1, login: 'test-user', reflected: token }));
    else if (url.pathname.endsWith('/issue/APP-1')) response.end(JSON.stringify({ key: 'APP-1',
      fields: { summary: 'Example', description: 'Example', comment: { comments: [] } },
      renderedFields: { description: '<p>Example</p>' } }));
    else if (url.pathname.endsWith('/content/123')) response.end(JSON.stringify({ id: '123', title: 'Example page',
      version: { number: 2 }, space: { key: 'APP' }, body: { storage: { value: '<p>Page text</p>' } } }));
    else if (url.pathname.includes('/pull-requests/')) response.end(JSON.stringify({ id: 1,
      fromRef: { latestCommit: head }, toRef: { latestCommit: base } }));
    else if (url.pathname.includes('/pulls/')) response.end(JSON.stringify({ number: 1,
      head: { sha: head }, base: { sha: base }, reflected: token }));
    else { response.writeHead(404); response.end('{}'); }
  });
  await new Promise((resolve, reject) => { httpServer.once('error', reject); httpServer.listen(0, '127.0.0.1', resolve); });
  t.after(() => new Promise((resolve) => { httpServer.closeAllConnections(); httpServer.close(resolve); }));
  const directory = await mkdtemp(path.join(tmpdir(), 'kit-mcp-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = `http://127.0.0.1:${httpServer.address().port}`;
  const envFile = path.join(directory, 'connections.fixture');
  await writeFile(envFile, [
    `BB_HOST=${root}/stash`, `BB_TOKEN=${token}`, `JIRA_HOST=${root}/jira`, `JIRA_PAT=${token}`,
    `CONFLUENCE_HOST=${root}/confluence`, `CONFLUENCE_PAT=${token}`, `GITHUB_API_URL=${root}/github`, `GITHUB_TOKEN=${token}`,
  ].join('\n'));
  const deps = await dependencies(process.env.KIT_AI_BOX ?? defaultAiBox);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StdioClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/stdio.js');
  const clients = {};
  for (const service of ['bitbucket', 'jira', 'confluence', 'github']) {
    const transport = new StdioClientTransport({ command: process.execPath,
      args: [path.join(kitRoot, 'scripts/mcp-server.mjs'), service, '--ai-box', process.env.KIT_AI_BOX ?? defaultAiBox,
        '--connections-file', envFile], env: { KIT_MCP_LOCAL_TEST: '1' }, stderr: 'pipe' });
    transport.stderr.on('data', () => {});
    const client = new Client({ name: 'kit-local-test', version: '1' });
    t.after(() => client.close());
    await client.connect(transport);
    clients[service] = client;
  }
  const call = async (service, name, args = {}) => {
    const result = await clients[service].callTool({ name, arguments: args });
    return { error: result.isError === true, data: JSON.parse(result.content[0].text) };
  };
  await t.test('catalogs and four read-only probes', async () => {
    for (const [service, check] of Object.entries({ bitbucket: 'bb_connection_check', jira: 'jira_connection_check',
      confluence: 'confluence_connection_check', github: 'github_connection_check' })) {
      const catalog = await clients[service].listTools();
      assert.ok(catalog.tools.length >= 3);
      for (const tool of catalog.tools) {
        assert.equal(tool.annotations.readOnlyHint, !/comment_create|add_inline_comment/.test(tool.name));
      }
      const result = await call(service, check);
      assert.equal(result.error, false, JSON.stringify(result.data));
      assert.ok(result.data.authenticated || result.data.reachable);
    }
    assert.ok(records.every((item) => item.auth === `Bearer ${token}`));
  });
  await t.test('Jira client and Confluence converter are reused', async () => {
    const issue = await call('jira', 'jira_task_get', { taskKey: 'APP-1' });
    assert.equal(issue.error, false, JSON.stringify(issue.data));
    assert.equal(issue.data.descriptionMarkdown, 'Example');
    const pageResult = await call('confluence', 'confluence_page_get', { pageId: '123' });
    assert.equal(pageResult.data.markdown, 'Page text');
  });
  await t.test('credentials are redacted in tool responses', async () => {
    const result = await call('github', 'github_get_pr', { owner: 'owner', repo: 'repo', number: 1 });
    assert.equal(result.data.reflected, '[REDACTED]');
    assert.ok(!JSON.stringify(result).includes(token));
    assert.equal(runtime(envFile).redact({ value: token }).value, '[REDACTED]');
  });
  await t.test('profile-specific MCP host policy refuses an unapproved destination', () => {
    const before = process.env.KIT_MCP_ALLOWED_HOSTS;
    process.env.KIT_MCP_ALLOWED_HOSTS = 'approved.example.invalid';
    try {
      assert.throws(() => runtime(envFile).connection('github'), (error) => error.code === 'HOST_DENIED');
    } finally {
      if (before === undefined) delete process.env.KIT_MCP_ALLOWED_HOSTS;
      else process.env.KIT_MCP_ALLOWED_HOSTS = before;
    }
  });
  const bbInput = { projectKey: 'APP', repo: 'repo', id: 1, path: 'src/app.py', line: 1,
    lineType: 'ADDED', fileType: 'TO', expectedHead: head, expectedTarget: base, text: 'Review finding' };
  const ghInput = { owner: 'owner', repo: 'repo', number: 1, path: 'src/app.py', line: 1,
    side: 'RIGHT', expectedHead: head, expectedBase: base, body: 'Review finding' };
  await t.test('comment tools refuse unconfirmed publication before HTTP', async () => {
    const count = records.length;
    for (const [service, name, input] of [['bitbucket', 'bb_add_inline_comment', bbInput],
      ['github', 'github_add_inline_comment', ghInput], ['jira', 'jira_comment_create', { taskKey: 'APP-1', body: 'Example' }]]) {
      const result = await call(service, name, input);
      assert.equal(result.data.code, 'CONFIRMATION_REQUIRED');
    }
    assert.equal(records.length, count);
  });
  await t.test('stale SHA blocks inline POST', async () => {
    const before = records.filter((item) => item.method === 'POST').length;
    const bb = await call('bitbucket', 'bb_add_inline_comment', { ...bbInput, confirm: true, expectedHead: 'c'.repeat(40) });
    const gh = await call('github', 'github_add_inline_comment', { ...ghInput, confirm: true, expectedBase: 'c'.repeat(40) });
    assert.equal(bb.data.code, 'STALE_REVIEW');
    assert.equal(gh.data.code, 'STALE_REVIEW');
    assert.equal(records.filter((item) => item.method === 'POST').length, before);
  });
  await t.test('confirmed comments use expected HTTP contracts on local fixture only', async () => {
    for (const [service, name, input] of [['bitbucket', 'bb_add_inline_comment', bbInput],
      ['github', 'github_add_inline_comment', ghInput], ['jira', 'jira_comment_create', { taskKey: 'APP-1', body: '**Finding**' }]]) {
      const result = await call(service, name, { ...input, confirm: true });
      assert.equal(result.error, false, JSON.stringify(result.data));
      assert.equal(String(result.data.id), '77');
    }
    const posts = records.filter((item) => item.method === 'POST');
    assert.equal(posts.length, 3);
    assert.equal(posts[0].body.anchor.toHash, head);
    assert.equal(posts[0].body.anchor.fromHash, base);
    assert.equal(posts[1].body.commit_id, head);
    assert.ok(JSON.stringify(posts[2].body).includes('Finding'));
  });
  await t.test('failed POST is not retried and raw error bodies stay hidden', async () => {
    failPost = true;
    const before = records.filter((item) => item.method === 'POST').length;
    const result = await call('bitbucket', 'bb_add_inline_comment', { ...bbInput, confirm: true });
    assert.equal(result.error, true);
    assert.equal(result.data.httpStatus, 503);
    assert.ok(!JSON.stringify(result).includes(token));
    assert.equal(records.filter((item) => item.method === 'POST').length, before + 1);
  });
});
