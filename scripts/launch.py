#!/usr/bin/env python3
"""Единая установка kit и запуск Codex из каталога выбранного проекта."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import uuid

import configure_mcp
import autonomy
import kit
import permissions
import scanner_install
import network_policy
import codex_runtime


def session_identifier(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError('Для resume нужен UUID сессии') from None


def install(args):
    destination = args.bin_dir / "kit"
    source = kit.KIT_ROOT / "bin/kit"
    if destination.is_symlink():
        if destination.resolve() != source:
            raise permissions.ProfileError("Команда kit уже указывает на другой файл; перезапись запрещена")
    elif destination.exists():
        raise permissions.ProfileError("Команда kit уже существует; перезапись запрещена")
    node = shutil.which("node")
    required = ("packages/bitbucket/dist/client.js", "packages/jira/dist/client.js",
                "node_modules/@modelcontextprotocol/sdk/package.json", "node_modules/zod/package.json")
    if not node or any(not (args.ai_box / item).is_file() for item in required):
        raise permissions.ProfileError("Нужны Node.js 22.12+ и готовые MCP-пакеты ai-box; зависимости автоматически не устанавливаются")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True,
                             env={"PATH": os.defpath}, timeout=10)
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+\s*", version.stdout)
    if not match or tuple(map(int, match.groups())) < (22, 12):
        raise permissions.ProfileError("Нужен Node.js 22.12 или новее; обновление самостоятельно не выполняется")
    if not shutil.which("codex") or not Path("/usr/bin/git").is_file() or not hasattr(os, "memfd_create"):
        raise permissions.ProfileError("Нужны Codex, Git и Linux/WSL с memfd; другие платформы пока не поддерживаются")
    permissions.preflight(Path.cwd(), args.codex_dir)
    skills = kit.install_skills(SimpleNamespace(dest=str(args.codex_dir / "skills")))
    mcp = configure_mcp.configure(args.codex_dir / "config.toml", args.ai_box.resolve(), node,
                                  with_context7=getattr(args, "with_context7", False))
    profile = permissions.install(args.codex_dir, include_rules=args.with_command_rules, dependencies=args.ai_box.resolve())
    args.bin_dir.mkdir(parents=True, exist_ok=True)
    if not destination.is_symlink():
        destination.symlink_to(source)
    return {"command": str(destination), "skills": skills["installed"], "mcp_added": mcp["added"],
            "profile": profile["profile"], "activated": False,
            "path_ready": str(args.bin_dir) in os.environ.get("PATH", "").split(os.pathsep),
            "rules_scope": profile["rules_scope"], "dependencies_installed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    setup = commands.add_parser("install", help="Один раз установить skills, MCP, профиль и команду kit")
    setup.add_argument("--ai-box", type=Path, default=permissions.ai_box())
    setup.add_argument("--codex-dir", type=Path, default=permissions.codex_home())
    setup.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    setup.add_argument("--with-command-rules", action="store_true", help="Установить правила для всех будущих сессий пользователя")
    setup.add_argument("--with-context7", action="store_true", help="Подключить удалённый MCP публичной документации")
    start = commands.add_parser("start", help="Запустить новую ограниченную сессию из проекта")
    start.add_argument("--project", type=Path, default=Path.cwd())
    start.add_argument("--kit-development", action="store_true")
    resume = commands.add_parser('resume', help='Продолжить сессию с backend и актуальной политикой kit')
    resume.add_argument('session_id', nargs='?', type=session_identifier)
    resume.add_argument('--last', action='store_true')
    resume.add_argument('--project', type=Path, default=Path.cwd())
    resume.add_argument('--kit-development', action='store_true')
    resume.set_defaults(resume_session=True)
    commands.add_parser("rollback", help="Показать команду отката установленного профиля")
    commands.add_parser("scanner", help="Предпросмотр root-owned scanner для подтверждённых команд")
    network = commands.add_parser("network", help="Показать сетевую политику и недостающие домены без выдачи доступа")
    network.add_argument('--project', type=Path, default=Path.cwd())
    network.add_argument('--codex-dir', type=Path, default=permissions.codex_home())
    policy = commands.add_parser("policy", help="Предпросмотр общей политики команд; применение по токену")
    policy.add_argument("--codex-dir", type=Path, default=permissions.codex_home())
    policy.add_argument("--local-commit", choices=("confirm", "auto"), help="Режим локальных коммитов; confirm сохраняет ручное согласование")
    policy.add_argument("--locked-dependencies", choices=("confirm", "auto"), help="Восстановление по lock в окружении проекта; auto сохраняет подтверждения новых зависимостей")
    policy.add_argument("--artifact-root", action="append", help="Разрешённая папка артефактов для всех запусков kit")
    policy.add_argument('--project', type=Path, help='Изменить только сетевые дополнения указанного проекта')
    policy.add_argument('--network-profile', action='append', choices=sorted(network_policy.PROFILES), help='Добавить точные домены реестров')
    policy.add_argument('--network-allow', action='append', help='Добавить точный DNS-домен в выбранной области')
    policy.add_argument('--network-deny', action='append', help='Добавить запрет домена; запрет приоритетнее allow')
    policy.add_argument('--network-remove', action='append', help='Отозвать собственные записи домена в выбранной области')
    backend = policy.add_mutually_exclusive_group()
    backend.add_argument('--runtime-executable', type=Path, help='Выбрать проверенный backend из личной папки Codex/kit-native')
    backend.add_argument('--clear-runtime', action='store_true', help='Вернуться к системному codex; backend-файлы сохраняются')
    modules = policy.add_mutually_exclusive_group()
    modules.add_argument("--code-module", action="append", help="Точный программный модуль: чтение после проверки происхождения и preview/apply")
    modules.add_argument("--clear-code-modules", action="store_true", help="Отозвать разрешения модулей через preview/apply")
    policy.add_argument("--apply", metavar="TOKEN", help="Применить точный предпросмотр после согласования")
    args = parser.parse_args()
    if args.action == 'resume' and args.session_id is not None and args.last:
        parser.error('Для resume выберите UUID или --last')
    try:
        if args.action == "install":
            print(json.dumps(install(args), ensure_ascii=False, indent=2))
        elif args.action == "policy":
            print(json.dumps(autonomy.configure(args.codex_dir, args.local_commit, args.locked_dependencies,
                                                args.artifact_root, args.apply,
                                                code_modules=[] if args.clear_code_modules else args.code_module,
                                                network_project=args.project, network_allow=args.network_allow,
                                                network_deny=args.network_deny, network_remove=args.network_remove,
                                                network_profiles=args.network_profile, runtime_executable=args.runtime_executable,
                                                clear_runtime=args.clear_runtime), ensure_ascii=False, indent=2))
        elif args.action == 'network':
            settings = autonomy.load(args.codex_dir) or {}
            profile = permissions.load_structure(args.codex_dir / (permissions.NAME + '.config.toml'))
            domains = profile.get('permissions', {}).get(permissions.NAME, {}).get('network', {}).get('domains', {})
            print(json.dumps(network_policy.inspect(args.project, settings.get('network'), domains), ensure_ascii=False, indent=2))
        elif args.action == "scanner":
            print(json.dumps(scanner_install.plan(), ensure_ascii=False, indent=2))
        elif args.action == "rollback":
            print("python3 " + str(kit.KIT_ROOT / "scripts/permissions.py") + " rollback")
            print("Добавьте --confirm только после просмотра списка файлов. Команда kit и skills сохраняются.")
        else:
            return permissions.run(args if args.action in {"start", "resume"} else SimpleNamespace(project=Path.cwd(), kit_development=False))
    except (OSError, ValueError, kit.KitError, subprocess.SubprocessError) as error:
        # При ошибке чужой конфигурации не выводим TOML и значения параметров.
        message = str(error) if isinstance(error, (permissions.ProfileError, kit.KitError, autonomy.PolicyError, scanner_install.ScannerInstallError, network_policy.NetworkPolicyError, codex_runtime.ScannerRuntimeError)) else "Установка/запуск остановлены; проверьте пути, готовые зависимости и конфликты файлов. Секретные значения скрыты."
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
