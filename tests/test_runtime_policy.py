import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import autonomy
import codex_runtime
import permissions


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kit-runtime-policy-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root/'codex-home'
        self.bundle = self.home/'kit-native/fixture'
        self.executable = self.bundle/'bin/codex'
        self.project = self.root/'project'
        self.project.mkdir()
        for name in codex_runtime.RUNTIME_FILES:
            path = self.bundle/name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == 'codex-package.json':
                path.write_text('{"layoutVersion":1}\n')
            else:
                # Реальные ELF-копии нужны для проверки файлового снимка; их не запускаем как Codex.
                shutil.copyfile('/usr/bin/true', path)
                path.chmod(0o755)

    def apply(self, **options):
        preview = autonomy.configure(self.home, **options)
        return autonomy.configure(self.home, **options, confirmation_token=preview['confirmation_token'])

    def test_selection_requires_preview_and_preserves_global_network_and_modes(self):
        self.apply(network_profiles=['python'])
        before = (self.home/autonomy.SETTINGS).read_bytes()
        preview = autonomy.configure(self.home, runtime_executable=self.executable)
        self.assertEqual((self.home/autonomy.SETTINGS).read_bytes(), before)
        autonomy.configure(self.home, runtime_executable=self.executable, confirmation_token=preview['confirmation_token'])
        settings = autonomy.load(self.home)
        self.assertEqual(codex_runtime.approved_runtime(settings['runtime'], self.home), self.executable)
        self.assertIn('pypi.org', settings['network']['allow'])
        self.assertEqual(settings['local_commit'], 'confirm')
        self.assertEqual(autonomy.configure(self.home, runtime_executable=self.executable)['files'], [])

    def test_changed_helper_invalidates_preview_and_prevents_launch_selection(self):
        self.apply()
        preview = autonomy.configure(self.home, runtime_executable=self.executable)
        helper = self.bundle/'bin/codex-exec-server'
        with helper.open('ab') as stream:
            stream.write(b'changed fixture')
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(self.home, runtime_executable=self.executable, confirmation_token=preview['confirmation_token'])
        self.assertNotIn('runtime', autonomy.load(self.home))
        self.apply(runtime_executable=self.executable)
        snapshot = autonomy.load(self.home)['runtime']
        replacement = helper.with_suffix('.replacement')
        replacement.write_bytes(helper.read_bytes())
        replacement.chmod(0o755)
        replacement.replace(helper)
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approved_runtime(snapshot, self.home)

    def test_session_hard_links_do_not_revoke_approved_runtime(self):
        snapshot = codex_runtime.approve_runtime(self.executable, self.home)
        os.link(self.executable, self.root/'session-hard-link')
        self.assertEqual(codex_runtime.approved_runtime(snapshot, self.home), self.executable)

    def test_symlink_in_bundle_and_unmanaged_location_are_rejected(self):
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approve_runtime(Path('/usr/bin/true'), self.home)
        helper = self.bundle/'bin/codex-code-mode-host'
        helper.unlink()
        helper.symlink_to('/usr/bin/true')
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approve_runtime(self.executable, self.home)
        helper.unlink()
        shutil.copyfile('/usr/bin/true', helper)
        helper.chmod(0o755)
        (self.bundle/'codex-package.json').write_text('[]')
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approve_runtime(self.executable, self.home)
        (self.bundle/'codex-package.json').write_text('{"layoutVersion":1}')
        codex_runtime.approve_runtime(self.executable, self.home)
        self.bundle.chmod(0o777)
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approve_runtime(self.executable, self.home)

    def test_runtime_can_be_revoked_when_bundle_is_missing_without_removing_network_policy(self):
        self.apply(network_profiles=['node'], runtime_executable=self.executable)
        snapshot = autonomy.load(self.home)['runtime']
        self.executable.unlink()
        with self.assertRaises(codex_runtime.ScannerRuntimeError):
            codex_runtime.approved_runtime(snapshot, self.home)
        self.apply(clear_runtime=True)
        settings = autonomy.load(self.home)
        self.assertNotIn('runtime', settings)
        self.assertEqual(settings['network']['allow'], ['registry.npmjs.org'])
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(self.home, network_project=self.project, clear_runtime=True)

    def test_resume_uses_native_parser_and_rejects_option_injection(self):
        executable = shutil.which('codex')
        for options in ({'session_id': str(uuid.uuid4())}, {'last': True}, {}):
            command = permissions.session_command(executable, self.project, [], resume=True, isolated_backend=True, **options)
            result = subprocess.run([*command, '--help'], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr[-700:])
            self.assertIn('resume', result.stdout.lower())
        start = permissions.session_command(executable, self.project, [], isolated_backend=True)
        result = subprocess.run([*start, '--help'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr[-700:])
        result = subprocess.run([sys.executable, str(permissions.KIT/'scripts/launch.py'), 'resume', '--',
                                 '--dangerously-bypass-approvals-and-sandbox'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UUID', result.stderr)
        self.assertFalse(result.stdout)


if __name__ == '__main__':
    unittest.main()
