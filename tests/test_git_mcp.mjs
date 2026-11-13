import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import test from 'node:test';
import { defaultAiBox, dependencies, kitRoot } from '../scripts/mcp-runtime.mjs';

const execute = promisify(execFile);

test('Git MCP stdio binds project, confirms mutations, commits and cleans real worktree', async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), 'kit-git-mcp-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const project = path.join(root, 'project');
  const source = path.join(project, 'backend');
  await mkdir(source, { recursive: true });
  const git = async (...args) => (await execute('git', ['-C', source, ...args], {
    env: { PATH: '/usr/bin:/bin', HOME: root, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' },
  })).stdout.trim();
  await git('init', '-q', '-b', 'main');
  await git('config', 'user.name', 'Fixture');
  await git('config', 'user.email', 'fixture@example.invalid');
  await writeFile(path.join(source, 'app.py'), 'value = 1\n');
  await git('add', 'app.py');
  await git('commit', '-q', '-m', 'base');
  await git('remote', 'add', 'origin', 'ssh://git@forge.example.test:7999/APP/backend.git');
  const connections = path.join(root, 'connections.fixture');
  await writeFile(connections, 'BB_HOST=https://forge.example.test/stash\nBB_TOKEN=synthetic-token-only\n');
  const deps = await dependencies(process.env.KIT_AI_BOX ?? defaultAiBox);
  const { Client } = await deps.module('@modelcontextprotocol/sdk/client/index.js');
  const { StdioClientTransport } = await deps.module('@modelcontextprotocol/sdk/client/stdio.js');
  const transport = new StdioClientTransport({ command: process.execPath, args: [
    path.join(kitRoot, 'scripts/mcp-server.mjs'), 'git', '--connections-file', connections,
    '--ai-box', process.env.KIT_AI_BOX ?? defaultAiBox,
  ], env: { KIT_GIT_PROJECT: project, KIT_MCP_ALLOWED_HOSTS: 'forge.example.test', KIT_PYTHON: '/usr/bin/python3' }, stderr: 'pipe' });
  transport.stderr.on('data', (chunk) => t.diagnostic(String(chunk)));
  const client = new Client({ name: 'kit-git-test', version: '1' });
  t.after(() => client.close());
  await client.connect(transport);
  const call = async (action, input = {}) => {
    const result = await client.callTool({ name: `kit_git_${action}`, arguments: input });
    return { error: result.isError === true, data: JSON.parse(result.content[0].text) };
  };
  const catalog = await client.listTools();
  assert.equal(catalog.tools.length, 17);
  assert.ok(!catalog.tools.some((tool) => /create.*pr|exec|shell/.test(tool.name)));
  for (const action of ['fetch', 'merge', 'merge_abort']) {
    const tool = catalog.tools.find((item) => item.name === `kit_git_${action}`);
    assert.equal(tool.annotations.readOnlyHint, false);
    const input = action === 'fetch' ? { repo: 'backend' } :
      { id: 'APP-1', repo: 'backend', confirmation_token: 'a'.repeat(64) };
    assert.equal((await call(action, input)).data.code, 'CONFIRMATION_REQUIRED');
  }
  assert.equal(catalog.tools.find((item) => item.name === 'kit_git_merge_preview').annotations.readOnlyHint, true);
  const discover = await call('discover');
  assert.deepEqual(discover.data.repositories, ['backend']);
  assert.equal((await call('init')).data.code, 'CONFIRMATION_REQUIRED');
  assert.equal((await call('init', { repos: ['backend'], confirm: true })).error, false);
  const currentSettings = { workspace: 'current', development_branch: 'dev', target_branch: 'master' };
  assert.equal((await call('configure', currentSettings)).data.code, 'CONFIRMATION_REQUIRED');
  const configured = await call('configure', { ...currentSettings, confirm: true });
  assert.equal(configured.error, false, JSON.stringify(configured.data));
  assert.equal(configured.data.branches_switched, false);
  assert.equal(await git('branch', '--show-current'), 'main');
  await call('configure', { workspace: 'worktree', development_branch: 'main', target_branch: 'main', confirm: true });
  assert.equal((await call('prepare', { id: 'APP-1', repo: 'backend', fetch: false, base: 'main' })).data.code, 'CONFIRMATION_REQUIRED');
  const prepared = await call('prepare', { id: 'APP-1', repo: 'backend', fetch: false, base: 'main', confirm: true });
  assert.equal(prepared.error, false, JSON.stringify(prepared.data));
  assert.ok(prepared.data.path.startsWith(project + path.sep));
  await writeFile(path.join(prepared.data.path, 'app.py'), 'value = 2\n');
  const commit = { id: 'APP-1', repo: 'backend', files: ['app.py'], message: 'Implement fixture',
    author: { name: 'Fixture', email: 'fixture@example.invalid' } };
  const preview = await call('commit_preview', commit);
  assert.equal(preview.error, false, JSON.stringify(preview.data));
  const input = { ...commit, confirmation_token: preview.data.confirmation_token };
  assert.equal((await call('commit', input)).data.code, 'CONFIRMATION_REQUIRED');
  assert.equal((await call('commit', { ...input, confirm: true })).error, false);
  const push = await call('push_preview', { id: 'APP-1', repo: 'backend' });
  assert.equal(push.error, false, JSON.stringify(push.data));
  assert.equal(push.data.destination, 'https://forge.example.test/stash/scm/APP/backend.git');
  assert.equal((await call('push', { id: 'APP-1', repo: 'backend', confirmation_token: push.data.confirmation_token })).data.code,
    'CONFIRMATION_REQUIRED');
  assert.equal(await git('remote', 'get-url', 'origin'), 'ssh://git@forge.example.test:7999/APP/backend.git');
  const cleanup = await call('cleanup_preview', { id: 'APP-1' });
  assert.equal(cleanup.data.can_remove, true);
  const removed = await call('cleanup', { id: 'APP-1', confirmation_token: cleanup.data.confirmation_token,
    idle_confirmed: true, confirm: true });
  assert.equal(removed.error, false, JSON.stringify(removed.data));
  assert.equal(removed.data.branches_preserved, true);
  assert.equal(await git('log', 'kit/APP-1', '-1', '--format=%s'), commit.message);
  await git('branch', 'dev', 'main');
  await git('branch', 'master', 'main');
  await call('configure', { ...currentSettings, confirm: true });
  const current = await call('prepare', { id: 'APP-2', repo: 'backend', switch_branch: true, confirm: true });
  assert.equal(current.error, false, JSON.stringify(current.data));
  assert.equal(current.data.path, source);
  assert.equal(current.data.worktree_created, false);
  await writeFile(path.join(source, 'app.py'), 'value = 3\n');
  const currentCommit = { ...commit, id: 'APP-2', message: 'Change on dev' };
  const currentPreview = await call('commit_preview', currentCommit);
  assert.equal(currentPreview.error, false, JSON.stringify(currentPreview.data));
  assert.equal(currentPreview.data.workspace, 'current');
  const committed = await call('commit', { ...currentCommit, confirmation_token: currentPreview.data.confirmation_token, confirm: true });
  assert.equal(committed.error, false, JSON.stringify(committed.data));
  assert.equal(await git('log', 'dev', '-1', '--format=%s'), currentCommit.message);
  const kept = await call('cleanup_preview', { id: 'APP-2' });
  assert.equal(kept.data.can_remove, false);
  const refused = await call('cleanup', { id: 'APP-2', confirmation_token: kept.data.confirmation_token,
    idle_confirmed: true, confirm: true });
  assert.equal(refused.error, true);
  assert.equal(await git('branch', '--show-current'), 'dev');

  // Реальная локальная fetch-квитанция — подготовка fixture; MCP не разрешает file transport.
  const originalHead = await git('rev-parse', 'HEAD');
  const incoming = await git('commit-tree', await git('rev-parse', 'HEAD^{tree}'), '-p',
    await git('rev-parse', 'main'), '-m', 'Upstream equivalent tree');
  await git('update-ref', 'refs/heads/fixture-upstream', incoming);
  const remote = path.join(root, 'remote.git');
  await execute('git', ['clone', '-q', '--bare', source, remote]);
  await execute('git', ['-C', remote, 'update-ref', 'refs/heads/main', incoming]);
  await git('remote', 'set-url', 'origin', remote);
  const refusedFetch = await call('fetch', { repo: 'backend', branch: 'main', confirm: true });
  assert.equal(refusedFetch.error, true); // Ограничение origin не ослаблено ради теста.
  await execute('/usr/bin/python3', ['-c',
    'import sys; sys.path.insert(0, sys.argv[1]); from git_worker import WorkspaceGit; ' +
    'WorkspaceGit(sys.argv[2], local_test=True).dispatch({"action":"fetch","repo":"backend","branch":"main","confirm":True})',
    path.join(kitRoot, 'scripts'), project]);
  const mergeRequest = { id: 'APP-2', repo: 'backend', revision: 'origin/main' };
  const mergePreview = await call('merge_preview', mergeRequest);
  assert.equal(mergePreview.error, false, JSON.stringify(mergePreview.data));
  assert.equal(mergePreview.data.source_sha, incoming);
  assert.equal(mergePreview.data.scenario, 'merge');
  const mergeResult = await call('merge', { ...mergeRequest,
    confirmation_token: mergePreview.data.confirmation_token, confirm: true });
  assert.equal(mergeResult.error, false, JSON.stringify(mergeResult.data));
  assert.equal(mergeResult.data.outcome, 'merged_pending_commit');
  assert.equal(await git('rev-parse', 'HEAD'), originalHead);
  const mergeStatus = await call('status', { id: 'APP-2' });
  assert.deepEqual(mergeStatus.data.entries[0].merge.parents, [originalHead, incoming]);
  const mergeCommit = { id: 'APP-2', repo: 'backend', mode: 'merge', message: 'Merge fixture main', author: commit.author };
  const mergeCommitPreview = await call('commit_preview', mergeCommit);
  assert.equal(mergeCommitPreview.error, false, JSON.stringify(mergeCommitPreview.data));
  assert.deepEqual(mergeCommitPreview.data.parents, [originalHead, incoming]);
  const done = await call('commit', { ...mergeCommit,
    confirmation_token: mergeCommitPreview.data.confirmation_token, confirm: true });
  assert.equal(done.error, false, JSON.stringify(done.data));
  assert.deepEqual((await git('show', '-s', '--format=%P', 'HEAD')).split(' '), [originalHead, incoming]);
  assert.equal(await git('status', '--porcelain'), '');
});
