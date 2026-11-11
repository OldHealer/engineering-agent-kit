import { checkedFetch, jsonRequest, PublicError, requireConfirmation, safePath } from './mcp-runtime.mjs';

const segment = (value) => encodeURIComponent(value);

export async function registerService(server, service, deps, context) {
  const { z, external } = deps;
  const text = z.string().min(1);
  const id = z.number().int().positive();
  const page = z.number().int().positive().default(1);
  const confirm = z.boolean().default(false).describe('true только после явного поручения пользователя опубликовать указанный текст');
  const sha = z.string().regex(/^[a-f0-9]{40,64}$/i);
  const register = (name, description, schema, handler, write = false) => {
    server.registerTool(name, {
      description, inputSchema: schema.shape,
      annotations: { readOnlyHint: !write, destructiveHint: false, idempotentHint: !write, openWorldHint: true },
    }, async (input) => {
      try {
        if (write) requireConfirmation(input);
        const result = context.redact(await handler(input));
        return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      } catch (error) {
        // Не сериализуем Axios/fetch exceptions: там могут быть заголовки Authorization.
        const { safeError } = await import('./mcp-runtime.mjs');
        return { isError: true, content: [{ type: 'text', text: JSON.stringify(context.redact(safeError(error))) }] };
      }
    });
  };

  if (service === 'bitbucket') {
    const { BbClient } = await external('packages/bitbucket/dist/client.js');
    const { getPrChanges, getPrDiff } = await external('packages/bitbucket/dist/tools/diff.js');
    const { listPrs } = await external('packages/bitbucket/dist/tools/prs.js');
    const { getPrComments } = await external('packages/bitbucket/dist/tools/comments.js');
    const project = z.object({ projectKey: text });
    const pr = project.extend({ repo: text, id });
    const bb = (input) => {
      const conn = context.connection('bitbucket');
      const config = { host: conn.host, token: conn.token, projectKey: input.projectKey,
        defaultTarget: {}, userAliases: {} };
      // POST нельзя повторять: при сетевом обрыве комментарий мог уже сохраниться.
      const client = new BbClient({ config, maxRetries: 0, timeoutMs: 20000, fetchImpl: checkedFetch });
      return { client, config };
    };
    const route = (input) => `/rest/api/1.0/projects/${segment(input.projectKey)}/repos/${segment(input.repo)}/pull-requests/${input.id}`;
    register('bb_connection_check', 'Проверить чтение списка проектов Bitbucket, не возвращая их содержимое', z.object({}), async () => {
      const result = await bb({ projectKey: 'UNUSED' }).client.request('GET', '/rest/api/1.0/projects?limit=1');
      if (!Array.isArray(result?.values)) throw new PublicError('UNEXPECTED_RESPONSE', 'Не получен ожидаемый список проектов');
      return { reachable: true, returnedItems: result.values.length };
    });
    // Внешний пакет может использовать другую версию Zod: не смешиваем схемы.
    register('bb_list_prs', 'Список PR; projectKey задаётся явно для каждого проекта', project.extend({ repo: text,
      state: z.enum(['OPEN', 'MERGED', 'DECLINED', 'ALL']).optional(), author: text.optional(), reviewer: text.optional(),
      all: z.boolean().optional(), limit: id.max(1000).optional() }), (input) => listPrs(bb(input), input));
    register('bb_get_pr', 'Данные PR с source/target и их SHA; исходный ответ Bitbucket', pr, async (input) => {
      const result = await bb(input).client.request('GET', route(input));
      if (!result?.fromRef?.latestCommit || !result?.toRef?.latestCommit) throw new PublicError('UNEXPECTED_RESPONSE', 'Ответ не содержит ревизии PR');
      return result;
    });
    register('bb_get_pr_changes', 'Изменённые файлы PR; проверьте возможное усечение на больших PR', pr, (input) => getPrChanges(bb(input), input));
    register('bb_get_pr_diff', 'Diff одного разрешённого файла PR; сначала получите список файлов', pr.extend({ path: text,
      contextLines: z.number().int().nonnegative().max(200).optional() }), (input) => {
      safePath(input.path);
      return getPrDiff(bb(input), { ...input, all: false });
    });
    register('bb_get_pr_comments', 'Комментарии PR для проверки существующих замечаний и исключения дублей', pr.extend({
      state: z.enum(['OPEN', 'RESOLVED', 'PENDING', 'ALL']).optional(), limit: id.max(1000).optional(),
    }), (input) => getPrComments(bb(input), input));
    register('bb_add_inline_comment', 'Опубликовать согласованное inline-замечание. Не повторять автоматически после ошибки. expectedHead/expectedTarget должны соответствовать проверенному PR.',
      pr.extend({ text, path: text, line: id, lineType: z.enum(['ADDED', 'REMOVED', 'CONTEXT']),
        fileType: z.enum(['FROM', 'TO']), expectedHead: sha, expectedTarget: sha, confirm }), async (input) => {
        safePath(input.path);
        const { client } = bb(input);
        const current = await client.request('GET', route(input));
        if (current?.fromRef?.latestCommit !== input.expectedHead || current?.toRef?.latestCommit !== input.expectedTarget) {
          throw new PublicError('STALE_REVIEW', 'PR изменился после ревью; обновите замечание и его привязку');
        }
        const result = await client.request('POST', `${route(input)}/comments`, {
          text: input.text,
          anchor: { path: input.path, line: input.line, lineType: input.lineType, fileType: input.fileType,
            diffType: 'EFFECTIVE', fromHash: input.expectedTarget, toHash: input.expectedHead },
        });
        if (!result?.id) throw new PublicError('WRITE_RESULT_UNKNOWN', 'Сервис не подтвердил ID комментария; проверьте список комментариев перед повтором');
        return { id: result.id, text: result.text, state: result.state, anchor: result.anchor };
      }, true);
    return;
  }

  if (service === 'jira') {
    const { createJiraClient } = await external('packages/jira/dist/client.js');
    const { mdToWiki, wikiToMd } = await external('packages/jira/dist/markup.js');
    const { htmlToMarkdown } = await external('packages/jira/dist/helpers/confluence.js');
    const task = z.object({ taskKey: z.string().regex(/^[A-Z][A-Z0-9]*-\d+$/) });
    const jira = () => {
      const conn = context.connection('jira');
      return createJiraClient({ jiraHost: conn.host, jiraPat: conn.token, jiraProjectKey: 'UNUSED' });
    };
    register('jira_connection_check', 'Проверить авторизованное чтение текущего пользователя Jira', z.object({}), async () => {
      const result = await jsonRequest(context.connection('jira'), '/rest/api/2/myself');
      return { authenticated: Boolean(result.name || result.key || result.accountId) };
    });
    register('jira_task_get', 'Прочитать задачу, комментарии и ссылки. Не скачивает вложения и не отправляет их внешним моделям.', task, async (input) => {
      const result = await jira().getIssue({ issueIdOrKey: input.taskKey, expand: 'renderedFields' });
      return { key: result.key, fields: result.fields,
        descriptionMarkdown: result.renderedFields?.description
          ? htmlToMarkdown(result.renderedFields.description) : wikiToMd(result.fields?.description ?? '') };
    });
    register('jira_search', 'Поиск Jira по явному JQL, одна страница; project key не берётся из общих секретов',
      z.object({ jql: text, startAt: z.number().int().nonnegative().default(0), maxResults: id.max(50).default(20) }),
      (input) => jsonRequest(context.connection('jira'), `/rest/api/2/search?${new URLSearchParams({
        jql: input.jql, startAt: input.startAt, maxResults: input.maxResults, fields: 'summary,status,issuetype,project' })}`));
    register('jira_comment_create', 'Добавить согласованный Markdown-комментарий к задаче Jira. Не повторять автоматически после сетевой ошибки.',
      task.extend({ body: text, confirm }), async (input) => {
        const result = await jira().addComment({ issueIdOrKey: input.taskKey, comment: mdToWiki(input.body) });
        if (!result?.id) throw new PublicError('WRITE_RESULT_UNKNOWN', 'Не получен ID комментария; проверьте задачу перед повтором');
        return { id: result.id, taskKey: input.taskKey, created: result.created };
      }, true);
    return;
  }

  if (service === 'confluence') {
    const { htmlToMarkdown } = await external('packages/jira/dist/helpers/confluence.js');
    const request = (route) => jsonRequest(context.connection('confluence'), `/rest/api${route}`);
    register('confluence_connection_check', 'Проверить авторизованное чтение текущего пользователя Confluence', z.object({}), async () => {
      const result = await request('/user/current');
      return { authenticated: result.type !== 'anonymous' && Boolean(result.username || result.userKey || result.accountId) };
    });
    register('confluence_page_get', 'Прочитать страницу Confluence по pageId и преобразовать storage HTML в Markdown',
      z.object({ pageId: z.string().regex(/^\d+$/) }), async (input) => {
        const result = await request(`/content/${input.pageId}?expand=body.storage,version,space`);
        return { id: result.id, title: result.title, version: result.version?.number,
          space: result.space?.key, markdown: htmlToMarkdown(result.body?.storage?.value), links: result._links };
      });
    register('confluence_search', 'Поиск страниц Confluence по явному CQL, одна страница',
      z.object({ cql: text, start: z.number().int().nonnegative().default(0), limit: id.max(50).default(20) }),
      (input) => request(`/content/search?${new URLSearchParams(input)}`));
    return;
  }

  if (service === 'github') {
    const repo = z.object({ owner: text, repo: text });
    const pr = repo.extend({ number: id });
    const route = (input) => `/repos/${segment(input.owner)}/${segment(input.repo)}/pulls/${input.number}`;
    const request = (route, method, body) => jsonRequest(context.connection('github'), route, method, body);
    register('github_connection_check', 'Проверить авторизованное чтение текущего пользователя GitHub', z.object({}), async () => {
      const result = await request('/user');
      return { authenticated: Boolean(result.id && result.login) };
    });
    register('github_get_pr', 'Получить PR, включая head.sha и base.sha', pr, (input) => request(route(input)));
    register('github_list_prs', 'Список PR GitHub, одна страница', repo.extend({ state: z.enum(['open', 'closed', 'all']).default('open'), page }),
      (input) => request(`/repos/${segment(input.owner)}/${segment(input.repo)}/pulls?state=${input.state}&page=${input.page}&per_page=50`));
    register('github_get_pr_files', 'Изменённые файлы PR без patch; запросите diff нужного файла отдельно', pr.extend({ page }), async (input) => {
      const files = await request(`${route(input)}/files?page=${input.page}&per_page=100`);
      return { page: input.page, mayHaveMore: files.length === 100, files: files.map(({ patch, ...file }) => file) };
    });
    register('github_get_pr_diff', 'Diff одного разрешённого файла PR. GitHub может усекать patch больших файлов.', pr.extend({ path: text }), async (input) => {
      safePath(input.path);
      for (let current = 1; current <= 30; current += 1) {
        const files = await request(`${route(input)}/files?page=${current}&per_page=100`);
        const found = files.find((file) => file.filename === input.path);
        if (found) return { path: found.filename, patch: found.patch ?? null, patchMayBeTruncated: true };
        if (files.length < 100) break;
      }
      throw new PublicError('FILE_NOT_FOUND', 'Файл не найден среди доступных изменений PR');
    });
    register('github_get_pr_comments', 'Inline-комментарии PR, одна страница для проверки дублей', pr.extend({ page }),
      (input) => request(`${route(input)}/comments?page=${input.page}&per_page=100`));
    register('github_add_inline_comment', 'Опубликовать согласованное inline-замечание GitHub на проверенном коммите. Не повторять автоматически после ошибки.',
      pr.extend({ body: text, path: text, line: id, side: z.enum(['LEFT', 'RIGHT']), expectedHead: sha, expectedBase: sha, confirm }), async (input) => {
        safePath(input.path);
        const current = await request(route(input));
        if (current.head?.sha !== input.expectedHead || current.base?.sha !== input.expectedBase) {
          throw new PublicError('STALE_REVIEW', 'PR изменился после ревью; обновите замечание и его привязку');
        }
        const result = await request(`${route(input)}/comments`, 'POST', {
          body: input.body, commit_id: input.expectedHead, path: input.path, line: input.line, side: input.side,
        });
        if (!result?.id) throw new PublicError('WRITE_RESULT_UNKNOWN', 'Не получен ID комментария; проверьте PR перед повтором');
        return { id: result.id, url: result.html_url, commit: result.commit_id };
      }, true);
    return;
  }
  throw new PublicError('UNKNOWN_SERVICE', 'Неизвестный сервис MCP');
}
