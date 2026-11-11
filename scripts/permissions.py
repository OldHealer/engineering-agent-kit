#!/usr/bin/env python3
"""Явный запуск Codex с ограниченным профилем; секретные файлы не читаются."""

import argparse
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

import configure_mcp
import autonomy
import file_access
import scanner_install
import network_policy
import local_settings
from kit import has_git_metadata
from codex_runtime import ScannerRuntimeError, approved_runtime, run_attached, scanner_runtime


KIT = Path(__file__).resolve().parents[1]
NAME = "kit-restricted"
MARKER = "# Managed by engineering-agent-kit permissions.py\n"
DOMAINS = local_settings.network_hosts()
SECRET_DIRS = (".ssh", ".aws", ".azure", ".kube", ".gnupg", ".config/gcloud", ".config/gh")
SECRET_FILES = (".git-credentials", ".netrc", ".npmrc", ".pypirc", ".docker/config.json",
                ".codex/auth.json", ".codex/config.toml", ".claude.json", ".gitconfig",
                ".config/git/config", ".config/git/credentials")
SHELL_FILES = (".profile", ".bash_profile", ".bash_login", ".bashrc", ".zshenv", ".zprofile", ".zshrc")
LEGACY_CODE_GLOBS = {"**/credentials*": "**/[c]redentials*", "**/*secret*": "**/*[s]ecret*"}
GLOBS = ("**/.env*", "**/*.env", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
         *LEGACY_CODE_GLOBS.values(), "**/auth.json", "**/.netrc", "**/.npmrc",
         "**/.git-credentials", "**/id_rsa*", "**/id_ed25519*")
ARTIFACT_GLOBS = (*GLOBS, "**/*[pP][aA][sS][sS][wW][oO][rR][dD]*",
                  "**/*[sS][eE][cC][rR][eE][tT]*", "**/*[cC][rR][eE][dD][eE][nN][tT][iI][aA][lL]*",
                  "**/*[пП][аА][рР][оО][лЛ]*")
WRITE_TOOLS = {"kit-bitbucket": "bb_add_inline_comment", "kit-github": "github_add_inline_comment",
               "kit-jira": "jira_comment_create"}
SCAN_BIN = KIT / "bin/permission-scan"


class ProfileError(ValueError):
    pass


def quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def toml_value(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{quote(key)}={toml_value(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def compact_denies(filesystem):
    denied = [Path(key) for key, value in filesystem.items() if value == "deny" and key.startswith("/")]
    for child in denied:
        if any(parent != child and child.is_relative_to(parent) for parent in denied):
            filesystem.pop(str(child))
    return filesystem


def codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).absolute()


def ai_box():
    return local_settings.ai_box()


def denied_paths(home=None, kit=None):
    home = home or Path.home()
    kit = kit or KIT
    paths = [home / item for item in (*SECRET_DIRS, *SECRET_FILES, *SHELL_FILES)]
    paths.extend([home / ".codex/sessions", home / ".codex/log", home / ".codex/logs",
                  kit / ".engineering-kit/connections.env"])
    if os.environ.get("CODEX_HOME"):
        paths.extend(codex_home() / item for item in ("auth.json", "config.toml", "sessions", "log", "logs"))
    result = set()
    for item in paths:
        result.add(str(item.absolute()))
        result.add(str(item.resolve()))
    return sorted(result)


def render_profile(home=None, kit=None, dependencies=None):
    home, kit = home or Path.home(), kit or KIT
    dependencies = dependencies or ai_box()
    lines = [MARKER.rstrip(), 'default_permissions = "kit-restricted"', 'approval_policy = "on-request"',
             'approvals_reviewer = "user"', 'web_search = "disabled"',
             '', '[features]', 'network_proxy = true',
             '', f'[permissions.{NAME}]', 'extends = ":workspace"',
             'description = "Project writes, explicit network proxy, credential files denied"',
             '', f'[permissions.{NAME}.filesystem]', 'glob_scan_max_depth = 8',
             '":slash_tmp" = "read"', '":tmpdir" = "read"']
    for item in denied_paths(home, kit):
        # В 0.166.0 bubblewrap не может маскировать отсутствующий каталог.
        # Пересобираем точные запреты при каждом запуске, не читая содержимое.
        if Path(item).exists() and not Path(item).is_symlink():
            lines.append(f'{quote(item)} = "deny"')
    lines += ['', f'[permissions.{NAME}.filesystem.":workspace_roots"]']
    lines += [f'{quote(pattern)} = "deny"' for pattern in GLOBS]
    for item in sorted({str(kit.resolve()), str(dependencies.resolve())}):
        lines += ['', f'[permissions.{NAME}.filesystem.{quote(item)}]', '"." = "read"']
        lines += [f'{quote(pattern)} = "deny"' for pattern in GLOBS]
    lines += ['', f'[permissions.{NAME}.network]', 'enabled = true', 'enable_socks5 = true',
              'enable_socks5_udp = false', 'allow_upstream_proxy = false',
              '# Подключения ограничены точными списками доменов; локальные параметры задаются отдельно.',
              'allow_local_binding = true',
              '', f'[permissions.{NAME}.network.domains]']
    lines += [f'{quote(domain)} = "allow"' for domain in DOMAINS]
    tool_paths = {str(Path(shutil.which(tool)).parent) for tool in ("node", "npm", "python3", "git", "ruff", "rtk")
                  if shutil.which(tool)}
    tool_paths.update(os.defpath.split(os.pathsep))
    lines += ['', '[shell_environment_policy]', 'inherit = "none"', 'ignore_default_excludes = false',
              'experimental_use_profile = false', '', '[shell_environment_policy.set]',
              f'PATH = {quote(os.pathsep.join(sorted(tool_paths)))}', f'HOME = {quote(home)}', 'LANG = "C.UTF-8"',
              'GIT_CONFIG_GLOBAL = "/dev/null"', 'GIT_CONFIG_NOSYSTEM = "1"',
              '', '[shell_environment_policy.filters]', '"*_TOKEN" = "exclude"', '"*_PAT" = "exclude"',
              '"*_SECRET*" = "exclude"', '"*_PASSWORD" = "exclude"', '"*_KEY" = "exclude"']
    # config/batchWrite проверяет профиль отдельно от наложений: каждому MCP нужен транспорт.
    for service in ("bitbucket", "github", "jira", "confluence"):
        server = configure_mcp.server_config(service, dependencies, shutil.which("node") or "node")
        lines += ['', f'[mcp_servers.{quote("kit-" + service)}]',
                  f'command = {quote(server["command"])}', f'args = {toml_value(server["args"])}']
    for server, tool in WRITE_TOOLS.items():
        lines += ['', f'[mcp_servers.{quote(server)}.tools.{quote(tool)}]', 'approval_mode = "prompt"']
    for server in ("kit-bitbucket", "kit-github", "kit-jira", "kit-confluence"):
        hosts = ','.join(local_settings.mcp_hosts(server.removeprefix('kit-')))
        lines += ['', f'[mcp_servers.{quote(server)}.env]', f'KIT_MCP_ALLOWED_HOSTS = {quote(hosts)}']
    result = "\n".join(lines) + "\n"
    tomllib.loads(result)
    return result


def load_structure(path):
    # Читается только TOML конфигурации, не auth.json и не файлы подключений.
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def refresh_file_rules(original):
    """Отличить управляемые маски kit от внешних deny, сохранив их множество совпадений."""
    if not original.startswith(MARKER):
        raise ProfileError("Профиль не принадлежит kit; файловые правила не изменены")
    scopes = {":workspace_roots", str(KIT.resolve()), str(ai_box().resolve())}
    current = None
    result = []
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            table = tomllib.loads(line).get('permissions', {}).get(NAME, {}).get('filesystem', {})
            current = next(iter(table)) if len(table) == 1 and next(iter(table)) in scopes else None
        elif current is not None:
            entry = tomllib.loads(line)
            if len(entry) == 1:
                key, value = next(iter(entry.items()))
                if key in LEGACY_CODE_GLOBS and value == 'deny':
                    head, separator, tail = line.partition('=')
                    line = head[:len(head) - len(head.lstrip())] + quote(LEGACY_CODE_GLOBS[key]) + head[len(head.rstrip()):] + separator + tail
        result.append(line)
    updated = ''.join(result)
    expected = tomllib.loads(original)
    filesystem = expected.get('permissions', {}).get(NAME, {}).get('filesystem', {})
    for root in scopes:
        for old, new in LEGACY_CODE_GLOBS.items():
            if isinstance(filesystem.get(root), dict) and filesystem[root].get(old) == 'deny':
                filesystem[root][new] = filesystem[root].pop(old)
    if tomllib.loads(updated) != expected:
        raise ProfileError("Не удалось сохранить пользовательские файловые правила; запись остановлена")
    return updated


def repair_profile_transport(path, base):
    original = path.read_text()
    require_marker = original.startswith(MARKER) and not path.is_symlink()
    if not require_marker:
        raise ProfileError("Миграция разрешена только для обычного файла профиля kit с его служебной отметкой")
    profile = tomllib.loads(original)
    additions = {}
    node = shutil.which("node")
    for service in ("bitbucket", "github", "jira", "confluence"):
        name = "kit-" + service
        entry = profile.get("mcp_servers", {}).get(name)
        if entry is None or "command" in entry or "url" in entry:
            continue
        registered = base.get("mcp_servers", {}).get(name, {})
        args = registered.get("args", [])
        if (not node or registered.get("command") != node or not isinstance(args, list) or len(args) != 4
                or args[:3] != [str(KIT / "scripts/mcp-server.mjs"), service, "--ai-box"]
                or not isinstance(args[3], str) or not Path(args[3]).is_absolute()):
            raise ProfileError(f"Не подтверждён штатный транспорт {name}; настройки сохранены, значения скрыты")
        # Копируем только проверенные пути запуска, без env и пользовательских данных авторизации.
        additions[name] = f'command = {quote(node)}\nargs = {toml_value(args)}\n'
    if not additions:
        return []
    updated = original
    for name, transport in additions.items():
        header = re.compile(r'^\[mcp_servers\.(?:"' + re.escape(name) + r'"|' + re.escape(name) + r')(\]|\.)', re.M)
        matches = list(header.finditer(updated))
        if not matches:
            raise ProfileError("Не найден заголовок MCP в профиле kit; файл сохранён без изменений")
        explicit = next((match for match in matches if match.group(1) == "]"), None)
        if explicit:
            end = updated.find("\n", explicit.end())
            if end < 0:
                updated += "\n"
                end = len(updated)
            else:
                end += 1
            updated = updated[:end] + transport + updated[end:]
        else:
            start = matches[0].start()
            updated = updated[:start] + f'[mcp_servers.{quote(name)}]\n' + transport + '\n' + updated[start:]
    tomllib.loads(updated)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".kit-profile-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(updated)
    try:
        if path.is_symlink() or path.read_text() != original:
            raise ProfileError("Профиль изменился во время миграции; повторите запуск, перезаписи не было")
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return list(additions)


def preflight(project, directory=None):
    directory = directory or codex_home()
    paths = [Path("/etc/codex/config.toml"), directory / "config.toml"]
    current = project
    while True:
        paths.append(current / ".codex/config.toml")
        if (current / ".git").exists() or current == current.parent:
            break
        current = current.parent
    for path in paths:
        data = load_structure(path)
        if NAME in data.get("permissions", {}):
            raise ProfileError(f"В {path} уже определены права с именем {NAME}; согласуйте конфликт отдельно")
        if path not in paths[:2] and any(key in data for key in
                ("default_permissions", "permissions", "approval_policy", "approvals_reviewer", "shell_environment_policy", "mcp_servers")):
            raise ProfileError(f"В {path} есть проектные настройки доступа или MCP; согласуйте их отдельно")
        if any(key in data for key in ("sandbox_mode", "sandbox_workspace_write")):
            raise ProfileError(f"Обнаружены старые sandbox-настройки в {path}; согласуйте миграцию отдельно")
        policy = data.get("shell_environment_policy", {})
        if any(key in policy for key in ("exclude", "include_only")):
            raise ProfileError(f"Старые фильтры окружения в {path}; согласуйте миграцию отдельно")
        if set(policy.get("set", {})) - {"PATH", "HOME", "LANG", "TERM", "TMPDIR"}:
            raise ProfileError(f"В {path} заданы дополнительные shell-переменные; запуск остановлен без вывода значений")


def repository_roots(project):
    candidates = [project] if has_git_metadata(project) else [
        item for item in project.iterdir() if item.is_dir() and has_git_metadata(item)
    ]
    return sorted({item.resolve() for item in candidates})


def git_directories(project):
    result = set()
    for repo in repository_roots(project):
        for option in ("--absolute-git-dir", "--git-common-dir"):
            command = subprocess.run(["git", "-C", str(repo), "rev-parse", option],
                                     capture_output=True, text=True, check=True)
            value = Path(command.stdout.strip())
            result.add((value if value.is_absolute() else repo / value).resolve())
    return sorted(result)


def isolated_worktree_containers(project):
    containers = set()
    managed = project / ".engineering-kit/workspaces"
    originals = repository_roots(project)
    for repo in originals:
        if not (repo / ".git").is_dir():
            continue
        result = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"],
                                capture_output=True, check=True,
                                env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        for record in result.stdout.split(b"\0"):
            if not record.startswith(b"worktree "):
                continue
            worktree = Path(os.path.abspath(os.fsdecode(record[len(b"worktree "):])))
            if not worktree.is_relative_to(project) or worktree.is_relative_to(managed):
                continue
            parent = project
            for part in worktree.relative_to(project).parts:
                parent = parent / part
                if part == ".worktrees":
                    if parent.is_dir() and parent.resolve() == parent and not any(root.is_relative_to(parent) for root in originals):
                        containers.add(str(parent))
                    break
    return sorted(containers)


def unreadable_directories(roots, max_depth=8, excluded=()):
    denied = set()
    excluded = [Path(path) for path in excluded]

    def inaccessible(error):
        if error.errno in {errno.EACCES, errno.EPERM} and error.filename:
            denied.add(str(Path(error.filename).absolute()))
        elif error.errno not in {errno.ENOENT, errno.ENOTDIR}:
            raise ProfileError("Не удалось проверить каталоги для безопасного поиска; содержимое файлов не читалось")

    for scope in sorted({Path(root).resolve() for root in roots}):
        if any(scope.is_relative_to(path) for path in excluded):
            continue
        if not scope.is_dir():
            continue
        # Глубина совпадает с rg --max-depth: прямые дети корня находятся на уровне 1.
        # Не пропускаем node_modules/.git: штатный scanner Codex также использует --no-ignore.
        for root, directories, _ in os.walk(scope, followlinks=False, onerror=inaccessible):
            directories[:] = [name for name in directories
                              if not any((Path(root) / name).is_relative_to(path) for path in excluded)]
            if len(Path(root).relative_to(scope).parts) >= max_depth - 1:
                directories[:] = []
    return sorted(denied)


def scan_overrides(project, filesystem, isolated=None, code_modules=()):
    scopes = [project] + [Path(key) for key, value in filesystem.items()
                         if key.startswith("/") and isinstance(value, dict)
                         and any(access == "deny" and any(char in pattern for char in "*?[")
                                 for pattern, access in value.items())]
    isolated = isolated if isolated is not None else isolated_worktree_containers(project)
    denied = sorted(set(isolated) | set(unreadable_directories(scopes, filesystem.get("glob_scan_max_depth", 8), isolated)))
    if not denied and not code_modules:
        return []
    system_scanner = scanner_install.installed()
    real_rg = str(scanner_install.DESTINATION) if system_scanner else shutil.which("rg", path=os.defpath)
    if not real_rg or not (SCAN_BIN / "rg").is_file():
        raise ProfileError("Для безопасного запуска с закрытыми каталогами нужны rg и адаптер kit; зависимости автоматически не устанавливаются")
    if any(any(char in path for char in "*?[]{}\\") for path in denied):
        raise ProfileError("Закрытый каталог содержит метасимволы glob; безопасный точный deny не гарантирован, запуск остановлен")
    for path in denied:
        filesystem[path] = "deny"
    policy = tomllib.loads(render_profile())["shell_environment_policy"]["set"]
    # После broad grant Codex отвергает пользовательский rg; root-owned fallback остаётся допустимым.
    fallback = [str(scanner_install.DIRECTORY)] if system_scanner else []
    path = os.pathsep.join([str(SCAN_BIN), *fallback, policy["PATH"]])
    arguments = []
    policy = {"PATH": path, "KIT_SCAN_DENIED_DIRS": json.dumps(denied), "KIT_SCAN_RG": real_rg}
    if code_modules:
        policy["KIT_SCAN_CODE_MODULES"] = json.dumps(code_modules)
        policy["KIT_SCAN_MANAGED_GLOBS"] = json.dumps({"root": str(project), "patterns": GLOBS})
    for key, value in policy.items():
        arguments += ["-c", f"shell_environment_policy.set.{key}={quote(value)}"]
    return arguments


def artifact_sensitive_name(name):
    lowered = name.lower()
    return bool(re.search(r"password|secret|credential|парол", name, re.I)
                or lowered.startswith((".env", "id_rsa", "id_ed25519"))
                or lowered.endswith((".env", ".pem", ".key", ".p12", ".pfx"))
                or lowered in {"auth.json", ".netrc", ".npmrc", ".git-credentials"})


def artifact_denies(roots):
    denied = set()
    for scope in roots:
        for root, directories, files in os.walk(scope, followlinks=False):
            for name in files + directories[:]:
                candidate = Path(root) / name
                target = candidate.resolve() if candidate.is_symlink() else candidate
                sensitive = artifact_sensitive_name(name) or (candidate.is_symlink()
                            and any(artifact_sensitive_name(part) for part in target.parts))
                if sensitive:
                    # Запрет на сам writable-симлинк не поддерживается bubblewrap; закрываем цель.
                    denied.add(str(target))
                    if name in directories:
                        directories.remove(name)
    return denied


def project_overrides(project, allow_kit_edit=False, extra_denied=(), protected_paths=(), artifact_roots=(),
                      code_modules=(), installed_filesystem=None, network_settings=None, installed_domains=None):
    project = project.resolve()
    isolated = isolated_worktree_containers(project)
    arguments = []
    filesystem = tomllib.loads(render_profile())["permissions"][NAME]["filesystem"]
    artifact_roots = autonomy.validate_roots(artifact_roots, codex_home())
    for root in artifact_roots:
        filesystem[root] = {".": "write", **dict.fromkeys(ARTIFACT_GLOBS, "deny"),
                            ".git": "read", ".agents": "read", ".codex": "read", ".aws": "read"}
    for directory in protected_paths:
        filesystem[str(directory.resolve())] = {".": "read", **dict.fromkeys(GLOBS, "deny")}

    def setting(key, value):
        arguments.extend(["-c", f"{key}={quote(value)}"])

    setting('shell_environment_policy.set.TMPDIR', project / ".engineering-kit/tmp")
    setting('default_permissions', NAME)
    setting('approval_policy', 'on-request')
    setting('approvals_reviewer', 'user')
    arguments += ["-c", "features.network_proxy=true"]
    filesystem[str(project / ".engineering-kit/tmp")] = 'write'
    # Даже если сессия запущена из worktree, служебная Git-база остаётся защищённой.
    for directory in git_directories(project):
        filesystem[str(directory)] = 'read'
    if project == KIT.resolve():
        if not allow_kit_edit:
            raise ProfileError("Для разработки kit укажите --kit-development явно")
        filesystem[str(KIT.resolve())]["."] = 'write'
    # Для существующих секретных симлинков запрещаем также их реальные цели.
    for root, directories, files in os.walk(project, followlinks=False):
        relative = Path(root).relative_to(project)
        git_pointer = Path(root) / ".git"
        if git_pointer.exists():
            filesystem[str(git_pointer)] = 'read'
        entries = files + directories[:]
        directories[:] = [name for name in directories
                          if name not in {".git", "node_modules", ".venv", "venv", "__pycache__"}
                          and not any((Path(root) / name).is_relative_to(Path(path)) for path in isolated)
                          and len(relative.parts) < 8]
        for name in entries:
            candidate = Path(root) / name
            sensitive = (name.startswith(".env") or re.search(r"secret|credentials", name, re.I)
                         or name.endswith((".env", ".pem", ".key", ".p12", ".pfx"))
                         or name in {"auth.json", ".netrc", ".npmrc", ".git-credentials"}
                         or name.startswith(("id_rsa", "id_ed25519")))
            if sensitive and candidate.is_symlink():
                filesystem[str(candidate.resolve())] = 'deny'
    # Для vault проверяем имена на всей глубине, независимо от лимита glob scanner.
    for denied in artifact_denies(artifact_roots):
        filesystem[denied] = 'deny'
    for denied in extra_denied:
        filesystem[str(denied.resolve())] = 'deny'
    # Пользовательские deny не теряются при пересборке таблицы launcher-ом.
    for root, rule in (installed_filesystem or {}).items():
        if rule == 'deny':
            filesystem[root] = 'deny'
        elif isinstance(rule, dict):
            denied = {key: access for key, access in rule.items() if access == 'deny'}
            if denied and filesystem.get(root) != 'deny':
                current = filesystem.get(root, {})
                filesystem[root] = {**(current if isinstance(current, dict) else {'.': current}), **denied}
    exact_denies = []
    for root, rule in filesystem.items():
        base = project if root == ':workspace_roots' else Path(root) if root.startswith('/') else None
        if root == ':root':
            base = Path('/')
        if base is None:
            continue
        if rule == 'deny':
            exact_denies.append(base)
        elif isinstance(rule, dict):
            exact_denies += [base / key for key, access in rule.items()
                             if access == 'deny' and not any(char in key for char in '*?[]{}')]
    approved = []
    for module in code_modules:
        path = Path(module['path'])
        if (not path.is_relative_to(project) or any(path.is_relative_to(Path(root)) for root in artifact_roots)
                or any(path.is_relative_to(root) for root in exact_denies)
                or not file_access.unchanged(module, provenance=True)):
            continue
        filesystem[str(path)] = 'read'
        approved.append(module)
    arguments += scan_overrides(project, filesystem, isolated, approved)
    # CLI -c разбивает ключ по точкам, поэтому пути передаём внутри TOML-таблицы.
    arguments += ["-c", f"permissions.{NAME}.filesystem={toml_value(compact_denies(filesystem))}"]
    if network_settings is not None:
        domains = network_policy.effective(network_settings, project,
                    installed_domains if installed_domains is not None else dict.fromkeys(DOMAINS, 'allow'))
        arguments += ['-c', f'permissions.{NAME}.network.domains={toml_value(domains)}']
    return arguments


def render_rules(projects=()):
    result = (KIT / "permissions/sensitive.rules").read_text()
    return MARKER + result


def install(directory, projects=(), include_rules=False, dependencies=None):
    files = {directory / f"{NAME}.config.toml": render_profile(dependencies=dependencies)}
    if include_rules:
        files[directory / "rules/kit-sensitive.rules"] = render_rules(projects)
    for path, content in files.items():
        if path.exists() and path.read_text() != content:
            raise ProfileError(f"Файл уже существует с другим содержимым: {path}; автоматической перезаписи нет")
    for path, content in files.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return {"profile": NAME, "files": [str(path) for path in files], "activated": False,
            "rules_scope": "all future Codex sessions for this user" if include_rules else "not installed"}


def rollback(directory, confirm=False):
    paths = [directory / f"{NAME}.config.toml", directory / "rules/kit-sensitive.rules"]
    existing = [path for path in paths if path.exists()]
    for path in existing:
        if not path.read_text().startswith(MARKER):
            raise ProfileError(f"Файл не помечен как принадлежащий kit: {path}")
        if path.with_suffix(path.suffix + ".disabled").exists():
            raise ProfileError("Путь .disabled уже занят; перезапись запрещена")
    if confirm:
        for path in existing:
            path.rename(path.with_suffix(path.suffix + ".disabled"))
    return {"files": [str(path) for path in existing], "disabled": confirm,
            "operation": "rename to .disabled; no deletion"}


def launch_environment(source, overrides=()):
    environment = {key: value for key, value in source.items()
                   if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "COLORTERM", "DISPLAY", "WAYLAND_DISPLAY", "CODEX_HOME"}}
    # Часть версий Codex запускает scanner до применения shell_environment_policy.
    # В обе среды передаётся одна несекретная политика; deny остаются в filesystem.
    for index in range(0, len(overrides), 2):
        key, _, value = overrides[index + 1].partition("=")
        prefix = "shell_environment_policy.set."
        if key.startswith(prefix) and key[len(prefix):] in {"PATH", "KIT_SCAN_DENIED_DIRS", "KIT_SCAN_RG", "KIT_SCAN_CODE_MODULES", "KIT_SCAN_MANAGED_GLOBS"}:
            environment[key[len(prefix):]] = tomllib.loads("value=" + value)["value"]
    return environment


def git_session_overrides(project, base, autonomy_settings=None):
    node = shutil.which("node")
    if not node:
        raise ProfileError("Node.js не найден; выполните kit install с готовыми MCP-зависимостями")
    server = base.get("mcp_servers", {}).get("kit-git")
    if not server or server.get("command") != node or server.get("args", [])[:2] != [str(KIT / "scripts/mcp-server.mjs"), "git"]:
        raise ProfileError("Сначала выполните kit install: Git MCP не зарегистрирован или его запись отличается")
    scoped = dict(server)
    scoped.update(enabled=True, default_tools_approval_mode="writes", env={
        "KIT_GIT_PROJECT": str(project), "KIT_MCP_ALLOWED_HOSTS": ",".join(local_settings.mcp_hosts('git')),
        "KIT_PYTHON": sys.executable})
    scoped["tools"] = {f"kit_git_{action}": {"approval_mode": "prompt"}
                       for action in ("init", "configure", "prepare", "commit", "push", "cleanup")}
    if autonomy_settings:
        scoped["tools"]["kit_git_prepare"]["approval_mode"] = "approve"
        if autonomy_settings["local_commit"] == "auto":
            scoped["tools"]["kit_git_commit"]["approval_mode"] = "approve"
    overrides = ["-c", f"mcp_servers.kit-git={toml_value(scoped)}"]
    instructions = base.get("developer_instructions", "")
    if not isinstance(instructions, str):
        raise ProfileError("Неподдерживаемый формат developer_instructions; значения скрыты")
    instructions += (f"\nEngineering Agent Kit: прочитай {KIT / 'AGENTS.md'} и применимые инструкции проекта. "
                     "Роли выполняй последовательно. Для координации задач по PRD прочитай skill feature-hub; "
                     "для планирования/реализации одной задачи — plan-feature; для QA — verify-feature; "
                     "для PR — review-pr. Проект ограничен текущим каталогом. "
                     "При доступном kit_git_discover используй kit_git_* для init, подготовки worktree, коммита, push и очистки; "
                     "не передавай их в обычный shell. Прочитай режим проекта через kit_git_discover: "
                     "current использует исходную папку выбранной ветки без создания worktree; "
                     "для worktree сначала согласуй репозитории и базовые ветки (обычно main). "
                     "Режим current и ветки сохраняй через kit_git_configure только после согласия. "
                     "Переключение ветки или включение имеющихся изменений требует отдельного согласия. "
                     "Для режима commit следуй общей политике автономии, если она настроена; иначе согласуй состав и текст. "
                     "push выполняй только по отдельному поручению. Создание PR запрещено. "
                     "Очистка требует согласования точных путей и проверки отсутствия используемых процессов. "
                     "Не читай секретные файлы и не ослабляй профиль доступа.")
    overrides += ["-c", "developer_instructions=" + quote(instructions)]
    if autonomy_settings:
        policy = "\n" + (KIT / "permissions/autonomy.md").read_text()
        policy += f"\nРежим локального commit: {autonomy_settings['local_commit']}; восстановления зависимостей по lock: {autonomy_settings['locked_dependencies']}.\n"
        policy += "Режим auto разрешает соответствующее действие в рамках порученной задачи без нового вопроса. Режим confirm требует согласования. Push, новые зависимости и сервисы требуют подтверждения.\n"
        overrides[-1] = "developer_instructions=" + quote(instructions + policy)
    return overrides


def session_command(executable, project, overrides, resume=False, session_id=None, last=False, isolated_backend=False):
    if session_id is not None and last:
        raise ProfileError("Для resume выберите ID сессии или --last")
    command = [str(executable), '--profile', NAME, '--cd', str(project), *overrides]
    if resume:
        command += ['resume']
        if session_id is not None:
            command += [session_id]
        elif last:
            command += ['--last']
    if isolated_backend:
        # Общий daemon может быть запущен старым binary без согласованного исправления.
        command += ['--no-daemon']
    return command


def run(args):
    project = args.project.resolve()
    preflight(project)
    profile = codex_home() / f"{NAME}.config.toml"
    if not profile.is_file() or not profile.read_text().startswith(MARKER):
        raise ProfileError("Сначала установите профиль командой install")
    base = load_structure(codex_home() / "config.toml")
    repair_profile_transport(profile, base)
    installed_profile = tomllib.loads(profile.read_text())["permissions"][NAME]
    installed = installed_profile["filesystem"]
    protected = [Path(key) for key, value in installed.items() if key.startswith('/')
                 and (value == 'read' or isinstance(value, dict) and value.get('.') == 'read')]
    policy = autonomy.load(codex_home())
    selected_native = None
    if policy and policy.get('runtime'):
        selected_native = approved_runtime(policy['runtime'], codex_home())
        runtime_root = selected_native.parent.parent
        if project.is_relative_to(runtime_root) or runtime_root.is_relative_to(project):
            raise ProfileError("Проект пересекается с согласованным backend; выберите папку исходного проекта")
        protected.append(runtime_root)
    overrides = project_overrides(project, args.kit_development, protected_paths=protected,
                                  artifact_roots=policy["artifact_roots"] if policy else (),
                                  code_modules=policy.get("code_modules", []) if policy else (),
                                  installed_filesystem=installed,
                                  network_settings=policy.get('network') if policy else None,
                                  installed_domains=installed_profile.get('network', {}).get('domains', {}))
    overrides += git_session_overrides(project, base, autonomy_settings=policy)
    if selected_native is not None:
        overrides += ['-c', 'check_for_update_on_startup=false']
    network = network_policy.inspect(project, policy.get('network') if policy else None,
                                     installed_profile.get('network', {}).get('domains', {}))
    if network['missing_domains'] or network['denied_domains'] or network['discovery']['issues']:
        summary = {key: network[key] for key in ('missing_domains', 'denied_domains')}
        summary['discovery_issues'] = sum(item['count'] for item in network['discovery']['issues'])
        print('Kit network: ' + json.dumps(summary)
              + '\nПодробности: kit network --project ' + shlex.quote(str(project)) + '; доступ автоматически не выдаётся.', file=sys.stderr)
    temporary = project / ".engineering-kit/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    # Не переносим в новую сессию секреты из терминала или цепочку внешних прокси.
    environment = launch_environment(os.environ, overrides)
    native = str(selected_native) if selected_native is not None else shutil.which("codex", path=environment.get("PATH"))
    if not native:
        raise ProfileError("Codex не найден в PATH")
    try:
        with scanner_runtime(native, SCAN_BIN / "rg", environment, codex_home() / "kit-runtime", project) as runtime:
            overrides += runtime_overrides(overrides, runtime)
            command = session_command(runtime.executable, project, overrides,
                        resume=getattr(args, 'resume_session', False), session_id=getattr(args, 'session_id', None),
                        last=getattr(args, 'last', False), isolated_backend=selected_native is not None)
            result = run_attached(command, environment, runtime.pass_fds, runtime.directory)
            if selected_native is not None:
                print('Продолжить с выбранным backend kit: cd ' + shlex.quote(str(project)) + ' && kit resume'
                      + (' --kit-development' if args.kit_development else ''))
            return result
    except ScannerRuntimeError as error:
        raise ProfileError(str(error)) from error


def runtime_overrides(arguments, runtime):
    if runtime.directory is None:
        return []
    key = f"permissions.{NAME}.filesystem="
    tables = [value[len(key):] for flag, value in zip(arguments[::2], arguments[1::2])
              if flag == "-c" and value.startswith(key)]
    if not tables:
        raise ProfileError("Не найдена таблица файловых прав для защиты runtime")
    filesystem = tomllib.loads("value=" + tables[-1])["value"]
    filesystem[str(runtime.directory.parent)] = "read"
    return ["-c", key + toml_value(filesystem), "-c", "check_for_update_on_startup=false"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("install", help="Установить отдельный профиль, не активируя его")
    setup.add_argument("--codex-dir", type=Path, default=codex_home())
    setup.add_argument("--project", type=Path, action="append", default=[])
    setup.add_argument("--ai-box", type=Path, default=ai_box(), help="Корень используемых MCP-зависимостей, защищаемый от записи")
    setup.add_argument("--with-command-rules", action="store_true", help="Добавить глобальные ограничивающие .rules для будущих сессий")
    undo = commands.add_parser("rollback", help="Предпросмотр обратимого отключения файлов kit")
    undo.add_argument("--codex-dir", type=Path, default=codex_home())
    undo.add_argument("--confirm", action="store_true")
    launch = commands.add_parser("run", help="Явно запустить новую ограниченную сессию")
    launch.add_argument("--project", type=Path, default=Path.cwd())
    launch.add_argument("--kit-development", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "install":
            projects = [path.resolve() for path in args.project]
            for project in projects:
                preflight(project, args.codex_dir)
            result = install(args.codex_dir, projects, args.with_command_rules, args.ai_box.resolve())
        elif args.action == "rollback":
            result = rollback(args.codex_dir, args.confirm)
        else:
            return run(args)
    except ProfileError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.CalledProcessError):
        # Чужой TOML или вывод Git могут содержать секреты; не показываем их.
        print("Операция остановлена: проверьте конфликты старых настроек, существующие файлы и права. Значения конфигурации скрыты.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
