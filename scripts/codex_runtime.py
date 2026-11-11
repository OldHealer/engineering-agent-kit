"""Изолированный запуск проверенных версий Codex с защищённым deny-glob scanner."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


MARKER = "engineering-agent-kit scanner runtime v1\n"
SUPPORTED_VERSIONS = {"codex-cli 0.166.0", "codex-cli 0.167.0"}
RUNTIME_FILES = ("bin/codex", "bin/codex-code-mode-host", "bin/codex-exec-server",
                 "codex-resources/bwrap", "codex-package.json")
RUNTIME_STAMP = {"device", "inode", "size", "mtime_ns", "mode", "uid", "gid", "sha256"}


class ScannerRuntimeError(ValueError):
    pass


def valid_runtime_snapshot(value):
    if (not isinstance(value, dict) or set(value) != {"version", "executable", "files"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["executable"], str) or not Path(value["executable"]).is_absolute()
            or not isinstance(value["files"], dict) or set(value["files"]) != set(RUNTIME_FILES)):
        return False
    for stamp in value["files"].values():
        if (not isinstance(stamp, dict) or set(stamp) != RUNTIME_STAMP
                or any(type(stamp[key]) is not int for key in RUNTIME_STAMP - {"sha256"})
                or not isinstance(stamp["sha256"], str) or len(stamp["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in stamp["sha256"])):
            return False
    return True


def approve_runtime(executable, directory):
    executable = Path(executable).expanduser().absolute()
    root = executable.parent.parent
    storage = Path(directory).expanduser().absolute() / "kit-native"
    if (executable.name != "codex" or executable.parent.name != "bin" or root == storage
            or not root.is_relative_to(storage) or root.resolve() != root):
        raise ScannerRuntimeError("Backend должен находиться в отдельной папке Codex/kit-native без симлинков")
    directories = [Path(directory).expanduser().absolute(), root / 'bin', root / 'codex-resources']
    current = root
    while current.is_relative_to(storage):
        directories.append(current)
        if current == storage:
            break
        current = current.parent
    for path in directories:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ScannerRuntimeError("Папка backend не защищена от чужой записи; её права не изменены")
    files = {}
    for name in RUNTIME_FILES:
        path = root / name
        if path.resolve() != path:
            raise ScannerRuntimeError("Backend не должен содержать симлинки")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                    or (name != "codex-package.json" and not before.st_mode & 0o111)):
                raise ScannerRuntimeError("Компонент backend имеет неподходящий тип, владельца или права")
            if name == "codex-package.json":
                if before.st_size > 4096:
                    raise ScannerRuntimeError("Неизвестная структура standalone backend")
                layout = json.loads(stream.read())
                if not isinstance(layout, dict) or layout.get("layoutVersion") != 1:
                    raise ScannerRuntimeError("Неизвестная структура standalone backend")
            elif stream.read(4) != b"\x7fELF":
                raise ScannerRuntimeError("Backend должен содержать штатные ELF-компоненты")
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid", "st_gid")
        if any(getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(current, key) for key in keys):
            raise ScannerRuntimeError("Компонент backend изменился во время проверки")
        # nlink/ctime меняются при штатных hard link в session runtime, содержимое остаётся тем же.
        files[name] = dict(zip(("device", "inode", "size", "mtime_ns", "mode", "uid", "gid"),
                              (getattr(before, key) for key in keys)), sha256=digest)
    return {"version": 1, "executable": str(executable), "files": files}


def approved_runtime(snapshot, directory):
    if not valid_runtime_snapshot(snapshot):
        raise ScannerRuntimeError("Некорректный снимок согласованного backend")
    try:
        current = approve_runtime(snapshot["executable"], directory)
    except (OSError, ValueError):
        raise ScannerRuntimeError("Согласованный backend недоступен; нужен новый предпросмотр или --clear-runtime") from None
    if current != snapshot:
        raise ScannerRuntimeError("Согласованный backend изменился; нужен новый предпросмотр или --clear-runtime")
    return Path(snapshot["executable"])


@dataclass
class Runtime:
    executable: Path
    directory: Path | None = None
    lease: int | None = None

    @property
    def pass_fds(self):
        return () if self.lease is None else (self.lease,)


def _running_owner(directory):
    owner = directory / "owner.json"
    if owner.is_symlink():
        return True
    if not owner.exists():
        return False
    try:
        pid = json.loads(owner.read_text()).get("pid")
    except (ValueError, UnicodeError):
        return True  # Повреждённая запись не доказывает завершение процесса.
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)  # Проверка существования без отправки сигнала и чтения /proc.
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cleanup(directory):
    """Удалить только завершённый runtime kit; чужие каталоги и активные сессии оставить."""
    if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("session-"):
        return False
    marker, lease = directory / ".kit-runtime", directory / ".lease"
    if marker.is_symlink() or lease.is_symlink() or not marker.is_file() or not lease.is_file():
        return False
    if directory.stat().st_uid != os.getuid() or marker.read_text() != MARKER:
        return False
    with lease.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if _running_owner(directory):
            return False
        # rmtree на Linux использует fd и не следует симлинкам на ресурсы Codex.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ScannerRuntimeError("Безопасная очистка runtime недоступна на этой платформе")
        shutil.rmtree(directory)
    return True


def prepare_cache(cache, project, create=True):
    cache = Path(cache)
    if cache.is_symlink():
        raise ScannerRuntimeError("Каталог runtime kit не должен быть симлинком")
    cache = cache.parent.resolve() / cache.name
    if cache.is_relative_to(project.resolve()) or project.resolve().is_relative_to(cache):
        raise ScannerRuntimeError("Runtime kit и рабочий проект должны находиться в отдельных каталогах")
    if not create and not cache.exists():
        return cache
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = cache.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ScannerRuntimeError("Runtime kit требует личный каталог с правами 0700; чужие права не изменены")
    for candidate in cache.iterdir():
        # Другая завершающаяся сессия могла удалить свой runtime после iterdir.
        try:
            cleanup(candidate)
        except FileNotFoundError:
            continue
    return cache


@contextmanager
def scanner_runtime(native, adapter, environment, cache, project):
    native = Path(native).resolve(strict=True)
    if "KIT_SCAN_DENIED_DIRS" not in environment:
        prepare_cache(cache, project, create=False)
        yield Runtime(native)
        return
    with native.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ScannerRuntimeError("Обход scanner требует нативный standalone Codex Linux; установка не изменена")
    version = subprocess.run([str(native), "--version"], env=environment, capture_output=True, text=True,
                             timeout=10, check=True)
    if version.stdout.strip() not in SUPPORTED_VERSIONS:
        raise ScannerRuntimeError("Обход scanner проверен для Codex 0.166.0/0.167.0; для другой версии нужна проверка совместимости")
    helpers = [native.parent / name for name in ("codex-code-mode-host", "codex-exec-server")]
    resources = native.parent.parent / "codex-resources"
    if not all(path.is_file() for path in helpers) or not resources.is_dir():
        raise ScannerRuntimeError("Не найдены штатные вспомогательные файлы standalone Codex; установка не изменена")
    denied = json.loads(environment["KIT_SCAN_DENIED_DIRS"])
    real_rg = environment["KIT_SCAN_RG"]
    if not isinstance(denied, list) or any(not isinstance(item, str) or not Path(item).is_absolute() for item in denied):
        raise ScannerRuntimeError("Некорректная политика scanner")
    if not Path(real_rg).is_absolute() or Path(real_rg).resolve() == adapter.resolve():
        raise ScannerRuntimeError("Некорректный путь штатного rg")
    cache = prepare_cache(cache, project)
    directory = Path(tempfile.mkdtemp(prefix="session-", dir=cache))
    lease = (directory / ".lease").open("xb")
    fcntl.flock(lease, fcntl.LOCK_EX)
    (directory / ".kit-runtime").write_text(MARKER)
    try:
        (directory / "bin").mkdir()
        executable = directory / "bin/codex"
        try:
            # Hard link не дублирует бинарник. Никогда не chmod: inode общий с установленным Codex.
            # Симлинк helper возвращает current_exe в установку, где нет scanner runtime kit.
            for source in (native, *helpers):
                os.link(source, directory / "bin" / source.name)
        except OSError as error:
            raise ScannerRuntimeError("Не удалось создать hard link бинарников Codex: runtime и установка должны быть на одном файловом разделе") from error
        (directory / "codex-resources").symlink_to(resources, target_is_directory=True)
        (directory / "codex-package.json").write_text('{"layoutVersion":1}\n')
        scanner = directory / "codex-path"
        scanner.mkdir()
        (scanner / "rg").write_bytes(adapter.read_bytes())
        (scanner / "rg").chmod(0o700)
        policy = {"denied": denied, "rg": real_rg}
        if "KIT_SCAN_CODE_MODULES" in environment:
            policy.update(code_modules=json.loads(environment["KIT_SCAN_CODE_MODULES"]),
                          managed_globs=json.loads(environment["KIT_SCAN_MANAGED_GLOBS"]))
            (scanner / "file_access.py").write_bytes((adapter.parents[2] / "scripts/file_access.py").read_bytes())
        (scanner / "scan-policy.json").write_text(json.dumps(policy))
        yield Runtime(executable, directory, lease.fileno())
    finally:
        lease.close()
        cleanup(directory)


def run_attached(command, environment, pass_fds=(), directory=None):
    with subprocess.Popen(command, env=environment, pass_fds=pass_fds) as process:
        if directory is not None:
            pending = directory / "owner.pending"
            pending.write_text(json.dumps({"pid": process.pid}))
            pending.replace(directory / "owner.json")
        while True:
            try:
                code = process.wait()
                return code if code >= 0 else 128 - code
            except KeyboardInterrupt:
                # Терминал доставляет SIGINT обоим процессам; Codex завершает работу сам.
                continue
