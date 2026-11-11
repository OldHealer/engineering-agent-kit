import base64
import hashlib
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
import autonomy
import check_permissions
import codex_runtime
import file_access
import permissions


class FileAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kit-file-access-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / '.engineering-kit/tmp').mkdir(parents=True)
        self.repo = self.project / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.modules = [self.repo / name for name in [
            'app/services/secret_encryption_service.py',
            'src/components/secretFormControl/secretFormControlState.ts',
            'src/components/secretFormControl/secretFormControlState.test.ts']]
        for path in self.modules:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC_MODULE\n')
        subprocess.run(['git', 'add', '--', *[str(path.relative_to(self.repo)) for path in self.modules]],
                       cwd=self.repo, check=True)
        self.site = self.repo / '.venv/lib/python3.11/site-packages'
        dependency = self.site / 'redis/credentials.py'
        dependency.parent.mkdir(parents=True)
        dependency.write_text('SYNTHETIC_MODULE\n')
        record = self.site / 'redis-1.0.dist-info/RECORD'
        record.parent.mkdir()
        checksum = base64.urlsafe_b64encode(hashlib.sha256(b'SYNTHETIC_MODULE\n').digest()).rstrip(b'=').decode()
        record.write_text(f'redis/credentials.py,sha256={checksum},{len("SYNTHETIC_MODULE") + 1}\n')
        self.modules.append(dependency)
        self.protected = [self.repo / name for name in [
            '.env.fixture', 'private.key', 'auth.json', 'credentials.py', 'secrets.ts',
            'app/services/runtime_secret.py', '.venv/lib/python3.11/site-packages/redis/secret_data.py']]
        for path in self.protected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC_PRIVATE_DATA\n')
        self.vault = self.root / 'obsidian'
        self.vault.mkdir()
        for name in ['Password Matrix.md', 'SECRET.py', 'CrEdEnTiAl.json', 'ПАРОЛИ.md', 'pAsSwOrD/ordinary.md']:
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC_PRIVATE_DATA\n')
            self.protected.append(path)
        self.approvals = [file_access.approve(path) for path in self.modules]

    def probe(self, approvals=(), extra_denied=(), installed=None, check_notes=False):
        profile = tomllib.loads(permissions.render_profile())
        profile['features']['network_proxy'] = False
        profile['permissions'][permissions.NAME]['network']['enabled'] = False
        arguments = check_permissions.overrides(profile)
        arguments += permissions.project_overrides(self.project, code_modules=approvals,
            artifact_roots=[str(self.vault)], extra_denied=extra_denied, installed_filesystem=installed)
        arguments += ['-c', 'features.network_proxy=false']
        environment = permissions.launch_environment(os.environ, arguments)
        code = '''import json,sys
from pathlib import Path
modules,protected,vault,notes=json.loads(sys.argv[1])
def readable(name):
    try:
        Path(name).read_bytes()
        return True
    except OSError:
        return False
def writable(name):
    try:
        Path(name).write_text('SYNTHETIC_WRITE')
        return True
    except OSError:
        return False
result={'modules':[readable(name) for name in modules],
        'protected':[readable(name) for name in protected]}
if notes:
    note=Path(vault)/'ordinary.md'
    with note.open('x') as stream:
        stream.write('created')
    assert note.read_text()=='created'
    with note.open('a') as stream:
        stream.write(' appended')
    result['note']=note.read_text()=='created appended'
    result['module_writes_denied']=all(not writable(name) for name in modules)
print(json.dumps(result))
'''
        with codex_runtime.scanner_runtime(Path(shutil.which('codex')).resolve(), permissions.SCAN_BIN / 'rg',
                environment, self.root / 'cache', self.project) as runtime:
            current = arguments + permissions.runtime_overrides(arguments, runtime)
            result = subprocess.run([str(runtime.executable), *current, 'sandbox', '--permission-profile',
                permissions.NAME, '--include-managed-config', '--cd', str(self.project), '--', '/usr/bin/python3',
                '-I', '-c', code, json.dumps([[str(p) for p in self.modules], [str(p) for p in self.protected],
                                            str(self.vault), check_notes])],
                capture_output=True, text=True, env=environment, pass_fds=runtime.pass_fds, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_native_policy_allows_only_approved_modules_and_keeps_vault_private(self):
        before = self.probe()
        self.assertEqual(before['modules'], [False] * 4)
        self.assertFalse(any(before['protected']))
        after = self.probe(self.approvals, check_notes=True)
        self.assertEqual(after['modules'], [True] * 4)
        self.assertFalse(any(after['protected']))
        self.assertTrue(after['note'])
        self.assertTrue(after['module_writes_denied'])

    def test_explicit_denies_and_unmarked_user_glob_win_over_module_permissions(self):
        denied = self.probe(self.approvals, extra_denied=[self.modules[0]])
        self.assertFalse(denied['modules'][0])
        denied = self.probe(self.approvals, installed={str(self.modules[0]): 'deny'})
        self.assertFalse(denied['modules'][0])
        denied = self.probe(self.approvals, installed={':workspace_roots': {'**/*secret*': 'deny'}})
        self.assertEqual(denied['modules'][:3], [False] * 3)
        self.assertFalse(any(denied['protected']))

    def test_replaced_module_and_source_symlink_do_not_inherit_the_permission(self):
        path = self.modules[0]
        path.unlink()
        path.symlink_to(self.protected[1])
        with self.assertRaises(ValueError):
            file_access.approve(path)
        result = self.probe(self.approvals)
        self.assertFalse(result['modules'][0])
        self.assertFalse(any(result['protected']))
        path.unlink()
        path.write_text('SYNTHETIC_REPLACEMENT\n')
        result = self.probe(self.approvals)
        self.assertFalse(result['modules'][0])

    def test_private_data_cannot_be_approved_by_changing_its_extension(self):
        for path in [self.repo / 'credentials.py', self.repo / 'secrets.ts', self.modules[-1].with_name('secret_data.py')]:
            with self.assertRaises(ValueError):
                file_access.approve(path)

    def test_same_policy_supports_an_actual_managed_worktree_and_original_dependencies(self):
        subprocess.run(['git', '-c', 'user.name=Kit fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture modules'], cwd=self.repo, check=True)
        worktree = self.project / '.engineering-kit/workspaces/fixture/repo'
        worktree.parent.mkdir(parents=True)
        subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(worktree), 'HEAD'], cwd=self.repo, check=True)
        source = worktree / 'app/services/secret_encryption_service.py'
        self.modules.append(source)
        result = self.probe([*self.approvals, file_access.approve(source)])
        self.assertTrue(all(result['modules']))
        self.assertFalse(any(result['protected']))

    def test_settings_preview_is_bound_to_module_metadata_and_preserves_custom_rules(self):
        home = self.root / 'codex'
        home.mkdir()
        original = permissions.render_profile()
        for old, new in permissions.LEGACY_CODE_GLOBS.items():
            original = original.replace(permissions.quote(new), permissions.quote(old))
        original += '\n[permissions.kit-restricted.filesystem."/fixture/custom"]\n"**/*secret*"="deny"\n'
        target = home / 'kit-restricted.config.toml'
        target.write_text(original)
        preview = autonomy.configure(home, code_modules=[str(self.modules[0])])
        self.assertEqual(target.read_text(), original)
        self.modules[0].write_text('SYNTHETIC_UPDATED_MODULE\n')
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(home, code_modules=[str(self.modules[0])], confirmation_token=preview['confirmation_token'])
        preview = autonomy.configure(home, code_modules=[str(self.modules[0])])
        applied = autonomy.configure(home, code_modules=[str(self.modules[0])], confirmation_token=preview['confirmation_token'])
        self.assertTrue(applied['applied'])
        filesystem = tomllib.loads(target.read_text())['permissions'][permissions.NAME]['filesystem']
        self.assertEqual(filesystem['/fixture/custom']['**/*secret*'], 'deny')
        self.assertTrue(file_access.unchanged(autonomy.load(home)['code_modules'][0], provenance=True))


if __name__ == '__main__':
    unittest.main()
