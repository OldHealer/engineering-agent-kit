"""Предпросмотр и установка пользовательской политики без вывода чужой конфигурации."""

import hashlib
import json
from pathlib import Path
import re
import tempfile
import tomllib

import file_access
import network_policy
import codex_runtime


KIT = Path(__file__).resolve().parents[1]
SETTINGS = "kit-autonomy.json"
MARKER = "# Managed by engineering-agent-kit autonomy.py\n"
BEGIN = "<!-- engineering-agent-kit:autonomy:start -->"
END = "<!-- engineering-agent-kit:autonomy:end -->"
MODES = {"confirm", "auto"}


class PolicyError(ValueError):
    pass


def load(directory):
    path = Path(directory) / SETTINGS
    if not path.exists():
        return None
    if path.is_symlink():
        raise PolicyError("Настройки политики не должны быть симлинком")
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or set(data) - {"version", "local_commit", "locked_dependencies", "artifact_roots", "code_modules", "network", "runtime"}
            or not {"version", "local_commit", "locked_dependencies", "artifact_roots"}.issubset(data)
            or data["version"] != 1 or not isinstance(data["local_commit"], str) or data["local_commit"] not in MODES
            or not isinstance(data["locked_dependencies"], str) or data["locked_dependencies"] not in MODES or not isinstance(data["artifact_roots"], list)
            or not all(isinstance(value, str) for value in data["artifact_roots"])):
        raise PolicyError("Неизвестный формат общей политики; значения скрыты")
    validate_roots(data["artifact_roots"], Path(directory))
    if not isinstance(data.get("code_modules", []), list) or not all(file_access.valid_snapshot(item) for item in data.get("code_modules", [])):
        raise PolicyError("Некорректные разрешения программных модулей")
    if 'network' in data:
        network_policy.validate(data['network'])
    if 'runtime' in data and not codex_runtime.valid_runtime_snapshot(data['runtime']):
        raise PolicyError("Некорректный снимок согласованного backend")
    return data


def validate_roots(values, directory):
    protected = [directory.resolve(), KIT] + [Path.home() / name for name in
                 (".codex", ".ssh", ".aws", ".azure", ".config", ".gnupg", ".kube", ".docker", ".claude", ".local/bin")]
    roots = []
    for value in values:
        path = Path(value).expanduser().absolute()
        if (not path.is_dir() or path.resolve() != path
                or any(path.is_relative_to(item) or item.is_relative_to(path) for item in protected)):
            raise PolicyError("Корень артефактов недоступен, содержит симлинк или пересекает защищённые каталоги")
        roots.append(str(path))
    return sorted(set(roots))


def update_scalars(original, settings):
    before = tomllib.loads(original)
    updated = original
    prefix = []
    for key, value in settings.items():
        if key not in before:
            prefix.append(f"{key} = {json.dumps(value)}\n")
        elif before[key] != value:
            pattern = re.compile(r"(?m)^(?:" + re.escape(key) + r'|"' + re.escape(key) + r'")\s*=\s*([^\n]*)$')
            matches = list(pattern.finditer(updated))
            if len(matches) != 1 or not re.fullmatch(r'''(?:"[^"\n]*"|'[^'\n]*')\s*(?:#.*)?''', matches[0][1]):
                raise PolicyError("Настройка подтверждений имеет неоднозначный формат; нужна отдельная проверка, значения скрыты")
            match = matches[0]
            updated = updated[:match.start()] + f"{key} = {json.dumps(value)}" + updated[match.end():]
    updated = "".join(prefix) + updated
    if tomllib.loads(updated) != {**before, **settings}:
        raise PolicyError("Проверка сохранности TOML не пройдена; изменения не записаны")
    return updated


def rules(settings):
    base = (KIT / "permissions/autonomy.rules").read_text()
    result = MARKER + base
    # RTK proxy выполняет ту же команду; её ограничения должны сохраняться под обёрткой.
    for line in base.splitlines():
        prefix = 'prefix_rule(pattern = ['
        if line.startswith(prefix):
            result += line.replace(prefix, 'prefix_rule(pattern = ["rtk", "proxy", ', 1) + '\n'
    patterns = [["npm", ["install", "ci", "add", "uninstall", "update", "prune"]],
                ["pnpm", ["install", "add", "remove", "update"]],
                ["yarn", ["install", "add", "remove", "upgrade"]],
                [["pip", "pip3"], ["install", "uninstall"]],
                [["python", "python3"], "-m", "pip", ["install", "uninstall"]],
                ["uv", ["add", "remove", "sync", "pip"]]]
    if settings["locked_dependencies"] == "auto":
        patterns = [["npm", ["install", "add", "uninstall", "update", "prune"]],
                    ["pnpm", ["add", "remove", "update"]], ["yarn", ["add", "remove", "upgrade"]],
                    [["pip", "pip3"], ["install", "uninstall"]],
                    [["python", "python3"], "-m", "pip", ["install", "uninstall"]],
                    ["uv", ["add", "remove", "pip"]]]
    if settings["local_commit"] == "confirm":
        patterns.append([["git", "/usr/bin/git"], "commit"])
    for pattern in patterns:
        for command in (pattern, ["rtk", "proxy", *pattern]):
            result += f'prefix_rule(pattern = {json.dumps(command)}, decision = "prompt", justification = "Сначала согласуйте изменение зависимостей или локальный коммит")\n'
    return result


def instructions(original, settings):
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise PolicyError("Неоднозначный служебный блок AGENTS.md; файл сохранён")
    details = ["Локальный commit: " + ("самостоятельно после проверок и предпросмотра" if settings["local_commit"] == "auto" else "после согласования состава и текста"),
               "Восстановление зависимостей по lock-файлу: " + ("самостоятельно, без изменения manifest/lock и без новых пакетов" if settings["locked_dependencies"] == "auto" else "после подтверждения"),
               "Новые зависимости и запуск/остановка сервисов: после подтверждения."]
    if settings["locked_dependencies"] == "auto":
        details.append("Для восстановления используй npm ci, uv sync --locked, pnpm install --frozen-lockfile или соответствующий версии Yarn режим immutable/frozen. "
                       "Сначала проверь наличие lock-файла, фактические scripts и изоляцию окружения, config/cache и целевой папки зависимостей. "
                       "Если lock отсутствует, его нужно изменить, установка затрагивает глобальное окружение или scripts меняют рабочие данные/сервисы — запроси согласование. "
                       "Справку этих команд запускай обычно внутри sandbox, без require_escalated.")
    block = BEGIN + "\n\n" + (KIT / "permissions/autonomy.md").read_text().rstrip() + "\n\n"
    block += "\n".join("- " + line for line in details) + "\n\n" + END
    if BEGIN in original:
        start, finish = original.index(BEGIN), original.index(END) + len(END)
        if finish <= start:
            raise PolicyError("Порядок служебных отметок AGENTS.md нарушен")
        return original[:start] + block + original[finish:]
    separator = "\n" if original.endswith("\n") else "\n\n" if original else ""
    return original + separator + block + "\n"


def plan(directory, local_commit=None, locked_dependencies=None, artifact_roots=None, code_modules=None,
         network_project=None, network_allow=None, network_deny=None, network_remove=None, network_profiles=None,
         runtime_executable=None, clear_runtime=False):
    directory = Path(directory).expanduser().absolute()
    if directory.is_symlink():
        raise PolicyError("Каталог настроек не должен быть симлинком")
    settings = load(directory) or {"version": 1, "local_commit": "confirm", "locked_dependencies": "confirm", "artifact_roots": []}
    if network_project is not None:
        network_project = network_policy.project_path(network_project, require_exists=bool(network_allow or network_profiles))
        if any(value is not None for value in (local_commit, locked_dependencies, artifact_roots, code_modules, runtime_executable)) or clear_runtime:
            raise PolicyError("С --project изменяются только сетевые дополнения; общую политику задавайте отдельно")
    if runtime_executable is not None and clear_runtime:
        raise PolicyError("Нельзя одновременно выбрать и отозвать backend")
    if runtime_executable is not None:
        settings['runtime'] = codex_runtime.approve_runtime(runtime_executable, directory)
    elif clear_runtime:
        settings.pop('runtime', None)
    if any(value is not None for value in (network_allow, network_deny, network_remove, network_profiles)):
        settings['network'] = network_policy.update(settings.get('network'), network_project,
            network_allow, network_deny, network_remove, network_profiles)
    for key, value in (("local_commit", local_commit), ("locked_dependencies", locked_dependencies)):
        if value is not None:
            if value not in MODES:
                raise PolicyError("Неизвестный режим политики")
            settings[key] = value
    if artifact_roots is not None:
        settings["artifact_roots"] = validate_roots(artifact_roots, directory)
    if code_modules is not None:
        for value in code_modules:
            path = Path(value).expanduser().absolute()
            if (any(path.is_relative_to(Path(root)) for root in settings["artifact_roots"])
                    or path.is_relative_to(directory) or path.is_relative_to(KIT)):
                raise PolicyError("Разрешение модулей не распространяется на Obsidian, настройки Codex или kit")
        settings["code_modules"] = [file_access.approve(path) for path in sorted(set(code_modules))]
    for module in settings.get("code_modules", []):
        path = Path(module["path"])
        if (any(path.is_relative_to(Path(root)) for root in settings["artifact_roots"])
                or path.is_relative_to(directory) or path.is_relative_to(KIT)):
            raise PolicyError("Разрешение модулей не распространяется на Obsidian, настройки Codex или kit")
    paths = [directory / "config.toml", directory / "kit-restricted.config.toml",
             directory / ("AGENTS.override.md" if (directory / "AGENTS.override.md").exists() else "AGENTS.md"),
             directory / "rules/kit-autonomy.rules", directory / SETTINGS]
    originals = {}
    for path in paths:
        if path.is_symlink() or path.parent.is_symlink():
            raise PolicyError("Запись политики через симлинк запрещена")
        originals[path] = path.read_text() if path.exists() else None
    rule_path = paths[3]
    if originals[rule_path] is not None and not originals[rule_path].startswith(MARKER):
        raise PolicyError("Файл правил не помечен как принадлежащий kit; перезаписи нет")
    global_settings = {"approval_policy": "on-request", "approvals_reviewer": "user"}
    base = tomllib.loads(originals[paths[0]] or "")
    if "default_permissions" not in base and not any(key in base for key in ("sandbox_mode", "sandbox_workspace_write")):
        global_settings["default_permissions"] = ":workspace"
    updated = {paths[0]: update_scalars(originals[paths[0]] or "", global_settings),
               paths[2]: instructions(originals[paths[2]] or "", settings), rule_path: rules(settings),
               paths[4]: json.dumps(settings, ensure_ascii=False, indent=2) + "\n"}
    if originals[paths[1]] is not None:
        if not originals[paths[1]].startswith("# Managed by engineering-agent-kit permissions.py\n"):
            raise PolicyError("Профиль kit не имеет служебной отметки; перезаписи нет")
        updated[paths[1]] = update_scalars(originals[paths[1]], {"approval_policy": "on-request", "approvals_reviewer": "user"})
        if code_modules is not None:
            from permissions import refresh_file_rules
            updated[paths[1]] = refresh_file_rules(updated[paths[1]])
    changes = {path: content for path, content in updated.items() if content != originals[path]}
    state = {str(path): {"before": originals[path], "after": updated.get(path)} for path in paths}
    token = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return {"settings": settings, "originals": originals, "changes": changes, "confirmation_token": token}


def configure(directory, local_commit=None, locked_dependencies=None, artifact_roots=None, confirmation_token=None, code_modules=None,
              network_project=None, network_allow=None, network_deny=None, network_remove=None, network_profiles=None,
              runtime_executable=None, clear_runtime=False):
    prepared = plan(directory, local_commit, locked_dependencies, artifact_roots, code_modules,
                    network_project, network_allow, network_deny, network_remove, network_profiles, runtime_executable, clear_runtime)
    settings = prepared["settings"]
    before_settings = json.loads(prepared['originals'][Path(directory).expanduser().absolute() / SETTINGS] or '{}')
    installed = tomllib.loads(prepared['originals'][Path(directory).expanduser().absolute() / 'kit-restricted.config.toml'] or '')
    base_domains = installed.get('permissions', {}).get('kit-restricted', {}).get('network', {}).get('domains', {})
    network = {"scope": network_policy.project_path(network_project, False) if network_project is not None else 'all kit projects',
               "before": network_policy.effective(before_settings.get('network'), network_project, base_domains),
               "after": network_policy.effective(settings.get('network'), network_project, base_domains),
               "automatic_grants": False, "proxy_required": True}
    for key, value in (('configured_before', before_settings.get('network')), ('configured_after', settings.get('network'))):
        configured = network_policy.validate({} if value is None else value)
        if network_project is not None:
            configured = configured['projects'].get(network_policy.project_path(network_project, False), {'allow': [], 'deny': []})
        network[key] = {name: configured[name] for name in ('allow', 'deny')}
    if confirmation_token is not None:
        if confirmation_token != prepared["confirmation_token"]:
            raise PolicyError("Настройки или политика изменились; получите новый предпросмотр")
        for path, before in prepared["originals"].items():
            if path.is_symlink() or path.parent.is_symlink() or (path.read_text() if path.exists() else None) != before:
                raise PolicyError("Файлы изменились после предпросмотра; запись остановлена")
        for path, content in prepared["changes"].items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".kit-autonomy-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            try:
                temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    return {"files": [str(path) for path in prepared["changes"]], "applied": confirmation_token is not None,
            "confirmation_token": prepared["confirmation_token"], "restart_session": True,
            "artifact_roots": settings["artifact_roots"],
            "code_modules": [{"path": item["path"], "origin": item["origin"]["kind"], "access": "read"}
                             for item in settings.get("code_modules", [])],
            "network": network,
            "runtime": {"before": before_settings.get('runtime', {}).get('executable'),
                        "after": settings.get('runtime', {}).get('executable'),
                        "scope": "all kit projects; system codex unchanged"},
            "behavior": {"local_commit": "самостоятельно" if settings["local_commit"] == "auto" else "подтверждение",
                         "locked_dependencies": "самостоятельно" if settings["locked_dependencies"] == "auto" else "подтверждение",
                         "push_services_new_dependencies": "подтверждение"},
            "existing_rules_preserved": True, "auto_review_enabled": False}
