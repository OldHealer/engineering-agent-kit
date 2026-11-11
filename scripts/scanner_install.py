"""Предпросмотр и атомарная установка host-protected scanner; содержимое данных не читается."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


SOURCE = Path(__file__).resolve().parents[1] / "bin/permission-scan/system-rg"
DIRECTORY = Path("/usr/local/libexec/engineering-agent-kit/permission-scan")
DESTINATION = DIRECTORY / "rg"


class ScannerInstallError(ValueError):
    pass


def metadata(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "gid": info.st_gid,
            "mode": info.st_mode, "links": info.st_nlink}


def parent_state():
    state = {}
    for path in reversed(DESTINATION.parents):
        info = metadata(path)
        if info is not None and (not stat.S_ISDIR(info["mode"]) or info["uid"] != 0 or info["mode"] & 0o022):
            raise ScannerInstallError(f"Родитель системного scanner не защищён: {path}; чужие права не изменены")
        state[str(path)] = info
    return state


def plan(source=SOURCE):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ScannerInstallError("Источник scanner должен быть обычным файлом kit")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    parents = parent_state()
    before = metadata(DESTINATION)
    if before is not None:
        if (not stat.S_ISREG(before["mode"]) or before["uid"] != 0 or before["gid"] != 0
                or stat.S_IMODE(before["mode"]) != 0o755
                or before["mode"] & 0o6000 or before["links"] != 1):
            raise ScannerInstallError("Системный путь scanner занят неподходящим объектом; перезаписи нет")
        if hashlib.sha256(DESTINATION.read_bytes()).hexdigest() != digest:
            raise ScannerInstallError("Системный scanner отличается; автоматическая перезапись чужой версии запрещена")
    state = {"source_sha256": digest, "installer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "destination": str(DESTINATION), "before": before, "parents": parents}
    token = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return {"confirmation_token": token, "destination": str(DESTINATION), "source_sha256": digest,
            "create_directories": [path for path, value in parents.items() if value is None],
            "create_file": before is None, "owner": "root:root", "mode": "0755", "setuid": False,
            "service": False, "existing_files_overwritten": False, "applied": False}


def installed():
    if not DESTINATION.exists() and not DESTINATION.is_symlink():
        return False
    return not plan()["create_file"]


def set_owner_and_mode(path, uid, gid):
    os.chown(path, uid, gid)
    path.chmod(0o755)


def apply(token):
    preview = plan()
    if token != preview["confirmation_token"]:
        raise ScannerInstallError("Источник или целевые пути изменились; нужен новый предпросмотр")
    if os.geteuid() != 0:
        raise ScannerInstallError("Установка root-owned scanner требует однократного административного доступа")
    if preview["create_file"]:
        for value in preview["create_directories"]:
            path = Path(value)
            path.mkdir(mode=0o755)
            set_owner_and_mode(path, 0, 0)
        parent_state()
        # В root-owned родителе непривилегированный процесс не может заменить временный inode.
        with tempfile.NamedTemporaryFile(dir=DIRECTORY, prefix=".kit-scanner-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(SOURCE.read_bytes())
        try:
            set_owner_and_mode(temporary, 0, 0)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != preview["source_sha256"]:
                raise ScannerInstallError("Источник изменился во время установки; scanner не опубликован")
            os.link(temporary, DESTINATION)  # Не перезаписывает цель, появившуюся после preview.
        finally:
            temporary.unlink()
    preview["applied"] = True
    return preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", metavar="TOKEN")
    args = parser.parse_args()
    try:
        result = apply(args.apply) if args.apply else plan()
    except (ScannerInstallError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
