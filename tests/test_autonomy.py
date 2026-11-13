from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import autonomy
import permissions


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-autonomy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"

    def apply(self, **options):
        preview = autonomy.configure(self.home, **options)
        return autonomy.configure(self.home, **options, confirmation_token=preview["confirmation_token"])

    def test_preview_creates_nothing_and_apply_is_idempotent(self):
        preview = autonomy.configure(self.home)
        self.assertFalse(self.home.exists())
        result = autonomy.configure(self.home, confirmation_token=preview["confirmation_token"])
        self.assertTrue(result["applied"])
        self.assertEqual(autonomy.configure(self.home)["files"], [])
        self.assertEqual(autonomy.load(self.home)["local_commit"], "confirm")
        self.assertFalse(result["auto_review_enabled"])

    def test_existing_settings_instructions_and_rules_are_preserved(self):
        self.home.mkdir()
        original = '# fixture\nmodel="fixture"\n[projects."/fixture"]\ntrust_level="trusted"\n'
        (self.home / "config.toml").write_text(original)
        instructions = "# User instructions\n\nKeep existing instructions.\n"
        (self.home / "AGENTS.md").write_text(instructions)
        rules = self.home / "rules/default.rules"
        rules.parent.mkdir()
        rules.write_text('prefix_rule(pattern=["fixture"], decision="forbidden")\n')
        previous_rules = rules.read_bytes()
        profile = self.home / "kit-restricted.config.toml"
        profile.write_text(permissions.render_profile() + '\n[tui]\nnotifications=false\n')
        before = tomllib.loads(profile.read_text())
        self.apply()
        after = tomllib.loads((self.home / "config.toml").read_text())
        for key, value in tomllib.loads(original).items():
            self.assertEqual(after[key], value)
        self.assertEqual(tomllib.loads(profile.read_text()), before)
        self.assertEqual(rules.read_bytes(), previous_rules)
        self.assertTrue((self.home / "AGENTS.md").read_text().startswith(instructions.rstrip()))

    def test_changed_configuration_invalidates_apply_without_partial_write(self):
        self.home.mkdir()
        config = self.home / "config.toml"
        config.write_text('model="fixture"\n')
        preview = autonomy.configure(self.home)
        config.write_text('model="changed-fixture"\n')
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(self.home, confirmation_token=preview["confirmation_token"])
        self.assertFalse((self.home / autonomy.SETTINGS).exists())
        self.assertFalse((self.home / "rules").exists())

    def test_unmanaged_rules_are_not_overwritten(self):
        path = self.home / "rules/kit-autonomy.rules"
        path.parent.mkdir(parents=True)
        path.write_text("# user file\n")
        with self.assertRaises(autonomy.PolicyError):
            self.apply()
        self.assertEqual(path.read_text(), "# user file\n")
        self.assertFalse((self.home / "config.toml").exists())

    def test_scalar_update_preserves_other_tables_and_rejects_ambiguous_changes(self):
        original = 'approval_policy="never"\n[other]\nvalue="fixture"\n'
        updated = autonomy.update_scalars(original, {"approval_policy": "on-request"})
        self.assertEqual(tomllib.loads(updated), {"approval_policy": "on-request", "other": {"value": "fixture"}})
        ambiguous = 'approval_policy={granular={rules=true}}\n'
        with self.assertRaises(autonomy.PolicyError):
            autonomy.update_scalars(ambiguous, {"approval_policy": "on-request"})

    def test_override_instructions_are_updated_without_touching_lower_priority_file(self):
        self.home.mkdir()
        (self.home / "AGENTS.md").write_text("# Lower priority fixture\n")
        (self.home / "AGENTS.override.md").write_text("# Higher priority fixture\n")
        self.apply()
        self.assertNotIn(autonomy.BEGIN, (self.home / "AGENTS.md").read_text())
        self.assertIn(autonomy.BEGIN, (self.home / "AGENTS.override.md").read_text())

    def test_symlinks_and_broad_artifact_roots_are_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("preserve fixture")
        self.home.mkdir()
        (self.home / "AGENTS.md").symlink_to(outside)
        with self.assertRaises(autonomy.PolicyError):
            self.apply()
        self.assertEqual(outside.read_text(), "preserve fixture")
        with self.assertRaises(autonomy.PolicyError):
            autonomy.validate_roots([str(self.root)], self.home)
        with self.assertRaises(autonomy.PolicyError):
            autonomy.validate_roots([str(autonomy.KIT)], self.home)

    def test_artifact_roots_and_modes_persist_and_replace_only_managed_instructions(self):
        artifacts = self.root / "obsidian/project"
        artifacts.mkdir(parents=True)
        self.apply(artifact_roots=[str(artifacts)], local_commit="auto", locked_dependencies="auto")
        settings = autonomy.load(self.home)
        self.assertEqual(settings["artifact_roots"], [str(artifacts)])
        self.assertEqual(settings["local_commit"], "auto")
        self.apply(local_commit="confirm")
        settings = autonomy.load(self.home)
        self.assertEqual(settings["artifact_roots"], [str(artifacts)])
        self.assertEqual(settings["locked_dependencies"], "auto")
        text = (self.home / "AGENTS.md").read_text()
        self.assertEqual(text.count(autonomy.BEGIN), 1)
        self.assertNotIn("Локальный commit: самостоятельно", text)

    def test_mcp_preparation_is_automatic_but_publication_and_cleanup_stay_manual(self):
        base = {"mcp_servers": {"kit-git": {"command": shutil.which("node"),
                "args": [str(permissions.KIT / "scripts/mcp-server.mjs"), "git"], "enabled": False}}}
        settings = {"local_commit": "confirm", "locked_dependencies": "confirm", "artifact_roots": []}
        args = permissions.git_session_overrides(self.root, base, autonomy_settings=settings)
        server = tomllib.loads('server=' + args[1].split('=', 1)[1])["server"]
        self.assertEqual(server["tools"]["kit_git_prepare"]["approval_mode"], "approve")
        for action in ("init", "configure", "commit", "push", "cleanup", "fetch", "merge", "merge_abort"):
            self.assertEqual(server["tools"]["kit_git_" + action]["approval_mode"], "prompt")
        settings["local_commit"] = "auto"
        args = permissions.git_session_overrides(self.root, base, autonomy_settings=settings)
        server = tomllib.loads('server=' + args[1].split('=', 1)[1])["server"]
        self.assertEqual(server["tools"]["kit_git_commit"]["approval_mode"], "approve")
        for action in ("push", "fetch", "merge", "merge_abort"):
            self.assertEqual(server["tools"]["kit_git_" + action]["approval_mode"], "prompt")

    def test_native_rules_require_risky_actions_without_blanket_interpreter_allow(self):
        self.apply()
        path = self.home / "rules/kit-autonomy.rules"
        risky = (["rm", "-rf", "/fixture"], ["/usr/bin/rm", "/fixture"],
                 ["git", "reset", "--hard"], ["git", "push", "--force"],
                 ["docker", "volume", "rm", "fixture"], ["docker", "compose", "down", "-v"],
                 ["alembic", "upgrade", "head"], ["psql", "-c", "DROP TABLE fixture"],
                 ["git", "commit", "-m", "fixture"], ["npm", "ci"], ["uv", "sync", "--locked"])
        routine = (["git", "status"], ["git", "diff"], ["rg", "fixture", "src"],
                   ["pytest", "tests"], ["npm", "run", "lint"], ["python3", "fixture.py"])
        for command in (*risky, *routine, *(["rtk", "proxy", *item] for item in (*risky, *routine))):
            with self.subTest(command=command):
                result = subprocess.run(["codex", "execpolicy", "check", "--rules", str(path), "--", *command],
                                        capture_output=True, text=True, check=True)
                data = json.loads(result.stdout)
                if command in risky or command[:2] == ["rtk", "proxy"] and command[2:] in risky:
                    self.assertEqual(data.get("decision"), "prompt")
                else:
                    self.assertIsNone(data.get("decision"))
        self.assertNotIn('decision = "allow"', path.read_text())

    def test_local_restore_mode_removes_only_restore_prompts_including_help_and_rtk(self):
        restore = (["uv", "sync", "--locked"], ["uv", "sync", "--help", "--no-config"],
                   ["npm", "ci"], ["npm", "ci", "--help"],
                   ["pnpm", "install", "--frozen-lockfile"], ["yarn", "install", "--immutable"])
        manual = (["uv", "add", "fixture"], ["uv", "pip", "install", "fixture"],
                  ["npm", "install", "fixture"], ["pnpm", "add", "fixture"], ["yarn", "add", "fixture"],
                  ["git", "commit", "-m", "fixture"], ["git", "push"],
                  ["docker", "compose", "up"], ["alembic", "upgrade", "head"], ["rm", "fixture"])
        for mode in ("confirm", "auto"):
            self.apply(local_commit="confirm", locked_dependencies=mode)
            path = self.home / "rules/kit-autonomy.rules"
            for command in (*restore, *manual):
                for wrapped in (command, ["rtk", "proxy", *command]):
                    with self.subTest(mode=mode, command=wrapped):
                        result = subprocess.run(["codex", "execpolicy", "check", "--rules", str(path), "--", *wrapped],
                                                capture_output=True, text=True, check=True)
                        decision = json.loads(result.stdout).get("decision")
                        self.assertEqual(decision, "prompt" if mode == "confirm" or command in manual else None)
            self.assertNotIn('decision = "allow"', path.read_text())
        settings = autonomy.load(self.home)
        self.assertEqual(settings["local_commit"], "confirm")
        self.assertEqual(settings["locked_dependencies"], "auto")
        config = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual(config["approvals_reviewer"], "user")
        self.assertEqual(config["approval_policy"], "on-request")

    def test_native_codex_accepts_the_installed_global_configuration(self):
        self.apply()
        result = subprocess.run(["codex", "app-server", "--strict-config", "--print-startup-config"],
                                env={**permissions.launch_environment(os.environ), "CODEX_HOME": str(self.home)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "Codex rejected fixture configuration; values hidden")
        self.assertIn("configRequirements", json.loads(result.stdout))

    def test_native_sandbox_writes_only_the_selected_artifact_directory(self):
        project = self.root / "project"
        (project / ".engineering-kit/tmp").mkdir(parents=True)
        artifacts = self.root / "obsidian/project"
        artifacts.mkdir(parents=True)
        other = self.root / "unrelated"
        other.mkdir()
        (artifacts / ".git").mkdir()
        (artifacts / ".git/config").write_text("fixture metadata")
        protected = []
        for name in ("Password Matrix.md", "password-matrix.txt", "PASSWORD_matrix.md", "Матрица паролей.md"):
            path = artifacts / name
            path.write_text("synthetic protected fixture")
            protected.append(path)
        secret_directory = artifacts / "Password Matrix"
        secret_directory.mkdir()
        (secret_directory / "nested.md").write_text("synthetic protected fixture")
        protected.append(secret_directory / "nested.md")
        deep = artifacts.joinpath(*["nested" + str(index) for index in range(10)], "Password Matrix.md")
        deep.parent.mkdir(parents=True)
        deep.write_text("synthetic protected fixture")
        protected.append(deep)
        target = self.root / "synthetic-private.md"
        target.write_text("synthetic protected fixture")
        (artifacts / "Password Matrix link.md").symlink_to(target)
        protected.extend((artifacts / "Password Matrix link.md", target))
        named_target = self.root / "Password Matrix outside.md"
        named_target.write_text("synthetic protected fixture")
        (artifacts / "ordinary-link.md").symlink_to(named_target)
        protected.extend((artifacts / "ordinary-link.md", named_target))
        initial = tomllib.loads(permissions.render_profile())
        initial["features"]["network_proxy"] = False
        initial["permissions"][permissions.NAME]["network"]["enabled"] = False
        arguments = []
        for key, value in initial.items():
            arguments += ["-c", key + "=" + permissions.toml_value(value)]
        arguments += permissions.project_overrides(project, artifact_roots=[str(artifacts)])
        arguments += ["-c", "features.network_proxy=false"]
        code = '''from pathlib import Path
import json
import sys
notes, other = map(Path, sys.argv[1:3])
protected = list(map(Path, sys.argv[3:]))
(notes/'report.md').write_text('fixture report')
def refused(path):
    try:
        path.write_text('refused')
        return False
    except OSError:
        return True
def unreadable(path):
    try:
        path.read_bytes()
        return False
    except OSError:
        return True
print(json.dumps({'artifact_write':(notes/'report.md').read_text()=='fixture report',
                  'other_write_denied':refused(other/'report.md'),
                  'metadata_write_denied':refused(notes/'.git/config'),
                  'protected_reads_denied':all(unreadable(path) for path in protected),
                  'protected_writes_denied':all(refused(path) for path in protected)}))
'''
        result = subprocess.run(["codex", *arguments, "sandbox", "--permission-profile", permissions.NAME,
                                "--include-managed-config", "--cd", str(project), "--", "/usr/bin/python3", "-I", "-c",
                                code, str(artifacts), str(other), *map(str, protected)], capture_output=True, text=True,
                                env=permissions.launch_environment(os.environ, arguments))
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertTrue(all(json.loads(result.stdout).values()))


if __name__ == "__main__":
    unittest.main()
