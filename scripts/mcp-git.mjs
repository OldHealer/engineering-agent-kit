import { spawn } from 'node:child_process';
import path from 'node:path';
import { kitRoot, PublicError, requireConfirmation, safeError } from './mcp-runtime.mjs';

export function registerGit(server, deps, context) {
  const { z } = deps;
  const project = process.env.KIT_GIT_PROJECT;
  if (!project || !path.isAbsolute(project)) {
    throw new PublicError('PROJECT_REQUIRED', 'Запускайте Git MCP через kit из каталога выбранного проекта');
  }
  const name = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/);
  const task = z.object({ id: name });
  const repo = task.extend({ repo: name });
  const confirmed = { confirm: z.boolean().default(false) };
  const token = z.string().regex(/^[a-f0-9]{16,64}$/);
  const commit = repo.extend({ files: z.array(z.string().min(1)).min(1).max(1000), message: z.string().min(1).max(16000),
    author: z.object({ name: z.string().min(1), email: z.string().min(1) }).optional() });
  let pending = Promise.resolve();
  const execute = (action, input) => new Promise((resolve, reject) => {
    const connections = {};
    if (['remote_check', 'push_preview', 'push'].includes(action) || action === 'prepare' && input.fetch !== false) {
      for (const service of ['bitbucket', 'github']) {
        try { connections[service] = context.connection(service); }
        catch (error) {
          if (!(error instanceof PublicError) || !['MISSING_CONFIGURATION', 'HOST_DENIED'].includes(error.code)) throw error;
        }
      }
    }
    const child = spawn(process.env.KIT_PYTHON || '/usr/bin/python3',
      [path.join(kitRoot, 'scripts/git_worker.py'), '--project', project],
      { env: { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '';
    const timer = setTimeout(() => { child.kill(); reject(new PublicError('TIMEOUT', 'Git-операция превысила время ожидания; проверьте состояние перед повтором')); }, 150000);
    child.stdout.on('data', (chunk) => {
      output += chunk;
      if (output.length > 4 * 1024 * 1024) { child.kill(); reject(new PublicError('OUTPUT_LIMIT', 'Ответ Git слишком велик')); }
    });
    child.stderr.on('data', () => {});
    child.on('error', () => { clearTimeout(timer); reject(new PublicError('GIT_START_FAILED', 'Не удалось запустить ограниченный Git backend')); });
    child.on('close', () => {
      clearTimeout(timer);
      try {
        const reply = JSON.parse(output);
        if (!reply.ok) reject(new PublicError(reply.code, reply.message));
        else resolve(reply.result);
      } catch (error) {
        reject(error instanceof PublicError ? error : new PublicError('GIT_RESULT_UNKNOWN', 'Результат операции не получен; проверьте состояние перед повтором'));
      }
    });
    child.stdin.on('error', () => {});
    // Авторизация только во входном pipe доверенного дочернего процесса, не в аргументах инструмента.
    child.stdin.end(JSON.stringify({ ...input, action, _connections: connections }));
  });
  const register = (action, description, schema, write = false, destructive = false) => {
    server.registerTool(`kit_git_${action}`, { description, inputSchema: schema.shape,
      annotations: { readOnlyHint: !write, destructiveHint: destructive, idempotentHint: !write, openWorldHint: true } }, async (input) => {
      const operation = async () => {
        try {
          if (write) requireConfirmation(input);
          return { content: [{ type: 'text', text: JSON.stringify(context.redact(await execute(action, input))) }] };
        } catch (error) {
          return { isError: true, content: [{ type: 'text', text: JSON.stringify(context.redact(safeError(error))) }] };
        }
      };
      const result = pending.then(operation, operation);
      pending = result.then(() => {});
      return result;
    });
  };
  register('discover', 'Обнаружить исходные репозитории только выбранного при запуске проекта', z.object({}));
  register('init', 'Подключить согласованные репозитории проекта; существующую конфигурацию сохранить',
    z.object({ repos: z.array(z.union([name, z.literal('.')])).optional(), ...confirmed }), true);
  register('configure', 'После согласия сохранить режим проекта: current для исходной папки или worktree. Ветки не переключает; main/master/целевая ветка для прямой разработки запрещены.',
    z.object({ workspace: z.enum(['current', 'worktree']), development_branch: z.string().min(1),
      target_branch: z.string().min(1), ...confirmed }), true);
  register('status', 'Задачи, режимы и состояние рабочих папок проекта, без чтения содержимого файлов', z.object({ id: name.optional() }));
  register('prepare', 'Начать задачу согласно настройкам проекта. current привязывает исходную папку без worktree/fetch; worktree создаёт отдельную копию. switch_branch/include_changes допустимы только после отдельного согласования.',
    repo.extend({ mode: z.enum(['review', 'feature']).default('feature'), base: z.string().min(1).optional(),
      workspace: z.enum(['current', 'worktree']).optional(), head: z.string().min(1).optional(), fetch: z.boolean().optional(),
      switch_branch: z.boolean().default(false), include_changes: z.boolean().default(false), ...confirmed }), true);
  register('commit_preview', 'Предпросмотр точного состава и текста коммита. Не выполняет commit; author задаётся явно, если его нет в локальном Git config.', commit);
  register('commit', 'Создать коммит только после согласования показанного состава и текста. Изменение HEAD/файлов/текста отменяет подтверждение.',
    commit.extend({ confirmation_token: token, ...confirmed }), true);
  register('push_preview', 'Предпросмотр SHA, ветки и HTTPS-адреса. В current читает удалённую ветку для проверки всех неотправленных коммитов; ничего не публикует.', repo);
  register('push', 'Отправить согласованный SHA в ветку задачи или явно настроенную ветку разработки current. Без push в main/master/целевую ветку, force, тегов, удаления веток и создания PR.',
    repo.extend({ confirmation_token: token, ...confirmed }), true);
  register('cleanup_preview', 'Проверить сохранность worktree и показать точные пути удаления; ничего не удаляет', task);
  register('cleanup', 'Удалить только согласованные чистые worktree kit, сохранив ветки, коммиты и отчёты.',
    task.extend({ confirmation_token: token, idle_confirmed: z.boolean(), ...confirmed }), true, true);
  register('remote_check', 'Прочитать HEAD или ветку kit/ID через HTTPS. Перед повтором неоднозначного push сравните sha с согласованной ревизией.',
    z.object({ repo: name, id: name.optional() }));
}
