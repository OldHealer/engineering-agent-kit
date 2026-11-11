"""Личная сетевая политика kit: наследование, предпросмотр и потребности проекта."""

import copy
import json
import os
from pathlib import Path
import re
import socket
import stat
import tomllib
from urllib.parse import urlsplit


PROFILES = {"python": ("pypi.org", "files.pythonhosted.org"), "node": ("registry.npmjs.org",)}
MANIFESTS = {"pyproject.toml": "python", "uv.lock": "python", "poetry.lock": "python",
             "package.json": "node", "package-lock.json": "node", "npm-shrinkwrap.json": "node",
             "yarn.lock": "node", "pnpm-lock.yaml": "node"}
SKIP = {".git", ".worktrees", ".engineering-kit", "node_modules", ".venv", "venv", "__pycache__",
        ".cache", ".next", "dist", "build", ".ssh", ".aws", ".azure", ".kube", ".gnupg",
        ".docker", ".codex", ".claude", ".agents", ".config"}
SENSITIVE = re.compile(r"password|secret|credential|парол", re.I)
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MANIFESTS = 200


class NetworkPolicyError(ValueError):
    pass


def domain(value):
    if not isinstance(value, str):
        raise NetworkPolicyError("Домен должен быть точным DNS-именем без URL, порта и wildcard")
    try:
        normalized = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise NetworkPolicyError("Некорректное DNS-имя; значение скрыто") from None
    labels = normalized.split(".")
    if (len(normalized) > 253 or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise NetworkPolicyError("Домен должен быть точным DNS-именем без URL, порта и wildcard")
    try:
        # inet_aton распознаёт также сокращённые, octal и hex формы IPv4 без DNS-запроса.
        socket.inet_aton(normalized)
    except OSError:
        return normalized
    raise NetworkPolicyError("Для сетевой политики нужны DNS-имена; разрешения IP задаются отдельно")


def project_path(value, require_exists=True):
    path = Path(value).expanduser().absolute()
    if (path == Path('/') or '..' in path.parts
            or (require_exists and (not path.is_dir() or path.resolve() != path))):
        raise NetworkPolicyError("Нужна существующая папка проекта без симлинков; корень диска не подходит")
    return str(path)


def validate(value):
    if not isinstance(value, dict) or set(value) - {"allow", "deny", "projects"}:
        raise NetworkPolicyError("Неизвестная структура сетевой политики; значения скрыты")

    def scope(entry):
        if not isinstance(entry, dict) or set(entry) - {"allow", "deny"}:
            raise NetworkPolicyError("Неизвестная структура сетевого дополнения проекта")
        result = {}
        for key in ("allow", "deny"):
            items = entry.get(key, [])
            if not isinstance(items, list):
                raise NetworkPolicyError("Список доменов имеет неверный формат")
            result[key] = sorted({domain(item) for item in items})
        return result

    result = scope({key: value[key] for key in ("allow", "deny") if key in value})
    projects = value.get("projects", {})
    if not isinstance(projects, dict):
        raise NetworkPolicyError("Сетевые дополнения проектов должны быть таблицей")
    result["projects"] = {}
    for root, entry in projects.items():
        if not isinstance(root, str) or not Path(root).is_absolute() or project_path(root, False) != root:
            raise NetworkPolicyError("Путь сохранённого сетевого дополнения проекта некорректен")
        result["projects"][root] = scope(entry)
    return result


def update(value=None, project=None, allow=(), deny=(), remove=(), profiles=()):
    result = copy.deepcopy(validate({} if value is None else value))
    scope = result
    if project is not None:
        root = project_path(project, require_exists=bool(allow or profiles))
        scope = result["projects"].setdefault(root, {"allow": [], "deny": []})
    additions = {domain(item) for item in allow or ()}
    explicit_allow = additions.copy()
    for name in profiles or ():
        if name not in PROFILES:
            raise NetworkPolicyError("Неизвестный профиль реестров")
        additions.update(PROFILES[name])
    denied = {domain(item) for item in deny or ()}
    removed = {domain(item) for item in remove or ()}
    if (additions | denied) & removed or explicit_allow & denied:
        raise NetworkPolicyError("Один домен нельзя одновременно добавить, запретить или удалить")
    # Запрет не снимается косвенно через allow: отзыв требует явного remove и нового preview.
    scope["allow"] = sorted((set(scope["allow"]) - removed) | additions)
    scope["deny"] = sorted((set(scope["deny"]) - removed) | denied)
    return validate(result)


def matching_projects(value, project):
    root = Path(project).resolve()
    return sorted((path for path in value["projects"] if root.is_relative_to(Path(path))),
                  key=lambda path: len(Path(path).parts))


def effective(value=None, project=None, base_domains=None):
    value = validate({} if value is None else value)
    base_domains = {} if base_domains is None else base_domains
    if not isinstance(base_domains, dict) or any(not isinstance(key, str) or not isinstance(mode, str)
            or mode not in {"allow", "deny"} for key, mode in base_domains.items()):
        raise NetworkPolicyError("Некорректные унаследованные правила доменов; значения скрыты")
    result = {}
    for host, mode in base_domains.items():
        if mode == 'deny' or result.get(host.lower()) != 'deny':
            result[host.lower()] = mode
    scopes = [value]
    if project is not None:
        scopes += [value["projects"][path] for path in matching_projects(value, project)]
    for scope in scopes:
        for host in scope["allow"]:
            if result.get(host) != "deny":
                result[host] = "allow"
        for host in scope["deny"]:
            result[host] = "deny"
    return dict(sorted(result.items()))


def access(host, rules):
    def matches(pattern):
        if pattern == '*':
            return True
        if pattern.startswith('**.'):
            return host == pattern[3:] or host.endswith('.' + pattern[3:])
        if pattern.startswith('*.'):
            return host.endswith('.' + pattern[2:])
        return host == pattern
    found = [mode for pattern, mode in rules.items() if matches(pattern)]
    return 'deny' if 'deny' in found else 'allow' if 'allow' in found else 'missing'


def _urls(value):
    if isinstance(value, str):
        yield from re.findall(r'https?://[^\s\"\'<>]+', value)
    elif isinstance(value, list):
        for item in value:
            yield from _urls(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _urls(item)


def _manifest_urls(name, text):
    if name.endswith('.json'):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('Manifest must be an object')
        if name == 'package.json':
            return list(_urls({key: data.get(key) for key in
                              ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')}))
        urls = []
        pending = [data]
        while pending:
            entry = pending.pop()
            if isinstance(entry, dict):
                urls.extend(_urls(entry.get('resolved')))
                pending.extend(item for item in entry.values() if isinstance(item, (dict, list)))
            elif isinstance(entry, list):
                pending.extend(entry)
        return urls
    if name in {'pyproject.toml', 'uv.lock', 'poetry.lock'}:
        data = tomllib.loads(text)
        if name == 'pyproject.toml':
            tool = data.get('tool', {})
            project = data.get('project', {})
            if not isinstance(tool, dict) or not isinstance(project, dict):
                raise ValueError('Manifest project and tool must be tables')
            uv, poetry = tool.get('uv', {}), tool.get('poetry', {})
            if not isinstance(uv, dict) or not isinstance(poetry, dict):
                raise ValueError('Manifest package managers must be tables')
            data = {'dependencies': project.get('dependencies', []),
                    'optional-dependencies': project.get('optional-dependencies', {}),
                    'dependency-groups': data.get('dependency-groups', {}), 'uv': uv.get('index', []),
                    'sources': uv.get('sources', {}), 'poetry': poetry.get('source', [])}
        return list(_urls(data))
    return list(_urls(text))


def discover(project, max_depth=4):
    project = Path(project_path(project))
    if any(SENSITIVE.search(part) for part in project.parts):
        raise NetworkPolicyError("Поиск manifest в папке с секретным именем запрещён")
    manifests, issues, ecosystems, hosts = [], [], set(), set()
    examined = 0

    def inaccessible(error):
        issues.append({'kind': 'directory_unavailable'})

    for root, directories, files in os.walk(project, followlinks=False, onerror=inaccessible):
        relative = Path(root).relative_to(project)
        directories[:] = [name for name in sorted(directories) if name not in SKIP
                          and not SENSITIVE.search(name) and not name.startswith('.env')
                          and len(relative.parts) < max_depth]
        for name in sorted(set(files) & MANIFESTS.keys()):
            path = Path(root) / name
            if examined >= MAX_MANIFESTS:
                issues.append({'kind': 'manifest_limit'})
                return _discovery_result(manifests, issues, ecosystems, hosts, max_depth)
            examined += 1
            if path.is_symlink():
                issues.append({'path': str(path.relative_to(project)), 'kind': 'symlink_skipped'})
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                        issues.append({'path': str(path.relative_to(project)), 'kind': 'size_or_type_skipped'})
                        continue
                    text = stream.read(MAX_FILE_BYTES + 1)
                    if len(text) > MAX_FILE_BYTES:
                        issues.append({'path': str(path.relative_to(project)), 'kind': 'size_or_type_skipped'})
                        continue
                    urls = _manifest_urls(name, text.decode('utf-8'))
            except (OSError, ValueError, UnicodeError, RecursionError):
                issues.append({'path': str(path.relative_to(project)), 'kind': 'manifest_unavailable_or_invalid'})
                continue
            manifests.append(str(path.relative_to(project)))
            ecosystems.add(MANIFESTS[name])
            for url in urls:
                try:
                    parsed = urlsplit(url)
                    if parsed.username is not None or parsed.password is not None or parsed.query:
                        issues.append({'path': str(path.relative_to(project)), 'kind': 'authenticated_url_not_used'})
                        continue
                    hosts.add(domain(parsed.hostname))
                except ValueError:
                    issues.append({'path': str(path.relative_to(project)), 'kind': 'unsupported_registry_url'})
    return _discovery_result(manifests, issues, ecosystems, hosts, max_depth)


def _discovery_result(manifests, issues, ecosystems, hosts, max_depth):
    suggested = {host for name in ecosystems for host in PROFILES[name]}
    grouped = {}
    for issue in issues:
        key = (issue.get('path'), issue['kind'])
        if key not in grouped:
            grouped[key] = {**issue, 'count': 0}
        grouped[key]['count'] += 1
    return {'manifests': manifests, 'ecosystems': sorted(ecosystems), 'lock_domains': sorted(hosts),
            'suggested_domains': sorted(hosts | suggested), 'issues': list(grouped.values()),
            'complete_within_limits': not issues,
            'limits': {'max_depth': max_depth, 'max_manifests': MAX_MANIFESTS, 'max_file_bytes': MAX_FILE_BYTES}}


def inspect(project, value=None, base_domains=None):
    project = project_path(project)
    value = validate({} if value is None else value)
    rules = effective(value, project, base_domains)
    discovery = discover(project)
    return {'project': project, 'scope': 'kit command network', 'domains': rules,
            'inherited_projects': matching_projects(value, project), 'discovery': discovery,
            'missing_domains': [host for host in discovery['suggested_domains'] if access(host, rules) == 'missing'],
            'denied_domains': [host for host in discovery['suggested_domains'] if access(host, rules) == 'deny'],
            'automatic_grants': False, 'configuration_written': False}
