import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
codex_runtime = importlib.import_module("codex_runtime")
permissions = importlib.import_module("permissions")
checks = importlib.import_module("check_permissions")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-runtime-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.cache = self.root / "cache"
        self.native = Path(shutil.which("codex")).resolve()
        self.adapter = permissions.SCAN_BIN / "rg"

    def runtime(self, environment):
        return codex_runtime.scanner_runtime(self.native, self.adapter, environment, self.cache, self.project)

    def environment(self):
        return {"PATH": os.environ["PATH"], "HOME": str(Path.home()),
                "KIT_SCAN_DENIED_DIRS": "[]", "KIT_SCAN_RG": shutil.which("rg", path=os.defpath)}

    def test_cleanup_preserves_live_sessions_foreign_files_and_original_installation(self):
        original = self.native.stat()
        with self.runtime(self.environment()) as first:
            directory = first.directory
            self.assertEqual(first.executable.stat().st_ino, original.st_ino)
            self.assertEqual(first.executable.stat().st_dev, original.st_dev)
            self.assertFalse(codex_runtime.cleanup(directory))
            foreign = self.cache / "session-foreign"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("fixture")
            with self.runtime(self.environment()) as second:
                self.assertTrue(directory.is_dir())
                self.assertNotEqual(second.directory, directory)
            self.assertFalse(second.directory.exists())
            self.assertTrue(foreign.is_dir())
        self.assertFalse(directory.exists())
        self.assertEqual(self.native.stat().st_mode, original.st_mode)
        self.assertTrue((self.native.parent.parent / "codex-resources").is_dir())

    def test_interrupted_context_and_stale_runtime_are_cleaned(self):
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with self.runtime(self.environment()) as runtime:
                directory = runtime.directory
                raise ValueError("fixture failure")
        self.assertFalse(directory.exists())
        stale = self.cache / "session-stale"
        stale.mkdir()
        (stale / ".lease").touch()
        (stale / ".kit-runtime").write_text(codex_runtime.MARKER)
        target = self.root / "keep.txt"
        target.write_text("fixture")
        (stale / "link").symlink_to(target)
        with self.runtime(self.environment()):
            self.assertFalse(stale.exists())
        self.assertEqual(target.read_text(), "fixture")

    def test_helper_processes_keep_the_runtime_executable_directory(self):
        with self.runtime(self.environment()) as runtime:
            for name in ("codex-exec-server", "codex-code-mode-host"):
                helper = runtime.directory / "bin" / name
                child = subprocess.Popen([str(helper), "--listen", "stdio"],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=self.environment())
                try:
                    self.assertEqual(Path(os.readlink(f"/proc/{child.pid}/exe")), helper)
                    source = self.native.parent / name
                    self.assertEqual(helper.stat().st_ino, source.stat().st_ino)
                    self.assertEqual(helper.stat().st_dev, source.stat().st_dev)
                finally:
                    child.terminate()
                    child.wait(timeout=10)
                    child.stdin.close()

    def test_no_runtime_without_closed_directories_and_no_symlinked_cache(self):
        with self.runtime({"PATH": os.defpath}) as runtime:
            self.assertIsNone(runtime.directory)
            self.assertFalse(self.cache.exists())
        self.cache.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            with self.runtime(self.environment()):
                self.fail("Symlinked runtime cache was accepted")

    def test_inherited_lease_prevents_cleanup_until_child_exits(self):
        with self.runtime(self.environment()) as runtime:
            directory = runtime.directory
            child = subprocess.Popen(["/usr/bin/python3", "-I", "-c", "input()"],
                                     stdin=subprocess.PIPE, pass_fds=runtime.pass_fds)
        try:
            self.assertTrue(directory.exists())
            self.assertFalse(codex_runtime.cleanup(directory))
        finally:
            child.communicate(b"\n", timeout=10)
        self.assertTrue(codex_runtime.cleanup(directory))
        self.assertFalse(directory.exists())

    def test_next_launch_without_adapter_cleans_only_finished_runtime(self):
        with self.runtime(self.environment()) as runtime:
            stale = self.cache / "session-stale"
            stale.mkdir()
            (stale / ".lease").touch()
            (stale / ".kit-runtime").write_text(codex_runtime.MARKER)
            with self.runtime({"PATH": os.defpath}) as plain:
                self.assertIsNone(plain.directory)
                self.assertFalse(stale.exists())
                self.assertTrue(runtime.directory.exists())

    def test_attached_process_preserves_exit_status_and_cleans_runtime(self):
        for code, expected in (("raise SystemExit(7)", 7), ("import os,signal; os.kill(os.getpid(),signal.SIGTERM)", 143)):
            with self.runtime(self.environment()) as runtime:
                status = codex_runtime.run_attached(["/usr/bin/python3", "-I", "-c", code], self.environment(),
                                                     runtime.pass_fds, runtime.directory)
                self.assertEqual(status, expected)
            self.assertFalse(runtime.directory.exists())

    def test_cleanup_keeps_a_running_owner_even_without_inherited_descriptors(self):
        with self.runtime(self.environment()) as runtime:
            directory = runtime.directory
            child = subprocess.Popen(["/usr/bin/python3", "-I", "-c", "input()"], stdin=subprocess.PIPE)
            (directory / "owner.json").write_text(json.dumps({"pid": child.pid}))
        try:
            self.assertTrue(directory.exists())
            self.assertFalse(codex_runtime.cleanup(directory))
        finally:
            child.communicate(b"\n", timeout=10)
        self.assertTrue(codex_runtime.cleanup(directory))

    def test_real_thread_start_and_sandbox_preserve_secret_and_runtime_protection(self):
        (self.project / ".engineering-kit/tmp").mkdir(parents=True)
        (self.project / "source.txt").write_text("fixture source")
        (self.project / ".env.fixture").write_text("DUMMY=fixture-only")
        blocked = self.project / "deployment/volume"
        blocked.mkdir(parents=True)
        blocked.chmod(0)
        try:
            initial = tomllib.loads(permissions.render_profile())
            initial["features"]["network_proxy"] = False
            initial["permissions"][permissions.NAME]["network"]["enabled"] = False
            arguments = checks.overrides(initial) + permissions.project_overrides(self.project)
            arguments += ["-c", "features.network_proxy=false"]
            for name in ("kit-bitbucket", "kit-jira", "kit-confluence", "kit-github", "kit-git"):
                arguments += ["-c", f"mcp_servers.{name}.enabled=false"]
            environment = permissions.launch_environment(os.environ, arguments)
            with self.runtime(environment) as runtime:
                arguments += permissions.runtime_overrides(arguments, runtime)
                report = checks.probe_thread_start(self.project, arguments, environment,
                                                   executable=runtime.executable, pass_fds=runtime.pass_fds)
                self.assertTrue(report["thread_started"], report)
                self.assertFalse(report["model_turn_started"])
                (self.project / "late").mkdir()
                (self.project / "late/.env.late").write_text("DUMMY=fixture-only")
                canary = runtime.directory / "canary.txt"
                canary.write_text("fixture")
                code = '''import json,sys
from pathlib import Path
root=Path.cwd()
def read_denied(path):
    try:
        path.read_bytes()
        return False
    except OSError:
        return True
def write_denied(path):
    try:
        path.write_text('fixture write')
        return False
    except OSError:
        return True
def locked_volume():
    try:
        (root/'deployment/volume').chmod(0o700)
        return False
    except OSError:
        return True
(root/'allowed.txt').write_text('fixture')
print(json.dumps({'source_read':(root/'source.txt').read_text()=='fixture source',
    'secret_denied':read_denied(root/'.env.fixture'),
    'late_secret_denied':read_denied(root/'late/.env.late'),
    'runtime_write_denied':write_denied(Path(sys.argv[1])),
    'scanner_policy_write_denied':write_denied(Path(sys.argv[2])),
    'volume_cannot_be_unlocked':locked_volume(),
    'outside_write_denied':write_denied(root.parent/'outside.txt')}))
'''
                result = subprocess.run([str(runtime.executable), *arguments, "sandbox", "--permission-profile",
                    permissions.NAME, "--include-managed-config", "--cd", str(self.project), "--",
                    "/usr/bin/python3", "-I", "-c", code, str(canary), str(runtime.directory / "codex-path/scan-policy.json")],
                    capture_output=True, text=True,
                    env=environment, pass_fds=runtime.pass_fds, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                self.assertTrue(all(json.loads(result.stdout).values()), result.stdout)
                self.assertEqual(canary.read_text(), "fixture")
                self.assertEqual(blocked.stat().st_mode & 0o777, 0)
            self.assertFalse(runtime.directory.exists())
        finally:
            blocked.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
