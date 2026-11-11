import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import shutil
import sys
import tempfile
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/permissions.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("kit_permissions", SCRIPT)
permissions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(permissions)


def native_config_write(home, target, project, arguments=()):
    environment = permissions.launch_environment(os.environ)
    environment["CODEX_HOME"] = str(home)
    process = subprocess.Popen(["codex", *arguments, "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=environment, bufsize=0)

    def request(identifier, method, params):
        process.stdin.write((json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        while select.select([process.stdout], [], [], 15)[0]:
            line = process.stdout.readline()
            if not line:
                break
            reply = json.loads(line)
            if reply.get("id") == identifier:
                return reply
        return {}

    try:
        initialized = request(1, "initialize", {"clientInfo": {"name": "kit-config-check", "version": "1"},
                                                "capabilities": {"experimentalApi": True}})
        if "result" not in initialized:
            return initialized
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        return request(2, "config/batchWrite", {"filePath": str(target), "edits": [{
            "keyPath": "projects", "mergeStrategy": "upsert", "value": {str(project): {"trust_level": "trusted"}}}]})
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


class PermissionTests(unittest.TestCase):
    def legacy_profile(self):
        return "\n".join(line for line in permissions.render_profile().splitlines()
                         if not line.startswith(("command = ", "args = "))) + "\n"

    def registered_servers(self):
        return {"mcp_servers": {"kit-" + service: permissions.configure_mcp.server_config(
            service, Path("/fixture/ai-box"), shutil.which("node"))
            for service in ("bitbucket", "github", "jira", "confluence")}}

    def test_profile_mcp_entries_have_standalone_transports(self):
        servers = tomllib.loads(permissions.render_profile())["mcp_servers"]
        for name, entry in servers.items():
            with self.subTest(server=name):
                self.assertIsInstance(entry["command"], str)
                self.assertEqual(entry["args"][:2], [str(permissions.KIT / "scripts/mcp-server.mjs"), name[4:]])

    def test_transport_repair_preserves_custom_settings_and_does_not_copy_environment(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            path = Path(directory) / "kit-restricted.config.toml"
            path.write_text(self.legacy_profile() + '\n# user setting\n[tui]\nnotifications = false\n'
                            '\n[mcp_servers.other]\ncommand = "fixture"\n')
            before = tomllib.loads(path.read_text())
            base = self.registered_servers()
            base["mcp_servers"]["kit-bitbucket"]["env"] = {"DUMMY_TOKEN": "fixture-only"}
            repaired = permissions.repair_profile_transport(path, base)
            self.assertEqual(len(repaired), 4)
            after = tomllib.loads(path.read_text())
            for name in repaired:
                self.assertEqual(after["mcp_servers"][name].pop("command"), base["mcp_servers"][name]["command"])
                self.assertEqual(after["mcp_servers"][name].pop("args"), base["mcp_servers"][name]["args"])
            self.assertEqual(after, before)
            self.assertIn("# user setting", path.read_text())
            self.assertNotIn("DUMMY_TOKEN", path.read_text())
            contents = path.read_bytes()
            self.assertEqual(permissions.repair_profile_transport(path, base), [])
            self.assertEqual(path.read_bytes(), contents)

    def test_transport_repair_refuses_unrecognized_launchers_without_partial_write(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            path = Path(directory) / "kit-restricted.config.toml"
            path.write_text(self.legacy_profile())
            before = path.read_bytes()
            base = self.registered_servers()
            base["mcp_servers"]["kit-jira"]["args"] += ["--unexpected", "fixture"]
            with self.assertRaises(permissions.ProfileError):
                permissions.repair_profile_transport(path, base)
            self.assertEqual(path.read_bytes(), before)
            path.write_text('[mcp_servers.kit-bitbucket.env]\nFIXTURE="fixture"\n')
            before = path.read_bytes()
            with self.assertRaises(permissions.ProfileError):
                permissions.repair_profile_transport(path, self.registered_servers())
            self.assertEqual(path.read_bytes(), before)

    def test_transport_repair_supports_an_explicit_table_at_end_of_file(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            path = Path(directory) / "kit-restricted.config.toml"
            path.write_text(permissions.MARKER + '\n[mcp_servers.kit-bitbucket]')
            self.assertEqual(permissions.repair_profile_transport(path, self.registered_servers()), ["kit-bitbucket"])
            self.assertIn("command", tomllib.loads(path.read_text())["mcp_servers"]["kit-bitbucket"])

    def test_native_trust_write_accepts_migrated_profile_and_preserves_restrictions(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            home = Path(directory)
            project = home / "project"
            project.mkdir()
            # config/batchWrite валидирует записываемый файл отдельно от CLI/base overlays.
            target = home / "config.toml"
            target.write_text(self.legacy_profile())
            before = tomllib.loads(target.read_text())
            arguments = ["-c", "features.network_proxy=false", "-c",
                         f"permissions.{permissions.NAME}.network.enabled=false"]
            for name in self.registered_servers()["mcp_servers"]:
                arguments += ["-c", f'mcp_servers.{name}.command="/usr/bin/true"', "-c", f'mcp_servers.{name}.enabled=false']
            refused = native_config_write(home, target, project, arguments)
            self.assertIn("invalid transport", refused.get("error", {}).get("message", ""))
            self.assertTrue(tomllib.loads(target.read_text()) == before, "Failed trust write changed the fixture")
            permissions.repair_profile_transport(target, self.registered_servers())
            accepted = native_config_write(home, target, project, arguments)
            self.assertTrue("result" in accepted, "Codex rejected trust persistence after transport repair")
            after = tomllib.loads(target.read_text())
            self.assertEqual(after["projects"][str(project)]["trust_level"], "trusted")
            self.assertEqual(after["permissions"], before["permissions"])
            self.assertEqual(after["approval_policy"], before["approval_policy"])
            for name, entry in before["mcp_servers"].items():
                for key, value in entry.items():
                    self.assertEqual(after["mcp_servers"][name][key], value)

    def test_empty_root_git_marker_does_not_hide_nested_repositories(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            project = Path(directory)
            (project / ".git").mkdir()
            repository = project / "backend"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
            blocked = project / "volume"
            blocked.mkdir()
            blocked.chmod(0)
            try:
                self.assertEqual(permissions.repository_roots(project), [repository])
                self.assertEqual(list((project / ".git").iterdir()), [])
            finally:
                blocked.chmod(0o700)

    def test_profile_uses_new_permissions_without_global_workspace_roots(self):
        data = tomllib.loads(permissions.render_profile())
        self.assertEqual(data["default_permissions"], "kit-restricted")
        self.assertNotIn("sandbox_mode", data)
        self.assertNotIn("sandbox_workspace_write", data)
        self.assertNotIn("workspace_roots", data["permissions"][permissions.NAME])
        self.assertTrue(data["features"]["network_proxy"])
        self.assertEqual(data["shell_environment_policy"]["inherit"], "none")
        self.assertEqual(data["mcp_servers"]["kit-jira"]["tools"]["jira_comment_create"]["approval_mode"], "prompt")

    def test_install_does_not_edit_base_or_existing_mcp_configuration(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            directory = Path(root)
            base = directory / "config.toml"
            content = '[mcp_servers.example]\ncommand = "example"\n'
            base.write_text(content)
            permissions.install(directory, include_rules=True)
            self.assertEqual(base.read_text(), content)
            self.assertEqual(permissions.install(directory, include_rules=True)["activated"], False)
            preview = permissions.rollback(directory)
            self.assertFalse(preview["disabled"])
            self.assertTrue((directory / "kit-restricted.config.toml").exists())
            permissions.rollback(directory, confirm=True)
            self.assertTrue((directory / "kit-restricted.config.toml.disabled").exists())
            self.assertEqual(base.read_text(), content)

    def test_symlinked_credentials_are_denied_at_the_real_location(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            root = Path(root)
            fake_home = root / "home"
            fake_home.mkdir()
            target = root / "private"
            target.mkdir()
            (fake_home / ".ssh").symlink_to(target, target_is_directory=True)
            paths = permissions.denied_paths(fake_home, root / "kit")
            self.assertIn(str(target), paths)
            self.assertIn(str(fake_home / ".ssh"), paths)

    def test_legacy_configuration_is_rejected_without_modifying_it(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            directory = Path(root)
            content = 'sandbox_mode = "workspace-write"\n'
            (directory / "config.toml").write_text(content)
            with self.assertRaises(ValueError):
                permissions.preflight(directory, directory)
            self.assertEqual((directory / "config.toml").read_text(), content)

    def test_sensitive_shell_set_variables_are_not_copied(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            directory = Path(root)
            (directory / "config.toml").write_text('[shell_environment_policy.set]\nDUMMY_TOKEN="fixture"\n')
            with self.assertRaises(ValueError):
                permissions.preflight(directory, directory)

    def test_project_configuration_cannot_silently_replace_permissions_or_mcp(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            directory = Path(root)
            (directory / ".codex").mkdir()
            path = directory / ".codex/config.toml"
            for content in ('default_permissions=":workspace"\n', '[mcp_servers.fixture]\ncommand="fixture"\n'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    permissions.preflight(directory, directory)

    def test_child_session_environment_has_no_tokens_or_credential_sockets(self):
        result = permissions.launch_environment({"PATH": "/usr/bin", "HOME": "/fixture", "USER": "fixture",
            "GITHUB_TOKEN": "fixture", "JIRA_PAT": "fixture", "OPENAI_API_KEY": "fixture",
            "SSH_AUTH_SOCK": "/fixture/agent", "HTTPS_PROXY": "http://fixture", "NODE_OPTIONS": "fixture"})
        self.assertEqual(set(result), {"PATH", "HOME", "USER"})

    def test_overlapping_denies_are_compacted_for_bubblewrap(self):
        result = permissions.compact_denies({"/fixture/private": "deny", "/fixture/private/key": "deny",
                                            "/fixture/source": "read"})
        self.assertNotIn("/fixture/private/key", result)
        self.assertEqual(result["/fixture/private"], "deny")

    def test_native_execpolicy_matches_sensitive_commands_without_running_them(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            path = Path(root) / "test.rules"
            path.write_text(permissions.render_rules([Path(root)]))
            for command in (["git", "push", "origin", "main"],
                            ["/usr/bin/git", "push", "origin", "main"],
                            ["git", "-c", "core.hooksPath=/fixture", "push", "origin", "main"],
                            ["git", "-C", root, "push", "origin", "main"],
                            ["/usr/bin/git", "-C", root, "push"],
                            ["bash", "-lc", "git push origin main"],
                            ["curl", "-X", "POST", "https://example.invalid"],
                            ["terraform", "apply"], ["kubectl", "delete", "pod", "fixture"]):
                result = subprocess.run(["codex", "execpolicy", "check", "--rules", str(path), "--", *command],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout)["decision"], "prompt", command)
            result = subprocess.run(["codex", "execpolicy", "check", "--rules", str(path), "--", "git", "status"],
                                    capture_output=True, text=True, check=True)
            self.assertNotEqual(json.loads(result.stdout).get("decision"), "prompt")

    def test_native_codex_accepts_project_scoped_git_configuration(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as root:
            base = {"developer_instructions": "Preserve fixture guidance", "mcp_servers": {"kit-git": {
                "command": shutil.which("node"), "args": [str(permissions.KIT / "scripts/mcp-server.mjs"), "git"],
                "enabled": False}}}
            arguments = permissions.git_session_overrides(Path(root), base)
            result = subprocess.run(["codex", *arguments, "app-server", "--strict-config", "--print-startup-config"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "Codex rejected the generated session configuration")
            self.assertIn("configRequirements", json.loads(result.stdout))

    def test_unreadable_volume_is_denied_without_breaking_native_glob_scan(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            project = Path(directory)
            blocked = project / "deployment/database-volume"
            blocked.mkdir(parents=True)
            (project / ".engineering-kit/tmp").mkdir(parents=True)
            (project / "source.txt").write_text("fixture source")
            (project / ".env.fixture").write_text("DUMMY=fixture-only")
            blocked.chmod(0)
            try:
                initial = tomllib.loads(permissions.render_profile())
                initial["features"]["network_proxy"] = False
                initial["permissions"][permissions.NAME]["network"]["enabled"] = False
                arguments = []
                for key, value in initial.items():
                    arguments += ["-c", key + "=" + permissions.toml_value(value)]
                arguments += permissions.project_overrides(project)
                environment = permissions.launch_environment(os.environ, arguments)
                # Создано после настройки профиля: штатные globs должны найти новый секрет.
                (project / "new-directory").mkdir()
                (project / "new-directory/.env.late").write_text("DUMMY=fixture-only")
                code = '''from pathlib import Path
import json
def denied(path):
    try:
        path.read_bytes()
        return False
    except OSError:
        return True
root=Path.cwd()
try:
    (root/'deployment/database-volume').chmod(0o700)
    volume_locked=False
except OSError:
    volume_locked=True
print(json.dumps({'source': (root/'source.txt').read_text() == 'fixture source',
                  'env_denied':denied(root/'.env.fixture'),
                  'late_env_denied':denied(root/'new-directory/.env.late'),
                  'volume_cannot_be_unlocked':volume_locked}))
'''
                result = subprocess.run(["codex", *arguments, "sandbox", "--permission-profile", permissions.NAME,
                    "--include-managed-config", "--cd", str(project), "--", "/usr/bin/python3", "-I", "-c", code],
                    capture_output=True, text=True, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                self.assertTrue(all(json.loads(result.stdout).values()))
                self.assertEqual(blocked.stat().st_mode & 0o777, 0)
                unexpected = project / "unlisted-volume"
                unexpected.mkdir()
                unexpected.chmod(0)
                try:
                    late_code = '''from pathlib import Path
try:
    Path('unlisted-volume').chmod(0o700)
    print('unlocked')
except OSError:
    print('closed')
'''
                    checked = subprocess.run(["codex", *arguments, "sandbox", "--permission-profile", permissions.NAME,
                        "--include-managed-config", "--cd", str(project), "--", "/usr/bin/python3", "-I", "-c", late_code],
                        capture_output=True, text=True, env=environment)
                    if permissions.scanner_install.installed():
                        self.assertEqual(checked.returncode, 0, checked.stderr[-1500:])
                        self.assertEqual(checked.stdout.strip(), "closed")
                        self.assertEqual(unexpected.stat().st_mode & 0o777, 0)
                    else:
                        self.assertNotEqual(checked.returncode, 0)
                        self.assertIn("ripgrep unreadable glob scan failed", checked.stderr)
                finally:
                    unexpected.chmod(0o700)
            finally:
                blocked.chmod(0o700)

    def test_foreign_worktrees_are_masked_but_kit_workspaces_remain_usable(self):
        with tempfile.TemporaryDirectory(prefix="kit-permission-test-") as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
            (project / "source.txt").write_text("fixture")
            subprocess.run(["git", "-C", str(project), "add", "source.txt"], check=True)
            subprocess.run(["git", "-C", str(project), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-q", "-m", "fixture"], check=True)
            foreign = project / ".worktrees/old-task"
            managed = project / ".engineering-kit/workspaces/current/repo"
            for path in (foreign, managed):
                subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "--detach", str(path), "HEAD"], check=True)
            (project / ".engineering-kit/tmp").mkdir(parents=True)
            initial = tomllib.loads(permissions.render_profile())
            initial["features"]["network_proxy"] = False
            initial["permissions"][permissions.NAME]["network"]["enabled"] = False
            arguments = []
            for key, value in initial.items():
                arguments += ["-c", key + "=" + permissions.toml_value(value)]
            arguments += permissions.project_overrides(project)
            environment = permissions.launch_environment(os.environ, arguments)
            self.assertIn(str(foreign.parent), json.loads(environment["KIT_SCAN_DENIED_DIRS"]))
            self.assertNotIn(str(foreign / ".git"), "\n".join(arguments))
            code = '''import json
from pathlib import Path
root=Path.cwd()
try:
    (root/'.worktrees/old-task/source.txt').read_text()
    foreign_denied=False
except OSError:
    foreign_denied=True
managed=root/'.engineering-kit/workspaces/current/repo'
(managed/'allowed.txt').write_text('fixture')
try:
    (managed/'.git').write_text('refused')
    metadata_denied=False
except OSError:
    metadata_denied=True
print(json.dumps({'original':(root/'source.txt').read_text()=='fixture',
                  'foreign_denied':foreign_denied,'managed_read':(managed/'source.txt').read_text()=='fixture',
                  'managed_write':(managed/'allowed.txt').read_text()=='fixture','metadata_denied':metadata_denied}))
'''
            result = subprocess.run(["codex", *arguments, "sandbox", "--permission-profile", permissions.NAME,
                "--include-managed-config", "--cd", str(project), "--", "/usr/bin/python3", "-I", "-c", code],
                capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertTrue(all(json.loads(result.stdout).values()))
            self.assertEqual((foreign / "source.txt").read_text(), "fixture")
            self.assertIn(str(foreign), subprocess.run(["git", "-C", str(project), "worktree", "list", "--porcelain"],
                                                     capture_output=True, text=True, check=True).stdout)


if __name__ == "__main__":
    unittest.main()
