import { spawn } from 'node:child_process';
import path from 'node:path';
import { kitRoot, PublicError, requireConfirmation, safeError } from './mcp-runtime.mjs';

export function registerQA(server, deps) {
  const { z } = deps;
  const project = process.env.KIT_QA_PROJECT;
  const store = process.env.KIT_QA_STORE;
  if (!project || !store || !path.isAbsolute(project) || !path.isAbsolute(store)) {
    throw new PublicError('QA_SCOPE_REQUIRED', 'QA needs project and protected ledger from kit launch');
  }
  const id = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/);
  const argv = z.array(z.string().min(1).max(8192)).max(64);
  const contract = z.object({ id, image: z.string().regex(/^sha256:[a-f0-9]{64}$/),
    env: z.record(z.string(), z.string().max(1024)), command: argv,
    storage_target: z.string().min(1), phases: z.record(z.string(), z.array(argv).min(1).max(10)),
    ttl_seconds: z.number().int().min(60).max(86400) });
  const token = z.string().regex(/^[a-f0-9]{64}$/);
  const grant = z.object({ contract });
  const run = z.object({ id });
  for (const [action, schema, write, confirm] of [
    ['preview', grant, false, false], ['grant', grant.extend({ confirmation_token: token, confirm: z.boolean().default(false) }), true, true],
    ['status', run, false, false], ['start', run, true, false], ['stop', run, true, false],
    ['restart', run, true, false], ['cleanup', run, true, false],
    ['check', run.extend({ phase: z.enum(['seed', 'migrate', 'test']), attempt: id.default('default') }), true, false],
    ['renew_preview', run, false, false], ['renew', run.extend({ confirmation_token: token, confirm: z.boolean().default(false) }), true, true],
  ]) {
    server.registerTool(`kit_qa_${action}`, {
      description: `${action}: isolated QA only. Grant/renew need user approval; other operations require a valid protected grant and verified resource ownership. No host exec, bind mounts, pull, ports or working databases. Cleanup removes only the recorded container and its anonymous volume.`,
      inputSchema: schema.shape,
      annotations: { readOnlyHint: !write, destructiveHint: ['cleanup', 'stop', 'restart'].includes(action),
        idempotentHint: ['status', 'stop', 'cleanup'].includes(action), openWorldHint: true },
    }, async (input) => {
      try {
        if (confirm) requireConfirmation(input);
        const result = await new Promise((resolve, reject) => {
          const child = spawn('/usr/bin/python3', [path.join(kitRoot, 'scripts/qa_worker.py'), '--project', project, '--store', store, '--owner-pid', String(process.pid)],
            { cwd: kitRoot, env: { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['pipe', 'pipe', 'pipe'] });
          let output = '';
          const timer = setTimeout(() => { child.kill(); reject(new PublicError('QA_TIMEOUT', 'Operation interrupted; inspect status before retry')); }, 150000);
          child.stdout.on('data', (chunk) => { output += chunk; if (output.length > 1024 * 1024) child.kill(); });
          child.stderr.on('data', () => {});
          child.on('error', () => { clearTimeout(timer); reject(new PublicError('QA_START_FAILED', 'QA backend unavailable')); });
          child.on('close', () => {
            clearTimeout(timer);
            try {
              const reply = JSON.parse(output);
              if (!reply.ok) reject(new PublicError('QA_REFUSED', reply.message)); else resolve(reply.result);
            } catch (error) { reject(error instanceof PublicError ? error : new PublicError('QA_UNKNOWN', 'Inspect QA status; do not repeat creation blindly')); }
          });
          child.stdin.on('error', () => {});
          child.stdin.end(JSON.stringify({ ...input, action }));
        });
        return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text', text: JSON.stringify(safeError(error)) }] };
      }
    });
  }
}
