#!/usr/bin/env python3
"""Локальные рабочие папки Engineering Agent Kit; только стандартная библиотека."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


KIT_ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("review-pr", "plan-feature", "investigate-bug", "workspace-cleanup", "feature-hub", "verify-feature", "library-docs")
GIT_EXECUTOR = None
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")


class KitError(Exception):
    pass


def has_git_metadata(repo):
    marker = Path(repo) / ".git"
    try:
        return marker.is_file() or marker.is_dir() and (marker / "HEAD").is_file()
    except PermissionError:
        return False


def git(repo, *args, check=True):
    if GIT_EXECUTOR is not None:
        return GIT_EXECUTOR(repo, *args, check=check)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=120,
    )
    if check and result.returncode:
        # Сообщение Git может содержать URL с токеном доступа.
        raise KitError(f"Git {args[0]} завершился с кодом {result.returncode}: {repo}")
    return result


def git_text(repo, *args):
    return git(repo, *args).stdout.decode().strip()


def valid_name(value):
    if not NAME.fullmatch(value):
        raise KitError("Имя должно содержать только буквы, цифры, '-' и '_', до 80 символов")
    return value


def save(path, data):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise KitError("Путь реестра содержит символьную ссылку")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Непредсказуемое имя и O_EXCL внутри tempfile не дают подложить ссылку на чужой файл.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".kit-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    if not path.is_file():
        raise KitError(f"Файл не найден: {path}")
    return json.loads(path.read_text())


def project_paths(project):
    project = Path(project).resolve()
    storage = project / ".engineering-kit"
    if storage.resolve() != storage:
        raise KitError("Каталог .engineering-kit не должен быть символьной ссылкой")
    return project, storage


def config_for(project):
    project, storage = project_paths(project)
    config = load(storage / "project.json")
    if config.get("version") != 1 or config.get("project") != str(project):
        raise KitError("Неизвестный формат или перемещённый проект; проверьте project.json")
    return project, storage, config


def state_for(project, task_id):
    project, storage, config = config_for(project)
    task_id = valid_name(task_id)
    state_path = storage / "runs" / task_id / "state.json"
    state = load(state_path)
    if state.get("id") != task_id or state.get("version") != 1:
        raise KitError("Некорректный реестр рабочей папки")
    return project, storage, config, state_path, state


def init_project(args):
    project, storage = project_paths(args.project)
    if not project.is_dir():
        raise KitError("Каталог проекта не существует")
    config_path = storage / "project.json"
    if config_path.exists():
        raise KitError("Проект уже подключён; существующая конфигурация сохранена")
    if args.repo:
        candidates = [project / item for item in args.repo]
    elif has_git_metadata(project):
        candidates = [project]
    else:
        candidates = sorted(p for p in project.iterdir() if has_git_metadata(p))
    repos = {}
    for candidate in candidates:
        repo = candidate.resolve()
        alias = valid_name(candidate.name)
        if repo != project and not repo.is_relative_to(project):
            raise KitError("Репозиторий должен находиться внутри каталога проекта")
        root = Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve()
        if root != repo or alias in repos:
            raise KitError(f"Неоднозначный или вложенный репозиторий: {alias}")
        repos[alias] = str(repo)
    if not repos:
        raise KitError("Git-репозитории не найдены")
    save(config_path, {"version": 1, "project": str(project), "repos": repos})
    # Исключение локальное: отслеживаемые файлы приложения не меняются.
    for repo in repos.values():
        if Path(repo) == project:
            exclude = Path(git_text(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = Path(repo) / exclude
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text() if exclude.exists() else ""
            if "/.engineering-kit/" not in existing.splitlines():
                with exclude.open("a") as stream:
                    stream.write("\n/.engineering-kit/\n")
    return {"project": str(project), "repositories": list(repos)}


def commit_sha(repo, revision):
    return git_text(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")


def prepare(args):
    project, storage, config = config_for(args.project)
    task_id = valid_name(args.id)
    alias = valid_name(args.repo)
    if alias not in config["repos"]:
        raise KitError(f"Репозиторий не зарегистрирован: {alias}")
    repo = Path(config["repos"][alias])
    task_path = storage / "workspaces" / task_id
    target = task_path / alias
    state_path = storage / "runs" / task_id / "state.json"
    state = load(state_path) if state_path.exists() else {
        "version": 1, "id": task_id, "mode": args.mode, "repos": {},
    }
    if state.get("workspace") == "current" or any(entry.get("workspace") == "current" for entry in state["repos"].values()):
        raise KitError("Задача работает в исходной папке; для worktree используйте другой ID")
    if state["mode"] != args.mode:
        raise KitError("Нельзя смешивать ревью и реализацию в одной рабочей папке")
    if alias in state["repos"] or target.exists():
        raise KitError("Worktree уже зарегистрирован или путь занят; используйте status")
    if args.fetch:
        # Без --prune: обновляем только запрошенные ссылки, ничего не удаляем.
        for revision in dict.fromkeys([args.base, args.head or args.base]):
            if not revision.startswith("origin/"):
                raise KitError("Для --fetch нужны ссылки origin/имя-ветки")
            branch = revision[len("origin/"):]
            git(repo, "check-ref-format", f"refs/heads/{branch}")
            # Обновляется только tracking-ref; локальные ветки и сохранённые ревизии задач не переписываются.
            git(repo, "fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    base = commit_sha(repo, args.base)
    head = commit_sha(repo, args.head or args.base)
    merge_base = git_text(repo, "merge-base", base, head)
    branch = f"kit/{task_id}" if args.mode == "feature" else None
    if branch and git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
        raise KitError(f"Ветка {branch} уже существует; выберите другой ID")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve() != task_path or target.resolve() != target:
        raise KitError("Путь worktree содержит символьную ссылку")
    entry = {
        "source": str(repo), "path": str(target), "base": base, "head": head,
        "merge_base": merge_base, "branch": branch, "status": "preparing",
        "base_ref": args.base, "head_ref": args.head or args.base,
    }
    state["repos"][alias] = entry
    # Сначала записываем намерение: при обрыве процесса папка остаётся видимой в реестре.
    save(state_path, state)
    pin = f"refs/engineering-kit/{task_id}/{alias}"
    git(repo, "update-ref", f"{pin}/base", base)
    git(repo, "update-ref", f"{pin}/head", head)
    if branch:
        git(repo, "worktree", "add", "-b", branch, str(target), head)
    else:
        git(repo, "worktree", "add", "--detach", str(target), head)
    entry["status"] = "active"
    save(state_path, state)
    return {"id": task_id, "repo": alias, **entry, "results": str(state_path.parent)}


def registered_worktree(repo, target):
    result = git(repo, "worktree", "list", "--porcelain", "-z").stdout
    return f"worktree {target}".encode() in result.split(b"\0")


def inspect_entry(project, storage, config, task_id, alias, entry):
    source = config["repos"].get(alias)
    current = entry.get("workspace") == "current"
    expected = Path(source) if current and source else storage / "workspaces" / task_id / valid_name(alias)
    blockers = []
    if current:
        blockers.append("исходная рабочая папка current никогда не удаляется")
    if source != entry.get("source") or str(expected) != entry.get("path"):
        raise KitError("Реестр worktree не совпадает с конфигурацией проекта")
    if expected.resolve() != expected or storage.resolve() != storage:
        raise KitError("Символьные ссылки в пути рабочей папки запрещены")
    if entry["status"] == "removed":
        if expected.exists() or registered_worktree(source, expected):
            blockers.append("папка или регистрация появились после удаления")
        return {"repo": alias, "path": str(expected), "removed": True, "blockers": blockers}
    if not expected.is_dir():
        return {"repo": alias, "path": str(expected), "blockers": ["папка отсутствует; нужна ручная сверка регистрации"]}
    if not current and not registered_worktree(source, expected):
        blockers.append("Git не подтверждает регистрацию worktree")
    if Path(git_text(expected, "rev-parse", "--show-toplevel")).resolve() != expected:
        raise KitError("Путь не является корнем зарегистрированного worktree")
    changes = git(expected, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    ignored = git(expected, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout
    if changes:
        blockers.append("есть staged, unstaged или новые файлы")
    if ignored:
        blockers.append("есть игнорируемые файлы; их сохранность не подтверждена")
    if git_text(expected, "submodule", "status"):
        blockers.append("репозиторий содержит submodule; автоматическая очистка не поддерживается")
    head = commit_sha(expected, "HEAD")
    symbolic = git(expected, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = symbolic.stdout.decode().strip() if symbolic.returncode == 0 else None
    if branch != entry["branch"]:
        blockers.append("ветка worktree изменилась после создания")
    if branch:
        if commit_sha(source, f"refs/heads/{branch}") != head:
            blockers.append("HEAD не сохранён ожидаемой веткой")
    else:
        pin = f"refs/engineering-kit/{task_id}/{alias}/head"
        if head != entry["head"] or commit_sha(source, pin) != head:
            blockers.append("detached HEAD отличается от сохранённого коммита ревью")
    return {
        "repo": alias, "path": str(expected), "head": head, "branch": branch,
        "blockers": blockers, "removed": False, "workspace": "current" if current else "worktree",
    }


def inspect_run(project, storage, config, task_id, state):
    entries = [inspect_entry(project, storage, config, task_id, alias, entry)
               for alias, entry in sorted(state["repos"].items())]
    payload = {"id": task_id, "entries": entries}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return {**payload, "confirmation_token": token,
            "results": str(storage / "runs" / task_id),
            "can_remove": bool(entries) and all(not item["blockers"] for item in entries)}


def status(args):
    project, storage, config = config_for(args.project)
    if args.id:
        _, _, _, _, state = state_for(project, args.id)
        return inspect_run(project, storage, config, args.id, state)
    runs = storage / "runs"
    return {"runs": [p.parent.name for p in sorted(runs.glob("*/state.json"))]}


def cleanup(args):
    project, storage, config, state_path, state = state_for(args.project, args.id)
    preview = inspect_run(project, storage, config, args.id, state)
    if not args.confirm:
        return preview
    if args.confirm != preview["confirmation_token"]:
        raise KitError("Состояние изменилось или неверный токен; снова выполните cleanup без --confirm")
    if not args.idle_confirmed:
        raise KitError("Подтвердите отсутствие работающих сессий и процессов: --idle-confirmed")
    if not preview["can_remove"]:
        raise KitError("Очистка заблокирована; сначала устраните причины из предварительного просмотра")
    for item in preview["entries"]:
        if item["removed"]:
            continue
        alias = item["repo"]
        entry = state["repos"][alias]
        fresh = inspect_entry(project, storage, config, args.id, alias, entry)
        if fresh != item:
            raise KitError("Worktree изменился во время очистки; удаление остановлено")
        git(entry["source"], "worktree", "remove", entry["path"])
        entry["status"] = "removed"
        save(state_path, state)
    # rmdir удаляет только пустой контейнер, оставляя любые посторонние файлы.
    container = storage / "workspaces" / args.id
    if container.is_dir() and not any(container.iterdir()):
        container.rmdir()
    return {"id": args.id, "removed": [item["path"] for item in preview["entries"]],
            "results": str(state_path.parent), "branches_preserved": True}


def install_skills(args):
    destination = Path(args.dest).expanduser().absolute()
    for name in SKILLS:
        target = destination / name
        source = KIT_ROOT / "skills" / name
        if target.is_symlink() and target.resolve() == source:
            continue
        if target.exists() or target.is_symlink():
            raise KitError(f"Навык уже существует, перезапись запрещена: {target}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in SKILLS:
        target = destination / name
        if not target.is_symlink():
            target.symlink_to(KIT_ROOT / "skills" / name, target_is_directory=True)
    return {"installed": list(SKILLS), "destination": str(destination)}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Подключить каталог с одним или несколькими репозиториями")
    init.add_argument("--project", default=".")
    init.add_argument("--repo", action="append", help="Относительный путь; можно повторять")
    init.set_defaults(run=init_project)
    prep = sub.add_parser("prepare", help="Добавить worktree репозитория в рабочую папку задачи")
    prep.add_argument("--project", default=".")
    prep.add_argument("--id", required=True)
    prep.add_argument("--repo", required=True)
    prep.add_argument("--mode", choices=("review", "feature"), required=True)
    prep.add_argument("--base", required=True)
    prep.add_argument("--head")
    prep.add_argument("--fetch", action="store_true")
    prep.set_defaults(run=prepare)
    stat = sub.add_parser("status", help="Показать задачи или состояние worktree")
    stat.add_argument("--project", default=".")
    stat.add_argument("--id")
    stat.set_defaults(run=status)
    clean = sub.add_parser("cleanup", help="Предпросмотр; удаление только с токеном подтверждения")
    clean.add_argument("--project", default=".")
    clean.add_argument("--id", required=True)
    clean.add_argument("--confirm", help="Токен из предпросмотра; передавать только после согласия пользователя")
    clean.add_argument("--idle-confirmed", action="store_true")
    clean.set_defaults(run=cleanup)
    install = sub.add_parser("install-skills", help="Подключить skills символьными ссылками без перезаписи")
    install.add_argument("--dest", default=str(Path.home() / ".codex" / "skills"))
    install.set_defaults(run=install_skills)
    return cli


def main():
    args = parser().parse_args()
    try:
        result = args.run(args)
    except (KitError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
