import { readFileSync, lstatSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseEnv } from 'node:util';

export const kitRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export function localSettings() {
  let data;
  try {
    const file = path.join(kitRoot, '.engineering-kit/local.json');
    if (!lstatSync(file).isFile()) throw new Error('invalid local config');
    data = JSON.parse(readFileSync(file, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return {};
    throw new Error('Локальная конфигурация недоступна или имеет неверный JSON; значения скрыты');
  }
  if (!data || Array.isArray(data) || typeof data !== 'object'
    || Object.keys(data).some((key) => !['ai_box', 'network_hosts', 'mcp_hosts'].includes(key))) {
    throw new Error('Неизвестная структура локальной конфигурации');
  }
  if (data.ai_box !== undefined && (typeof data.ai_box !== 'string' || !path.isAbsolute(data.ai_box))) {
    throw new Error('Путь зависимостей должен быть абсолютным');
  }
  return data;
}
export const defaultAiBox = process.env.KIT_AI_BOX ?? localSettings().ai_box
  ?? path.join(kitRoot, '.engineering-kit/dependencies/ai-box');
export function allowedHosts(service) {
  const hosts = localSettings().mcp_hosts?.[service]
    ?? (service === 'github' ? ['api.github.com'] : service === 'git' ? ['github.com', 'api.github.com'] : []);
  if (!Array.isArray(hosts) || hosts.some((host) => typeof host !== 'string'
    || !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]+$/.test(host))) {
    throw new Error('Нужны списки точных DNS-имён');
  }
  return hosts.join(',');
}

export class PublicError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.httpStatus = status;
  }
}

export async function dependencies(aiBox) {
  const require = createRequire(path.join(aiBox, 'package.json'));
  const module = async (name) => import(pathToFileURL(require.resolve(name)).href);
  const external = async (relative) => import(pathToFileURL(path.join(aiBox, relative)).href);
  return {
    ...(await module('@modelcontextprotocol/sdk/server/mcp.js')),
    ...(await module('@modelcontextprotocol/sdk/server/stdio.js')),
    ...(await module('zod')),
    external,
    module,
  };
}

export function runtime(envFile = path.join(kitRoot, '.engineering-kit/connections.env')) {
  // Файл разбирается как данные, не исполняется shell и не попадает в конфиг Codex.
  let env;
  try {
    env = parseEnv(readFileSync(envFile, 'utf8'));
  } catch {
    throw new PublicError('CONNECTIONS_FILE_UNAVAILABLE', 'Процесс MCP не смог загрузить общий файл подключений; содержимое не выводилось');
  }
  const secrets = Object.entries(env)
    .filter(([key, value]) => /TOKEN|PAT$|PASSWORD|SECRET|API_KEY/i.test(key) && value)
    .map(([, value]) => value).sort((a, b) => b.length - a.length);
  const redact = (value) => {
    const serialized = JSON.stringify(value ?? null);
    // Заменяем и JSON-экранированное представление, не только простой токен.
    let safe = serialized;
    for (const secret of secrets) {
      safe = safe.split(JSON.stringify(secret).slice(1, -1)).join('[REDACTED]');
    }
    return JSON.parse(safe);
  };
  const variable = (names, fallback) => {
    const found = names.map((name) => env[name]).find((value) => value?.trim());
    if (found) return found.trim();
    if (fallback !== undefined) return fallback;
    throw new PublicError('MISSING_CONFIGURATION', `Не задана переменная: ${names.join(' / ')}`);
  };
  const host = (names, fallback) => {
    const url = new URL(variable(names, fallback));
    const permittedHosts = process.env.KIT_MCP_ALLOWED_HOSTS?.split(',').map((item) => item.trim().toLowerCase());
    if (permittedHosts && !permittedHosts.includes(url.hostname.toLowerCase())) {
      throw new PublicError('HOST_DENIED', 'Адрес MCP не входит в точный список разрешённых хостов активного профиля');
    }
    if (url.username || url.password || url.search || url.hash) {
      throw new PublicError('INVALID_HOST', 'Адрес сервиса не должен содержать авторизацию, query или fragment');
    }
    const localTest = process.env.KIT_MCP_LOCAL_TEST === '1'
      && ['127.0.0.1', 'localhost'].includes(url.hostname);
    if (url.protocol !== 'https:' && !(localTest && url.protocol === 'http:')) {
      throw new PublicError('INVALID_HOST', 'Для сервисов требуется HTTPS');
    }
    return url.href.replace(/\/$/, '');
  };
  const connection = (service) => {
    const definitions = {
      bitbucket: [['BB_HOST', 'BITBUCKET_HOST', 'BITBUCKET_URL', 'BITBUCKET_SERVER_URL'], ['BB_TOKEN', 'BITBUCKET_TOKEN', 'BITBUCKET_PAT']],
      jira: [['JIRA_HOST', 'JIRA_URL'], ['JIRA_PAT', 'JIRA_TOKEN', 'JIRA_API_TOKEN']],
      confluence: [['CONFLUENCE_HOST', 'CONFLUENCE_URL'], ['CONFLUENCE_PAT', 'CONFLUENCE_TOKEN', 'CONFLUENCE_API_TOKEN']],
      github: [['GITHUB_API_URL'], ['GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_PAT', 'GITHUB_PERSONAL_ACCESS_TOKEN']],
    };
    const [hosts, tokens] = definitions[service];
    return { host: host(hosts, service === 'github' ? 'https://api.github.com' : undefined), token: variable(tokens) };
  };
  return { connection, redact };
}

export function safeError(error) {
  const chain = [];
  for (let current = error; current && chain.length < 8; current = current.cause) chain.push(current);
  const status = chain.map((item) => item.httpStatus ?? item.response?.status ?? item.status)
    .find((value) => Number.isInteger(value) && value >= 100 && value <= 599);
  const codes = chain.map((item) => item.code);
  let code = 'INTEGRATION_ERROR';
  let message = 'Ошибка интеграции; подробности скрыты, чтобы не раскрыть учётные данные';
  if (error instanceof PublicError) {
    code = error.code;
    message = error.message;
  } else if (codes.some((value) => ['EPERM', 'EACCES'].includes(value))
    || chain.some((item) => /operation not permitted/i.test(item.message ?? ''))) {
    code = 'NETWORK_BLOCKED';
    message = 'Сетевое соединение запрещено средой; токен не проверен';
  } else if (status) {
    code = status === 401 ? 'AUTHENTICATION_FAILED' : status === 403 ? 'ACCESS_DENIED' : 'HTTP_ERROR';
    message = `Сервис ответил HTTP ${status}`;
  } else if (codes.some((value) => ['ENOTFOUND', 'EAI_AGAIN'].includes(value))) {
    code = 'DNS_ERROR';
    message = 'Не удалось разрешить адрес сервиса; токен не проверен';
  } else if (chain.some((item) => /timeout|timed out/i.test(item.message ?? ''))
    || chain.some((item) => ['AbortError', 'TimeoutError'].includes(item.name))) {
    code = 'TIMEOUT';
    message = 'Превышено время ожидания; результат запроса неизвестен';
  } else if (codes.some((value) => ['ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH', 'ECONNRESET'].includes(value))) {
    code = 'NETWORK_ERROR';
    message = 'Сервис недоступен по сети; токен не проверен';
  }
  return { code, message, ...(status ? { httpStatus: status } : {}) };
}

export async function checkedFetch(url, init = {}) {
  const response = await fetch(url, { ...init, redirect: 'manual', signal: init.signal ?? AbortSignal.timeout(20000) });
  if (response.status >= 300 && response.status < 400) {
    throw new PublicError('REDIRECT_REFUSED', 'Перенаправление не выполнено; проверьте адрес сервиса', response.status);
  }
  return response;
}

export async function jsonRequest(connection, route, method = 'GET', body) {
  const response = await checkedFetch(`${connection.host}${route}`, {
    method,
    headers: { Accept: 'application/json', Authorization: `Bearer ${connection.token}`,
      'Content-Type': 'application/json', 'User-Agent': 'engineering-agent-kit' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new PublicError(
    response.status === 401 ? 'AUTHENTICATION_FAILED' : response.status === 403 ? 'ACCESS_DENIED' : 'HTTP_ERROR',
    `Сервис ответил HTTP ${response.status}`, response.status,
  );
  if (!response.headers.get('content-type')?.includes('json')) {
    throw new PublicError('UNEXPECTED_RESPONSE', 'Ожидался JSON, сервис вернул другой формат');
  }
  return response.json();
}

export function requireConfirmation(input) {
  if (input.confirm !== true) {
    throw new PublicError('CONFIRMATION_REQUIRED', 'Сначала получите явное поручение пользователя на публикацию этого комментария');
  }
}

export function safePath(value) {
  if (value.startsWith('/') || value.includes('\\') || value.split('/').includes('..')
    || /(?:^|\/)(?:\.env(?:\.[^/]*)?|[^/]*(?:secret|credentials)[^/]*|[^/]*\.(?:pem|key))(?:\/|$)/i.test(value)) {
    throw new PublicError('PATH_REFUSED', 'Путь недопустим или относится к секретным файлам');
  }
  return value;
}
