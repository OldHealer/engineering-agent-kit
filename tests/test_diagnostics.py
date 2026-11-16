import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import diagnostics
import permissions


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kit-diagnostics-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        self.home = self.root/'codex'
        self.home.mkdir()

    def test_qa_prompt_only_for_grant_and_renew_without_base_mutation(self):
        self.assertEqual(permissions.qa_session_overrides(self.project, {}), [])
        base = {'mcp_servers': {'kit-qa': {'command': shutil.which('node'),
                'args': [str(permissions.KIT/'scripts/mcp-server.mjs'), 'qa', '--ai-box', '/example/ai-box'], 'enabled': False}}}
        before = str(base)
        args = permissions.qa_session_overrides(self.project, base)
        scoped = tomllib.loads('entry='+args[1].split('=', 1)[1])['entry']
        self.assertTrue(scoped['enabled'])
        for action in ('grant', 'renew'):
            self.assertEqual(scoped['tools']['kit_qa_'+action]['approval_mode'], 'prompt')
        for action in ('start', 'stop', 'restart', 'check', 'cleanup'):
            self.assertEqual(scoped['tools']['kit_qa_'+action]['approval_mode'], 'approve')
        self.assertEqual(str(base), before)

    def test_missing_cwd_is_reported_without_recreation(self):
        missing = self.project/'removed'
        result = diagnostics.inspect(missing, self.home)
        self.assertEqual(result['checks'][0]['code'], 'PROJECT_MISSING')
        self.assertFalse(missing.exists())

    def test_legacy_configuration_is_reported_without_modification(self):
        config = self.home/'config.toml'
        before = 'sandbox_mode="workspace-write"\n'
        config.write_text(before)
        result = diagnostics.inspect(self.project, self.home)
        self.assertTrue(any(item['code'] == 'LEGACY_SANDBOX' for item in result['checks']))
        self.assertFalse(result['ready'])
        self.assertEqual(config.read_text(), before)

    def test_deleted_worktree_registry_is_preserved(self):
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.project, env=env, check=True, capture_output=True)
        git('init', '-q', '-b', 'main')
        (self.project/'source.txt').write_text('fixture')
        git('add', 'source.txt')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        target = self.project/'.worktrees/old'
        git('worktree', 'add', '-q', '--detach', str(target), 'HEAD')
        moved = self.project/'moved-without-git'
        target.rename(moved)
        before = git('worktree', 'list', '--porcelain', '-z').stdout
        result = diagnostics.inspect(self.project, self.home)
        self.assertTrue(any(item['code'] == 'STALE_WORKTREE' and item['path'] == str(target) for item in result['checks']))
        self.assertEqual(git('worktree', 'list', '--porcelain', '-z').stdout, before)
        self.assertTrue(moved.exists())


if __name__ == '__main__':
    unittest.main()
