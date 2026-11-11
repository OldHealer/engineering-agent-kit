#!/usr/bin/env python3
"""Проверки в отдельной штатной песочнице с фиктивными файлами, без изменения сессии."""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import tomllib

from permissions import NAME, project_overrides, render_profile, toml_value


def probe_thread_start(project, arguments, environment, timeout=60, executable="codex", pass_fds=()):
    """Проверить AGENTS/bootstrap через настоящий thread/start, без turn/start и вызова модели."""
    process = subprocess.Popen([str(executable), *arguments, "app-server", "--stdio"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=environment, cwd=project, bufsize=0, pass_fds=pass_fds)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": b"", "stderr": b""}

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    def receive(identifier):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                channel = key.data
                buffers[channel] = (buffers[channel] + chunk)[-1024 * 1024:]
                if channel == "stdout":
                    while b"\n" in buffers[channel]:
                        line, buffers[channel] = buffers[channel].split(b"\n", 1)
                        reply = json.loads(line)
                        if reply.get("id") == identifier:
                            return reply
            if process.poll() is not None:
                break
        return {}

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "kit-startup-check", "version": "1"}, "capabilities": {"experimentalApi": True}}})
        initialized = receive(1)
        if "result" not in initialized:
            return {"initialized": False, "thread_started": False, "model_turn_started": False}
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {
            "cwd": str(project), "permissions": NAME, "ephemeral": True}})
        reply = receive(2)
        message = reply.get("error", {}).get("message", "")
        return {"initialized": True, "thread_started": "result" in reply,
                "model_turn_started": False, "ephemeral": True,
                "error_code": reply.get("error", {}).get("code"),
                "unreadable_scan_error": "ripgrep unreadable glob scan failed" in message,
                "argument_limit": "maximum number of arguments" in message or "Argument list too long" in message,
                "error_summary": re.sub(r'/[^\s:,]+', '<path>', re.sub(r'"[^"\n]*"|`[^`\n]*`', '<value>', message))[-1800:]}
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def overrides(table):
    result = []
    for key, value in table.items():
        result += ["-c", key + "=" + toml_value(value)]
    return result


def port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def execute(command, timeout=35):
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        return 124, error.stdout or b"", error.stderr or b""


def run_checks(network=False, installed=False):
    with tempfile.TemporaryDirectory(prefix="kit-permission-check-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / ".engineering-kit/tmp").mkdir(parents=True)
        private = root / "private"
        private.mkdir()
        (project / "source.txt").write_text("fixture source\n")
        (project / ".env.fixture").write_text("DUMMY=not-a-real-secret\n")
        (private / "canary.txt").write_text("not-a-real-secret\n")
        (project / ".env.link").symlink_to(private / "canary.txt")
        (private / "directory").mkdir()
        (private / "directory/canary.txt").write_text("not-a-real-secret\n")
        (project / ".env.directory").symlink_to(private / "directory", target_is_directory=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=Kit fixture", "-c",
                        "user.email=kit-fixture@example.invalid", "commit", "-q", "-m", "fixture"], check=True)
        worktree = project / ".engineering-kit/workspaces/fixture/repo"
        subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "--detach", str(worktree), "HEAD"], check=True)
        initial = tomllib.loads(render_profile())
        # Символьный псевдоним проверяем и по реальному пути; секретные файлы пользователя не открываем.
        profile = (["--profile", NAME] if installed else overrides(initial)) + project_overrides(project)
        http_port, socks_port = port(), port()
        profile += ["-c", f'permissions.{NAME}.network.proxy_url="http://127.0.0.1:{http_port}"',
                    "-c", f'permissions.{NAME}.network.socks_url="http://127.0.0.1:{socks_port}"']
        prefix = ["codex", *profile, "sandbox", "--permission-profile", NAME,
                  "--include-managed-config", "--cd", str(project), "--"]
        code = r'''
import json, os
from pathlib import Path
project = Path.cwd()
outside = project.parent / 'outside.txt'
def readable(path):
    try:
        with path.open('rb') as stream:
            stream.read(1)
        return True
    except OSError:
        return False
def writable(path):
    try:
        path.write_text('fixture write')
        return True
    except OSError:
        return False
print(json.dumps({
    'source_read': readable(project/'source.txt'),
    'workspace_write': writable(project/'allowed.txt'),
    'outside_write_blocked': not writable(outside),
    'temp_write': writable(project/'.engineering-kit/tmp/allowed.txt'),
    'env_read_blocked': not readable(project/'.env.fixture'),
    'symlink_read_blocked': not readable(project/'.env.link'),
    'real_target_read_blocked': not readable(project.parent/'private/canary.txt'),
    'symlink_directory_blocked': not readable(project/'.env.directory/canary.txt'),
    'real_directory_blocked': not readable(project.parent/'private/directory/canary.txt'),
    'git_metadata_write_blocked': not writable(project/'.git/kit-canary'),
    'worktree_write': writable(project/'.engineering-kit/workspaces/fixture/repo/allowed.txt'),
    'worktree_git_pointer_blocked': not writable(project/'.engineering-kit/workspaces/fixture/repo/.git'),
    'proxy_in_environment': bool(os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')),
}))
'''
        status, stdout, stderr = execute(prefix + [sys.executable, "-c", code])
        if status:
            # Не выводим конфигурацию или окружение; диагностика только для известных условий.
            message = "sandbox_failed"
            if b"app-server socket directory" in stderr:
                message = "outer_environment_socket_restriction"
            elif b"Operation not permitted" in stderr:
                message = "outer_environment_restriction"
            diagnostic = stderr.decode(errors="replace")
            diagnostic = re.sub(r'"[^"\n]*"', '"<value>"', diagnostic)
            diagnostic = re.sub(r"/[^\s:,]+", lambda match: "<path>/" + Path(match.group()).name, diagnostic)
            return {"sandbox": message, "exit_code": status, "diagnostic": diagnostic[-1600:]}
        try:
            report = {"sandbox": "ok", "filesystem": json.loads(stdout)}
        except (ValueError, UnicodeError):
            return {"sandbox": "unexpected_output", "exit_code": status}
        # Реальный Git работает, но запись в защищённую .git не маскируется под успех.
        git_status, _, _ = execute(prefix + ["git", "status", "--porcelain"])
        git_write, _, _ = execute(prefix + ["git", "update-ref", "refs/heads/kit-probe", "HEAD"])
        report["git"] = {"local_read": git_status == 0, "metadata_update_succeeded": git_write == 0,
                         "authentication": "not_attempted"}
        if network:
            report["network"] = {}
            for label, url in (("github_https", "https://github.com/"),
                               ("github_api", "https://api.github.com/"),
                               ("denied_host", "https://example.com/"),
                               ("denied_private_literal", "https://127.0.0.1/")):
                status, stdout, stderr = execute(prefix + ["curl", "-q", "--silent", "--show-error", "--head",
                    "--max-time", "15", "--output", "/dev/null", "--write-out", "%{http_code}", url])
                http_status = stdout.decode(errors="replace").strip()
                denied = b"403" in stderr or b"denied" in stderr.lower() or b"blocked" in stderr.lower()
                report["network"][label] = {"exit_code": status, "http_status": http_status if http_status.isdigit() else None,
                                            "proxy_denial_observed": denied}
            direct_code = '''import socket,json
try:
    stream=socket.create_connection(('github.com',443),timeout=5)
    stream.close()
    print(json.dumps({'blocked':False}))
except OSError as error:
    print(json.dumps({'blocked':True,'errno':error.errno}))
'''
            status, stdout, _ = execute(prefix + [sys.executable, "-c", direct_code])
            report["network"]["direct_bypass"] = json.loads(stdout) if status == 0 else {"blocked": None}
            git_env_code = r'''
import os, subprocess, json
env = dict(os.environ)
env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0')
result = subprocess.run(['git','-c','credential.helper=','ls-remote','https://github.com/openai/codex.git','HEAD'],
                        env=env, capture_output=True, timeout=20)
print(json.dumps({'exit_code':result.returncode,'head_received':bool(result.stdout.strip())}))
'''
            status, stdout, _ = execute(prefix + [sys.executable, "-c", git_env_code], timeout=30)
            report["git"]["public_https_read"] = json.loads(stdout) if status == 0 else {"exit_code": status}
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--installed", action="store_true", help="Проверить установленный именованный профиль вместо шаблона")
    args = parser.parse_args()
    report = run_checks(args.network, args.installed)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("sandbox") != "ok":
        return 1
    passed = all(report["filesystem"].values()) and report["git"]["local_read"]
    passed = passed and not report["git"]["metadata_update_succeeded"]
    if args.network:
        network = report["network"]
        for label in ("allowed_https", "github_https", "github_api"):
            probe = network[label]
            passed = passed and probe["exit_code"] == 0 and probe["http_status"] not in {None, "000"}
        passed = passed and network["denied_host"]["proxy_denial_observed"]
        passed = passed and network["denied_host"]["exit_code"] != 0
        passed = passed and network["denied_private_literal"]["exit_code"] != 0
        passed = passed and network["direct_bypass"].get("blocked") is True
        passed = passed and report["git"]["public_https_read"].get("head_received") is True
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
