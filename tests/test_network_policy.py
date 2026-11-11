import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import autonomy
import check_permissions
import network_policy
import permissions


class NetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kit-network-policy-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.first = self.root / 'project-a'
        self.second = self.root / 'project-b'
        self.first.mkdir()
        self.second.mkdir()

    def apply(self, **options):
        preview = autonomy.configure(self.home, **options)
        return autonomy.configure(self.home, **options, confirmation_token=preview['confirmation_token'])

    def test_old_settings_remain_compatible_without_network_migration(self):
        self.apply()
        self.assertNotIn('network', autonomy.load(self.home))
        self.assertEqual(autonomy.configure(self.home)['files'], [])
        self.assertEqual(network_policy.effective(None, self.first, {'fixture.example': 'deny'}),
                         {'fixture.example': 'deny'})

    def test_shared_public_registries_and_project_only_private_registry(self):
        self.apply(network_profiles=['python', 'node'])
        self.apply(network_project=self.first, network_allow=['registry.corporate.example'])
        settings = autonomy.load(self.home)['network']
        for project in (self.first, self.second):
            effective = network_policy.effective(settings, project)
            self.assertEqual(effective['files.pythonhosted.org'], 'allow')
            self.assertEqual(effective['registry.npmjs.org'], 'allow')
        self.assertNotIn('registry.corporate.example', network_policy.effective(settings, self.second))
        worktree = self.first / '.engineering-kit/workspaces/task/repo'
        worktree.mkdir(parents=True)
        self.assertEqual(network_policy.effective(settings, worktree)['registry.corporate.example'], 'allow')
        sibling = self.root / 'project-a-copy'
        sibling.mkdir()
        self.assertNotIn('registry.corporate.example', network_policy.effective(settings, sibling))
        self.assertEqual(autonomy.load(self.home)['local_commit'], 'confirm')
        self.assertEqual(autonomy.load(self.home)['locked_dependencies'], 'confirm')

    def test_explicit_denies_win_over_project_allow_and_registry_profiles(self):
        settings = network_policy.update(deny=['registry.npmjs.org'])
        settings = network_policy.update(settings, self.first, profiles=['node'])
        rules = network_policy.effective(settings, self.first, {'*.corporate.example': 'deny'})
        self.assertEqual(network_policy.access('registry.npmjs.org', rules), 'deny')
        self.assertEqual(network_policy.access('private.corporate.example', rules), 'deny')
        settings = network_policy.update(settings, self.first, allow=['private.corporate.example'])
        self.assertEqual(network_policy.access('private.corporate.example',
            network_policy.effective(settings, self.first, {'*.corporate.example': 'deny'})), 'deny')
        self.assertEqual(network_policy.access('corporate.example', {'**.corporate.example': 'deny'}), 'deny')

    def test_revocation_is_explicit_scoped_and_preview_token_changes(self):
        self.apply(network_profiles=['python', 'node'])
        self.apply(network_project=self.first, network_allow=['private.example'])
        before = (self.home / autonomy.SETTINGS).read_bytes()
        preview = autonomy.configure(self.home, network_remove=['registry.npmjs.org'])
        self.assertEqual((self.home / autonomy.SETTINGS).read_bytes(), before)
        self.assertNotIn('registry.npmjs.org', preview['network']['after'])
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(self.home, network_remove=['pypi.org'], confirmation_token=preview['confirmation_token'])
        self.assertEqual((self.home / autonomy.SETTINGS).read_bytes(), before)
        autonomy.configure(self.home, network_remove=['registry.npmjs.org'], confirmation_token=preview['confirmation_token'])
        self.assertEqual(network_policy.effective(autonomy.load(self.home)['network'], self.first)['private.example'], 'allow')
        self.assertEqual(autonomy.configure(self.home, network_remove=['registry.npmjs.org'])['files'], [])

    def test_invalid_domains_and_ambiguous_scopes_are_rejected_without_leaking_input(self):
        for host in ['*', '*.example.com', 'https://user:fixture@example.com/path', 'example.com:443',
                     '127.0.0.1', '127.1', '0177.0.0.1', '0x7f.0.0.1', 'localhost', 'example.com\n', '-bad.example', None]:
            with self.subTest(host=host), self.assertRaises(network_policy.NetworkPolicyError):
                network_policy.domain(host)
        self.assertEqual(network_policy.domain('PYPI.ORG'), 'pypi.org')
        with self.assertRaises(autonomy.PolicyError):
            autonomy.configure(self.home, network_project=self.first, local_commit='auto')
        with self.assertRaises(network_policy.NetworkPolicyError):
            network_policy.update(allow=['example.com'], deny=['example.com'])
        for malformed in ([], '', False):
            with self.assertRaises(network_policy.NetworkPolicyError):
                network_policy.effective(malformed)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_permission_can_be_revoked_after_project_directory_was_removed(self):
        self.apply(network_project=self.first, network_allow=['private.example'])
        self.first.rmdir()
        self.apply(network_project=self.first, network_remove=['private.example'])
        configured = autonomy.load(self.home)['network']['projects'][str(self.first)]
        self.assertEqual(configured, {'allow': [], 'deny': []})
        with self.assertRaises(network_policy.NetworkPolicyError):
            self.apply(network_project=self.first, network_allow=['private.example'])

    def test_discovery_uses_only_manifests_and_never_grants_or_exposes_url_auth(self):
        (self.first / 'pyproject.toml').write_text('[project]\nname="fixture"\nversion="1"\n')
        (self.first / 'package-lock.json').write_text(json.dumps({'packages': {
            'a': {'resolved': 'https://private.example/packages/a.tgz'},
            'b': {'resolved': 'https://user:SYNTHETIC_PASSWORD@auth.example/a.tgz'},
            'c': {'resolved': 'https://query.example/a.tgz?token=SYNTHETIC_TOKEN'}}}))
        (self.first / 'package.json').write_text(json.dumps({'scripts': {'postinstall': 'https://not-a-dependency.example/'},
                                                            'homepage': 'https://not-a-registry.example/'}))
        target = self.root / 'private-fixture'
        target.write_text('SYNTHETIC_PRIVATE_FILE')
        (self.first / 'uv.lock').symlink_to(target)
        for directory in ('node_modules/a', '.worktrees/old', 'secrets', '.engineering-kit/workspaces/other', '.ssh', '.codex'):
            path = self.first / directory
            path.mkdir(parents=True)
            (path / 'package.json').write_text('invalid ignored content')
        self.apply(network_profiles=['python'])
        before = (self.home / autonomy.SETTINGS).read_bytes()
        result = network_policy.inspect(self.first, autonomy.load(self.home)['network'])
        self.assertEqual(result['missing_domains'], ['private.example', 'registry.npmjs.org'])
        self.assertFalse(result['automatic_grants'])
        self.assertEqual((self.home / autonomy.SETTINGS).read_bytes(), before)
        serialized = json.dumps(result)
        for forbidden in ('SYNTHETIC_PASSWORD', 'SYNTHETIC_TOKEN', 'SYNTHETIC_PRIVATE_FILE',
                          'not-a-dependency.example', 'not-a-registry.example', 'auth.example', 'query.example'):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(any(item['kind'] == 'symlink_skipped' for item in result['discovery']['issues']))

    def test_inspection_reports_explicit_denial_without_suggesting_an_override(self):
        (self.first / 'package.json').write_text('{}')
        result = network_policy.inspect(self.first, network_policy.update(profiles=['node'], deny=['registry.npmjs.org']))
        self.assertEqual(result['missing_domains'], [])
        self.assertEqual(result['denied_domains'], ['registry.npmjs.org'])

    def test_malformed_manifest_structure_and_limits_do_not_break_inspection(self):
        (self.first / 'pyproject.toml').write_text('tool="invalid table"\n')
        (self.first / 'package.json').write_text('[]')
        result = network_policy.discover(self.first, max_depth=1)
        self.assertEqual(result['manifests'], [])
        self.assertEqual(len(result['issues']), 2)
        self.assertEqual(result['limits']['max_depth'], 1)
        for index in range(network_policy.MAX_MANIFESTS + 1):
            path = self.second / str(index)
            path.mkdir()
            (path / 'package.json').write_text('invalid JSON')
        result = network_policy.discover(self.second)
        self.assertEqual(result['issues'][-1]['kind'], 'manifest_limit')
        self.assertEqual(len(result['issues']), network_policy.MAX_MANIFESTS + 1)

    def test_cli_preview_apply_and_inspection_preserve_unrelated_settings(self):
        config = self.home / 'config.toml'
        config.write_text('[fixture]\nvalue="preserved"\n')
        command = [sys.executable, str(permissions.KIT / 'scripts/launch.py')]
        options = ['policy', '--codex-dir', str(self.home), '--network-profile', 'node']
        preview = subprocess.run([*command, *options], capture_output=True, text=True, check=True)
        token = json.loads(preview.stdout)['confirmation_token']
        self.assertFalse((self.home / autonomy.SETTINGS).exists())
        subprocess.run([*command, *options, '--apply', token], capture_output=True, text=True, check=True)
        result = subprocess.run([*command, 'network', '--codex-dir', str(self.home), '--project', str(self.second)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['domains']['registry.npmjs.org'], 'allow')
        self.assertEqual(tomllib.loads(config.read_text())['fixture'], {'value': 'preserved'})

    def test_native_codex_accepts_merged_domains_without_changing_filesystem_or_approvals(self):
        initial = tomllib.loads(permissions.render_profile())
        initial['mcp_servers'] = {}
        policy = network_policy.update(profiles=['python', 'node'])
        policy = network_policy.update(policy, self.first, allow=['private.example'])
        original = permissions.project_overrides(self.first)
        arguments = permissions.project_overrides(self.first, network_settings=policy,
            installed_domains={'fixture.example': 'deny', 'github.com': 'allow'})
        def setting(values, prefix):
            return [value[len(prefix):] for value in values if value.startswith(prefix)][-1]
        key = 'permissions.' + permissions.NAME + '.filesystem='
        self.assertEqual(setting(original, key), setting(arguments, key))
        domains = tomllib.loads('value='+setting(arguments, 'permissions.'+permissions.NAME+'.network.domains='))['value']
        self.assertEqual(domains['fixture.example'], 'deny')
        self.assertEqual(domains['private.example'], 'allow')
        self.assertIn('approval_policy="on-request"', arguments)
        self.assertIn('features.network_proxy=true', arguments)
        checked = subprocess.run(['codex', *check_permissions.overrides(initial), *arguments,
                                  'app-server', '--strict-config', '--print-startup-config'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 0, 'Codex rejected merged network configuration')
        self.assertIn('configRequirements', json.loads(checked.stdout))


if __name__ == '__main__':
    unittest.main()
