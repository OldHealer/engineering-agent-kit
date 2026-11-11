"""Явные разрешения модулей по происхождению и метаданным, без чтения их содержимого."""

import csv
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess


SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
RESERVED = re.compile(r"^(?:auth|credentials?|passwords?|secrets?)(?:[._-]|$)", re.I)


def identity(path):
    path = Path(path)
    if path.resolve() != path:
        raise ValueError("Разрешение модуля не поддерживает симлинки в его пути")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Модуль должен быть обычным файлом пользователя без записи для группы/остальных")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink]


def approve(path):
    path = Path(path).expanduser().absolute()
    snapshot = identity(path)
    if any(char in str(path) for char in '*?[]{}') or path.suffix not in SOURCE_SUFFIXES or any(
            part.startswith((".env", "id_rsa", "id_ed25519"))
            or part.lower().endswith((".env", ".pem", ".key", ".p12", ".pfx"))
            or part.lower() in {".ssh", ".aws", ".codex", ".git", ".netrc", ".npmrc", ".git-credentials"}
            for part in path.parts):
        raise ValueError("Имя или расположение модуля относится к защищённым данным")
    # RECORD описывает конкретный установленный файл; всё окружение не становится разрешённым.
    for site in path.parents:
        if site.name != "site-packages":
            continue
        if path.suffix != ".py":
            break
        relative = path.relative_to(site).as_posix()
        for record in sorted(site.glob("*.dist-info/RECORD")):
            if re.search(r"secret|credential|password|парол", record.parent.name, re.I):
                continue
            identity(record)
            with record.open(newline="") as stream:
                rows = list(csv.reader(stream))
            matches = [row for row in rows if len(row) == 3 and row[0] == relative]
            if len(matches) == 1 and re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", matches[0][1]) and matches[0][2] == str(snapshot[2]):
                return {"path": str(path), "identity": snapshot, "origin": {
                    "kind": "python-record", "record": str(record), "member": relative,
                    "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest()}}
        raise ValueError("Модуль не подтверждён записью установленного Python-пакета")
    if any(RESERVED.match(part) for part in path.parts[:-1]):
        raise ValueError("Исходник находится в каталоге защищённых данных")
    if RESERVED.match(path.name):
        # credentials.py пакета отличается от пользовательского файла авторизации с тем же расширением.
        if not path.name.startswith("secret_"):
            raise ValueError("Файл авторизации нельзя разрешить как исходник")
    environment = {"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for repo in path.parents:
        if not (repo / ".git").exists():
            continue
        relative = path.relative_to(repo).as_posix()
        result = subprocess.run(["git", "ls-files", "--stage", "-z", "--", relative], cwd=repo,
                                env=environment, capture_output=True, check=True, timeout=15)
        records = [row for row in result.stdout.split(b"\0") if row]
        if len(records) == 1:
            header, name = records[0].split(b"\t", 1)
            fields = header.split()
            if fields[0] in {b"100644", b"100755"} and fields[2] == b"0" and os.fsdecode(name) == relative:
                return {"path": str(path), "identity": snapshot, "origin": {
                    "kind": "git-index", "repo": str(repo), "member": relative, "blob": fields[1].decode()}}
        break
    raise ValueError("Модуль не подтверждён Git index или метаданными установленного пакета")


def valid_snapshot(value):
    valid = (isinstance(value, dict) and set(value) == {"path", "identity", "origin"}
            and isinstance(value["path"], str) and Path(value["path"]).is_absolute()
            and isinstance(value["identity"], list) and len(value["identity"]) == 6
            and all(type(item) is int and item >= 0 for item in value["identity"])
            and isinstance(value["origin"], dict))
    if not valid:
        return False
    origin = value['origin']
    kind = origin.get('kind')
    keys = {'kind', 'repo', 'member', 'blob'} if kind == 'git-index' else {'kind', 'record', 'member', 'record_sha256'}
    return (kind in {'git-index', 'python-record'} and set(origin) == keys
            and all(isinstance(item, str) for item in origin.values())
            and Path(origin.get('repo', origin.get('record', ''))).is_absolute()
            and not Path(origin['member']).is_absolute() and '..' not in Path(origin['member']).parts)


def unchanged(value, provenance=False):
    if not valid_snapshot(value):
        return False
    try:
        if identity(value["path"]) != value["identity"]:
            return False
        return not provenance or approve(value["path"]) == value
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
