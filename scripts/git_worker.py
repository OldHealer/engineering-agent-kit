#!/usr/bin/env python3
"""Ограниченные Git-операции MCP. Запрос и авторизация передаются только по stdin."""

import argparse
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import kit


class GitError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GitError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_file(value):
    path = Path(value)
    require(value and not any(ord(char) < 32 for char in value) and not path.is_absolute() and "\\" not in value and
            all(part not in {".", ".."} for part in value.split("/")), "Недопустимый путь файла")
    for part in path.parts:
        require(not (part.startswith((".env", "id_rsa", "id_ed25519")) or
                     re.search(r"secret|credentials", part, re.I) or
                     part.endswith((".env", ".pem", ".key", ".p12", ".pfx")) or
                     part in {".git", ".engineering-kit", ".agents", ".codex", ".aws", ".ssh", "auth.json", ".netrc", ".npmrc"}),
                "Секретный или служебный путь исключён из операции")
    return path


class WorkspaceGit:
    def __init__(self, project, connections=None, local_test=False):
        self.project = Path(project).resolve(strict=True)
        require(self.project.is_dir(), "Проект не найден")
        self.connections = connections or {}
        self.local_test = local_test
        self.authorization = None
        kit.GIT_EXECUTOR = self.git

    def inside(self, path):
        path = Path(path).absolute()
        require(path.is_relative_to(self.project) and path.resolve() == path, "Путь выходит за проект или содержит симлинк")
        return path

    @contextmanager
    def lock(self):
        storage = self.inside(self.project / ".engineering-kit")
        storage.mkdir(exist_ok=True)
        descriptor = os.open(storage / "git.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError:
            raise GitError("С проектом уже выполняется Git-операция; повторите после её завершения") from None
        finally:
            os.close(descriptor)

    def metadata(self, repo):
        repo = self.inside(repo)
        pointer = repo / ".git"
        if pointer.is_dir() and not pointer.is_symlink():
            common = self.inside(pointer)
        else:
            # Разрешены только worktree под реестром kit, с Git-базой этого проекта.
            require(repo.is_relative_to(self.project / ".engineering-kit/workspaces") and pointer.is_file()
                    and not pointer.is_symlink(), "Используйте исходный каталог проекта, не чужой worktree")
            value = pointer.read_text().strip()
            require(value.startswith("gitdir: ") and len(value) < 4096, "Некорректный указатель worktree")
            directory = Path(value[8:])
            directory = self.inside(directory if directory.is_absolute() else repo / directory)
            require(directory.parent.name == "worktrees" and directory.parent.parent.name == ".git",
                    "Git-база worktree не принадлежит проекту")
            common = directory.parent.parent
            require((directory / "gitdir").is_file() and not (directory / "gitdir").is_symlink()
                    and Path((directory / "gitdir").read_text().strip()) == pointer, "Обратный указатель worktree не совпадает")
        require(not (common / "config").is_symlink(), "Git config не должен быть симлинком")
        # includes, filters, hooks и credential helpers нельзя выполнять с правами MCP.
        # hooksPath допустим: raw безусловно переопределяет его для каждого Git-вызова.
        keys = self.raw(repo, "config", "--local", "--no-includes", "--name-only", "--list").stdout.decode().splitlines()
        allowed = re.compile(r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|symlinks|"
                             r"precomposeunicode|quotepath|autocrlf|eol|compression|hookspath)|"
                             r"remote\.[^.]+\.(url|fetch|pushurl)|branch\..+\.(remote|merge|rebase)|"
                             r"user\.(name|email)|init\.defaultbranch|extensions\.objectformat)$", re.I)
        require(all(allowed.fullmatch(key) for key in keys),
                "В Git config есть настройки вне безопасного списка; проверьте их отдельно, значения скрыты")
        for relative in ("info", "info/exclude", "objects", "objects/info", "objects/info/alternates", "hooks", "refs", "worktrees"):
            candidate = common / relative
            require(not candidate.is_symlink(), "Симлинк в служебных файлах Git не поддерживается")
        return repo

    def raw(self, repo, *args, check=True):
        environment = {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                       "GIT_NO_REPLACE_OBJECTS": "1"}
        options = ["core.hooksPath=/dev/null", "core.fsmonitor=false", "submodule.recurse=false",
                   "credential.helper=", "credential.interactive=false", "http.followRedirects=false",
                   "http.sslVerify=true", "http.proxy=", "protocol.allow=never", "protocol.https.allow=always",
                   "fetch.fsckObjects=true", "fetch.recurseSubmodules=false", "maintenance.auto=false", "gc.auto=0"]
        if self.local_test:
            options += ["protocol.file.allow=always", "protocol.http.allow=always"]
        descriptor = None
        try:
            if self.authorization:
                # Linux/WSL memfd: заголовок не попадает в argv, окружение или файл на диске.
                descriptor = os.memfd_create("kit-git-auth", os.MFD_CLOEXEC)
                url, header = self.authorization
                content = f'[http {json.dumps(url)}]\nextraHeader = {json.dumps(header)}\n'
                os.write(descriptor, content.encode())
                os.lseek(descriptor, 0, os.SEEK_SET)
                environment["GIT_CONFIG_GLOBAL"] = f"/proc/self/fd/{descriptor}"
            command = ["/usr/bin/git", "--literal-pathspecs", "-C", str(repo)]
            for option in options:
                command += ["-c", option]
            result = subprocess.run(command + list(args), capture_output=True, env=environment, timeout=120,
                                    pass_fds=() if descriptor is None else (descriptor,))
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if check and result.returncode:
            diagnostic = result.stderr.lower()
            if b"operation not permitted" in diagnostic:
                raise GitError("Git-соединение запрещено средой; это не доказательство неверного токена")
            if b"could not resolve host" in diagnostic:
                raise GitError("Git не смог разрешить имя сервера; токен не проверен")
            if b"authentication failed" in diagnostic or b"error: 401" in diagnostic:
                raise GitError("Git-сервер отклонил авторизацию; проверьте пригодность и права токена отдельно")
            if b"error: 403" in diagnostic:
                raise GitError("Git-сервер отказал в доступе HTTP 403; проверьте права и ограничения сервиса")
            if b"couldn't find remote ref" in diagnostic:
                raise GitError("Запрошенная ветка не найдена на сервере; уточните базовую или исходную ветку")
            raise GitError(f"Git {args[0]} завершился с кодом {result.returncode}; вывод скрыт. Проверьте доступ или состояние операции перед повтором")
        return result

    def git(self, repo, *args, check=True):
        self.metadata(repo)
        if args[0] == "fetch":
            require(len(args) == 4 and args[1:3] == ("--no-tags", "origin"), "Неподдерживаемая операция fetch")
            url, header = self.remote(repo)
            old = self.authorization
            self.authorization = (url, header) if header else None
            try:
                return self.raw(repo, "fetch", "--no-tags", "--no-recurse-submodules", url, *args[3:], check=check)
            finally:
                self.authorization = old
        return self.raw(repo, *args, check=check)

    def text(self, repo, *args):
        return self.git(repo, *args).stdout.decode().strip()

    def remote(self, repo):
        value = self.raw(repo, "config", "--local", "--no-includes", "--get", "remote.origin.url").stdout.decode().strip()
        if self.local_test and (value.startswith("/") or value.startswith("http://127.0.0.1:")):
            token = self.connections.get("bitbucket", {}).get("token") if value.startswith("http:") else None
            return value, f"Authorization: Bearer {token}" if token else None
        require(value and not any(char in value for char in "\n\r\t"), "Не задан безопасный origin")
        if value.startswith("git@") and ":" in value:
            host, resource = value[4:].split(":", 1)
            parsed_host, parsed_path = host, resource
        else:
            parsed = urlsplit(value)
            require(parsed.scheme in {"https", "ssh"} and not parsed.password and not parsed.query and not parsed.fragment,
                    "Поддерживается HTTPS/SSH origin без встроенных секретов")
            require(parsed.username in {None, "git"}, "Origin содержит встроенную авторизацию")
            parsed_host, parsed_path = parsed.hostname, parsed.path.lstrip("/")
        parts = parsed_path.removesuffix(".git").split("/")
        if parsed_host == "github.com":
            require(len(parts) == 2 and all(re.fullmatch(r"[\w.-]+", item) and item not in {".", ".."} for item in parts), "Некорректный GitHub origin")
            connection = self.connections.get("github")
            require(connection is not None, "GitHub-токен не настроен в MCP")
            token = base64.b64encode(f"x-access-token:{connection['token']}".encode()).decode()
            return f"https://github.com/{'/'.join(parts)}.git", f"Authorization: Basic {token}"
        connection = self.connections.get("bitbucket")
        require(connection is not None, "Bitbucket-токен не настроен в MCP")
        base = urlsplit(connection["host"])
        require(parsed_host == base.hostname and base.scheme == "https", "Origin не принадлежит разрешённому Bitbucket")
        prefix = base.path.strip("/")
        if parts[:len(prefix.split("/")) + 1] == prefix.split("/") + ["scm"]:
            parts = parts[len(prefix.split("/")) + 1:]
        elif parts and parts[0] == "scm":
            parts = parts[1:]
        require(len(parts) == 2 and all(re.fullmatch(r"[\w.-]+", item) and item not in {".", ".."} for item in parts), "Некорректный Bitbucket origin")
        return f"{connection['host'].rstrip('/')}/scm/{'/'.join(parts)}.git", f"Authorization: Bearer {connection['token']}"

    def validate_registry(self):
        self.inside(self.project / ".engineering-kit/project.json")
        _, storage, config = kit.config_for(self.project)
        for value in config["repos"].values():
            source = self.inside(value)
            require(source == self.project or not source.is_relative_to(storage), "Исходный репозиторий не должен быть рабочей папкой kit")
            require((source / ".git").exists() and not (source / ".git").is_symlink(), "В реестре не найден Git-репозиторий проекта")
        runs = storage / "runs"
        require(not runs.is_symlink(), "Реестр задач не должен быть симлинком")
        return storage, config

    def state(self, task_id):
        kit.valid_name(task_id)
        self.inside(self.project / ".engineering-kit/runs" / task_id / "state.json")
        return kit.state_for(self.project, task_id)

    def branch_name(self, name):
        require(isinstance(name, str) and name and not name.startswith(("-", "refs/")) and "@{" not in name,
                "Нужно явное имя ветки")
        self.raw(self.project, "check-ref-format", "--branch", name)
        return name

    def workspace_settings(self, config):
        settings = config.get("workspace", {"mode": "worktree", "development_branch": "main", "target_branch": "main"})
        require(isinstance(settings, dict) and settings.get("mode") in {"worktree", "current"}, "Неизвестный режим проекта")
        result = {"mode": settings["mode"], "development_branch": settings.get("development_branch", "main"),
                  "target_branch": settings.get("target_branch", "main")}
        for key in ("development_branch", "target_branch"):
            self.branch_name(result[key])
        if result["mode"] == "current":
            require(result["development_branch"] not in {"main", "master", result["target_branch"]},
                    "Прямая разработка в main/master или целевой ветке не разрешена")
        return result

    def configure(self, request):
        storage, config = self.validate_registry()
        candidate = {"mode": request["workspace"], "development_branch": request["development_branch"],
                     "target_branch": request["target_branch"]}
        settings = self.workspace_settings({"workspace": candidate})
        config["workspace"] = settings
        kit.save(storage / "project.json", config)
        return {"workspace": settings, "branches_switched": False}

    def prepare_current(self, request, settings):
        require(request.get("mode", "feature") == "feature", "Ревью PR подготавливается отдельно в worktree")
        task_id, alias = kit.valid_name(request["id"]), kit.valid_name(request["repo"])
        storage, config = self.validate_registry()
        require(alias in config["repos"], "Репозиторий не зарегистрирован")
        source = self.metadata(Path(config["repos"][alias]))
        require((source / ".git").is_dir(), "В режиме current нужен исходный репозиторий")
        branch = settings["development_branch"]
        require(request.get("base") in {None, branch, f"origin/{branch}"} and request.get("head") is None,
                "Режим current использует выбранную ветку разработки, base/head относятся к worktree")
        require(request.get("fetch") is not True, "В current автоматический fetch не выполняется; рабочая ветка не обновляется молча")
        state_path = self.inside(storage / "runs" / task_id / "state.json")
        state = kit.load(state_path) if state_path.exists() else {
            "version": 1, "id": task_id, "mode": "feature", "workspace": "current", "repos": {}}
        require(state.get("workspace") == "current" and state.get("mode") == "feature"
                and state.get("id") == task_id and state.get("version") == 1,
                "Нельзя смешивать режимы задачи; используйте другой ID")
        require(alias not in state["repos"], "Репозиторий уже подключён к задаче; используйте status")
        changes = bool(self.git(source, "status", "--porcelain", "-z").stdout)
        require(not changes or request.get("include_changes") is True,
                "В исходной папке есть изменения; согласуйте их включение в задачу без stash/reset")
        actual = self.git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.decode().strip()
        if actual != branch:
            require(request.get("switch_branch") is True, "Текущая ветка отличается от выбранной; согласуйте switch_branch отдельно")
            require(not changes, "Переключение ветки с локальными изменениями запрещено")
            self.git(source, "show-ref", "--verify", f"refs/heads/{branch}")
            self.git(source, "switch", "--no-guess", branch)
        head = self.text(source, "rev-parse", "HEAD")
        entry = {"source": str(source), "path": str(source), "base": head, "head": head,
                 "merge_base": head, "branch": branch, "base_ref": branch, "head_ref": branch,
                 "target_branch": settings["target_branch"], "workspace": "current", "status": "active"}
        state["repos"][alias] = entry
        kit.save(state_path, state)
        return {"id": task_id, "repo": alias, **entry, "results": str(state_path.parent), "worktree_created": False}

    def entry(self, task_id, alias):
        kit.valid_name(alias)
        self.validate_registry()
        _, storage, config, _, state = self.state(task_id)
        require(state["mode"] == "feature", "Коммит и push разрешены только в рабочей ветке реализации")
        require(alias in state["repos"], "Репозиторий не выбран для задачи")
        entry = state["repos"][alias]
        current = entry.get("workspace") == "current"
        if current:
            settings = self.workspace_settings(config)
            require(settings["mode"] == "current" and entry["branch"] == settings["development_branch"]
                    and entry.get("target_branch") == settings["target_branch"],
                    "Настройки current изменились; согласуйте новый контекст задачи")
        expected = Path(config["repos"][alias]) if current else storage / "workspaces" / kit.valid_name(task_id) / kit.valid_name(alias)
        require(entry["path"] == str(expected) and entry["source"] == config["repos"][alias]
                and (current or entry["branch"] == f"kit/{task_id}") and entry["status"] == "active", "Реестр задачи не совпадает с рабочей папкой")
        require(re.fullmatch(r"[a-f0-9]{40,64}", entry["base"]), "Некорректная базовая ревизия")
        self.metadata(expected)
        common = self.text(expected, "rev-parse", "--git-common-dir")
        require((expected / common).resolve() == Path(entry["source"]) / ".git", "Worktree использует другую Git-базу")
        require(self.text(expected, "symbolic-ref", "--short", "HEAD") == entry["branch"], "Рабочая ветка изменилась")
        return expected, entry

    def check_private_artifacts(self, repo):
        # info/exclude не защищает файлы, уже попавшие в индекс или историю.
        tracked = self.git(repo, "ls-files", "-z").stdout.decode().strip("\0")
        require(not any(".engineering-kit" in Path(name).parts for name in tracked.split("\0") if name),
                "Личные артефакты .engineering-kit уже отслеживаются Git; разберите их отдельно до commit/push")

    def snapshot(self, request):
        repo, entry = self.entry(request["id"], request["repo"])
        files = sorted(set(request["files"]))
        require(files and len(files) <= 1000, "Выберите явный непустой список файлов")
        require(not self.git(repo, "diff", "--cached", "--name-only", "-z").stdout, "Индекс уже содержит изменения; сначала разберите их отдельно")
        require(not self.git(repo, "ls-files", "--unmerged", "-z").stdout, "В worktree есть неразрешённые конфликты")
        self.check_private_artifacts(repo)
        snapshots = []
        for name in files:
            path = self.inside(repo / safe_file(name))
            require(not path.is_symlink(), "Коммит симлинков требует отдельной проверки")
            if path.exists():
                require(path.is_file(), "В списке коммита должны быть файлы, не каталоги")
                # Точные имена проверены до чтения; содержимое не возвращается агенту.
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                mode = stat.S_IMODE(path.stat().st_mode)
                blob = self.text(repo, "hash-object", f"--path={name}", "--", name)
            else:
                require(self.git(repo, "ls-files", "--error-unmatch", "--", name, check=False).returncode == 0,
                        "Удалённый файл не отслеживался Git")
                checksum, mode, blob = None, None, None
            snapshots.append({"path": name, "sha256": checksum, "mode": mode, "git_blob": blob})
        message = request["message"]
        require(isinstance(message, str) and message.strip() and len(message) <= 16000 and "\x00" not in message, "Некорректный текст коммита")
        _, config = self.validate_registry()
        author = request.get("author") or config.get("author") or {
            key: self.raw(repo, "config", "--local", "--no-includes", "--get", f"user.{key}", check=False).stdout.decode().strip()
            for key in ("name", "email")}
        require(set(author) == {"name", "email"} and all(isinstance(value, str) and value.strip() and
                not any(char in value for char in "\r\n\x00<>") for value in author.values()),
                "Нужны имя и email автора; спросите пользователя один раз и передайте author")
        return {"repo": request["repo"], "id": request["id"], "branch": entry["branch"],
                "workspace": entry.get("workspace", "worktree"), "path": str(repo),
                "head": self.text(repo, "rev-parse", "HEAD"), "files": snapshots, "message": message, "author": author}

    def commit(self, request):
        preview = self.snapshot(request)
        if request["action"] == "commit_preview":
            return {**preview, "confirmation_token": digest(preview)}
        require(request.get("confirmation_token") == digest(preview), "Состав, текст или HEAD изменились; запросите новый предпросмотр коммита")
        repo, _ = self.entry(request["id"], request["repo"])
        names = [item["path"] for item in preview["files"]]
        self.git(repo, "add", "--", *names)
        staged = self.git(repo, "diff", "--cached", "--name-only", "-z").stdout.decode().strip("\0").split("\0")
        require(staged != [""] and set(staged).issubset(names), "Индекс отличается от согласованного состава; коммит остановлен")
        for item in preview["files"]:
            record = self.text(repo, "ls-files", "--stage", "--", item["path"])
            blob = record.split()[1] if record else None
            require(blob == item["git_blob"], "Файлы изменились во время подготовки индекса; коммит остановлен")
        tree = self.text(repo, "write-tree")
        commit = self.text(repo, "-c", f"user.name={preview['author']['name']}", "-c", f"user.email={preview['author']['email']}",
                           "commit-tree", tree, "-p", preview["head"], "-m", preview["message"])
        # Сравнение старого SHA атомарно: чужой commit не станет новым родителем молча.
        self.git(repo, "update-ref", "-m", "engineering-agent-kit commit", f"refs/heads/{preview['branch']}", commit, preview["head"])
        storage, config = self.validate_registry()
        if not config.get("author"):
            config["author"] = preview["author"]
            kit.save(storage / "project.json", config)
        return {"commit": commit, "branch": preview["branch"], "files": staged}

    def push(self, request):
        repo, entry = self.entry(request["id"], request["repo"])
        require(not self.git(repo, "status", "--porcelain", "-z").stdout, "Перед push разберите несохранённые изменения")
        self.check_private_artifacts(repo)
        self.git(repo, "merge-base", "--is-ancestor", entry["base"], "HEAD")
        url, header = self.remote(Path(entry["source"]))
        head = self.text(repo, "rev-parse", "HEAD")
        current = entry.get("workspace") == "current"
        remote_head, checked_base = None, entry["base"]
        if current:
            self.authorization = (url, header) if header else None
            try:
                result = self.git(repo, "ls-remote", url, f"refs/heads/{entry['branch']}")
            finally:
                self.authorization = None
            remote_head = result.stdout.decode().split()[0] if result.stdout.strip() else None
            require(remote_head is None or re.fullmatch(r"[a-f0-9]{40,64}", remote_head), "Неожиданный ответ Git-сервера")
            if remote_head:
                self.git(repo, "merge-base", "--is-ancestor", remote_head, head)
            checked_base = remote_head
        commits = self.text(repo, "rev-list", f"{checked_base}..{head}" if checked_base else head).splitlines()
        require(len(commits) <= 1000 and (commits or current and remote_head == head), "Нет новых коммитов либо диапазон слишком велик для проверки")
        for commit in commits:
            changed = self.git(repo, "diff-tree", "--root", "-m", "--no-commit-id", "--name-only", "-z", "-r", commit).stdout.decode().strip("\0")
            for name in changed.split("\0") if changed else []:
                safe_file(name)
        preview = {"id": request["id"], "repo": request["repo"], "branch": entry["branch"], "destination": url,
                   "head": head, "workspace": entry.get("workspace", "worktree"), "remote_head": remote_head,
                   "checked_base": checked_base, "up_to_date": current and remote_head == head}
        if request["action"] == "push_preview":
            return {**preview, "confirmation_token": digest(preview)}
        require(request.get("confirmation_token") == digest(preview), "HEAD или назначение изменились; повторите предпросмотр push")
        if preview["up_to_date"]:
            return {**preview, "pushed": False, "pull_request_created": False}
        self.authorization = (url, header) if header else None
        try:
            # Один точный SHA -> одна ветка задачи; force, теги, PR и удаление не предоставляются.
            self.git(repo, "push", "--porcelain", "--no-verify", "--no-follow-tags", "--recurse-submodules=no", url,
                     f"{preview['head']}:refs/heads/{preview['branch']}")
        finally:
            self.authorization = None
        return {**preview, "pushed": True, "pull_request_created": False}

    def dispatch(self, request):
        action = request["action"]
        require(action not in {"init", "configure", "prepare", "commit", "push", "cleanup"} or request.get("confirm") is True,
                "Требуется явное согласие пользователя на указанное действие")
        with self.lock():
            if action == "discover":
                roots = [self.project] if kit.has_git_metadata(self.project) else sorted(
                    item for item in self.project.iterdir() if item.is_dir() and kit.has_git_metadata(item)
                    and item.name != ".engineering-kit")
                registered = (self.project / ".engineering-kit/project.json").is_file()
                settings = self.workspace_settings(self.validate_registry()[1]) if registered else None
                return {"project": str(self.project), "repositories": [item.name for item in roots if item.resolve() == item],
                        "registered": registered, "workspace": settings}
            if action == "init":
                for name in request.get("repos", []):
                    require(name == "." or kit.valid_name(name), "Недопустимый исходный репозиторий")
                if (self.project / ".engineering-kit/project.json").exists():
                    _, config = self.validate_registry()
                    return {"project": str(self.project), "repositories": list(config["repos"]), "already_registered": True}
                return kit.init_project(SimpleNamespace(project=self.project, repo=request.get("repos")))
            self.validate_registry()
            if action == "configure":
                return self.configure(request)
            if action == "prepare":
                self.inside(self.project / ".engineering-kit/runs" / kit.valid_name(request["id"]) / "state.json")
                settings = self.workspace_settings(self.validate_registry()[1])
                workspace = request.get("workspace") or ("worktree" if request.get("mode") == "review" else settings["mode"])
                require(workspace in {"current", "worktree"}, "Неизвестный режим задачи")
                if workspace == "current":
                    require(settings["mode"] == "current", "Сначала согласуйте и настройте режим current проекта")
                    return self.prepare_current(request, settings)
                return kit.prepare(SimpleNamespace(project=self.project, id=request["id"], repo=request["repo"],
                    mode=request.get("mode", "feature"), base=request.get("base", f"origin/{settings['development_branch']}"),
                    head=request.get("head"), fetch=request.get("fetch", True)))
            if action == "status":
                if request.get("id"):
                    self.state(request["id"])
                return kit.status(SimpleNamespace(project=self.project, id=request.get("id")))
            if action == "cleanup_preview" or action == "cleanup":
                # Поля реестра не должны перенаправить удаление или чтение за корень сессии.
                _, _, _, _, state = self.state(request["id"])
                for alias, entry in state["repos"].items():
                    kit.valid_name(alias)
                    self.inside(entry["path"])
                return kit.cleanup(SimpleNamespace(project=self.project, id=request["id"],
                    confirm=request.get("confirmation_token") if action == "cleanup" else None,
                    idle_confirmed=request.get("idle_confirmed", False)))
            if action in {"commit_preview", "commit"}:
                return self.commit(request)
            if action in {"push_preview", "push"}:
                return self.push(request)
            if action == "remote_check":
                _, config = self.validate_registry()
                source = Path(config["repos"][request["repo"]])
                self.metadata(source)
                url, header = self.remote(source)
                self.authorization = (url, header) if header else None
                if request.get("id"):
                    _, entry = self.entry(request["id"], request["repo"])
                    reference = f"refs/heads/{entry['branch']}"
                else:
                    reference = "HEAD"
                result = self.git(source, "ls-remote", "--exit-code", url, reference, check=False)
                self.authorization = None
                require(result.returncode in {0, 2}, "Git-сервер недоступен или отказал в чтении; подробности скрыты, проверьте сеть и права отдельно")
                head = result.stdout.decode().split()[0] if result.stdout.strip() else None
                require(head is None or re.fullmatch(r"[a-f0-9]{40,64}", head), "Git-сервер вернул неожиданный ответ")
                return {"reachable": True, "head_received": bool(head), "reference": reference, "sha": head}
            raise GitError("Операция не поддерживается; произвольные команды, PR и force push недоступны")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        require(request.get("action") not in {"init", "configure", "prepare", "commit", "push", "cleanup"} or request.get("confirm") is True,
                "Требуется явное согласие пользователя на указанное действие")
        result = WorkspaceGit(args.project, request.pop("_connections", {})).dispatch(request)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except (GitError, kit.KitError) as error:
        print(json.dumps({"ok": False, "code": "GIT_OPERATION_REFUSED", "message": str(error)}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "code": "GIT_OPERATION_FAILED", "message": "Git-операция остановлена; подробности скрыты. Проверьте состояние перед повтором"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
