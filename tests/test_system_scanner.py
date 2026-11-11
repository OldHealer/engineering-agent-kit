import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_permissions
import codex_runtime
import permissions
import scanner_install


SOURCE = permissions.SCAN_BIN / 'system-rg'


class SystemScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kit-system-scanner-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        (self.project / '.engineering-kit/tmp').mkdir(parents=True)
        self.closed = self.project / 'deploy/database'
        self.closed.mkdir(parents=True)
        self.closed.chmod(0)
        self.addCleanup(self.closed.chmod, 0o700)
        (self.project / '.env.fixture').write_text('SYNTHETIC_PRIVATE\n')
        self.arguments = ['--no-config', '--files', '--hidden', '--no-ignore', '--null',
                          '--max-depth', '8', '--glob', '**/.env*', '--', str(self.project)]

    def scan(self, arguments=None):
        return subprocess.run(['/usr/bin/python3', '-I', str(SOURCE), *(arguments or self.arguments)],
                              capture_output=True, timeout=20)

    def test_unreadable_tree_is_a_deny_match_and_readable_secret_is_not_lost(self):
        raw = subprocess.run(['/usr/bin/rg', *self.arguments], capture_output=True, timeout=20)
        self.assertEqual(raw.returncode, 2)
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stderr)
        matches = set(result.stdout.split(b'\0')) - {b''}
        self.assertEqual(matches, {os.fsencode(self.closed), os.fsencode(self.project / '.env.fixture')})
        self.assertEqual(self.closed.stat().st_mode & 0o777, 0)

    def test_unknown_error_and_unknown_protocol_keep_ripgrep_failure(self):
        invalid = [*self.arguments]
        invalid[invalid.index('**/.env*')] = '['
        result = self.scan(invalid)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr)
        result = self.scan(['--files', '--hidden', '--no-ignore', '--null', '--', str(self.project)])
        self.assertEqual(result.returncode, 2)

    def test_entire_unreadable_scan_root_is_returned_as_deny(self):
        arguments = [*self.arguments]
        arguments[-1] = str(self.closed)
        result = self.scan(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, os.fsencode(self.closed) + b'\0')

    def test_native_sandbox_keeps_closed_tree_locked_even_if_created_after_profile(self):
        initial = tomllib.loads(permissions.render_profile())
        initial['features']['network_proxy'] = False
        initial['permissions'][permissions.NAME]['network']['enabled'] = False
        arguments = check_permissions.overrides(initial) + permissions.project_overrides(self.project)
        arguments += ['-c', 'features.network_proxy=false']
        late = self.project / 'new-volume'
        late.mkdir()
        late.chmod(0)
        self.addCleanup(late.chmod, 0o700)
        environment = permissions.launch_environment(os.environ, arguments)
        with codex_runtime.scanner_runtime(Path(shutil.which('codex')).resolve(), SOURCE,
                environment, self.root / 'runtime', self.project) as runtime:
            arguments += permissions.runtime_overrides(arguments, runtime)
            scanner_path = str(runtime.directory / 'codex-path') + os.pathsep + environment['PATH']
            environment['PATH'] = scanner_path
            arguments += ['-c', 'shell_environment_policy.set.PATH=' + permissions.quote(scanner_path)]
            code = '''import json
from pathlib import Path
root=Path.cwd()
def hidden(path):
    try:
        path.read_bytes()
        return False
    except OSError:
        return True
def locked(path):
    try:
        path.chmod(0o700)
        return False
    except OSError:
        return True
(root/'allowed.txt').write_text('fixture')
print(json.dumps({'secret_denied':hidden(root/'.env.fixture'),
    'known_volume_locked':locked(root/'deploy/database'),
    'new_volume_locked':locked(root/'new-volume')}))
'''
            result = subprocess.run([str(runtime.executable), *arguments, 'sandbox', '--permission-profile',
                permissions.NAME, '--include-managed-config', '--cd', str(self.project), '--', '/usr/bin/python3', '-I',
                '-c', code], capture_output=True, text=True, env=environment, pass_fds=runtime.pass_fds, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertTrue(all(json.loads(result.stdout).values()), result.stdout)

    def test_install_preview_is_read_only_and_bound_to_source_contents(self):
        before = scanner_install.metadata(scanner_install.DESTINATION)
        first = scanner_install.plan()
        candidate = self.root / 'public-scanner'
        candidate.write_bytes(SOURCE.read_bytes() + b'\n# fixture candidate\n')
        if before is None:
            other = scanner_install.plan(source=candidate)
            self.assertNotEqual(first['confirmation_token'], other['confirmation_token'])
        else:
            with self.assertRaises(scanner_install.ScannerInstallError):
                scanner_install.plan(source=candidate)
        self.assertEqual(scanner_install.metadata(scanner_install.DESTINATION), before)
        self.assertEqual(first['owner'], 'root:root')
        self.assertFalse(first['setuid'])
        self.assertFalse(first['service'])

    def test_installer_sets_ownership_and_mode_on_real_directory_and_file(self):
        directory = self.root / 'install-directory'
        directory.mkdir(mode=0o700)
        executable = directory / 'rg'
        executable.write_bytes(SOURCE.read_bytes())
        executable.chmod(0o600)
        for path in (directory, executable):
            scanner_install.set_owner_and_mode(path, os.getuid(), os.getgid())
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (os.getuid(), os.getgid()))
            self.assertEqual(info.st_mode & 0o7777, 0o755)
        self.assertEqual(executable.read_bytes(), SOURCE.read_bytes())


if __name__ == '__main__':
    unittest.main()
