import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import local_settings


class LocalSettingsTests(unittest.TestCase):
    def test_local_configuration_is_explicit_and_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory(prefix='kit-local-config-') as directory:
            path = Path(directory)/'local.json'
            self.assertEqual(local_settings.load(path), {})
            value = {'ai_box': '/example/ai-box', 'network_hosts': ['forge.example.test'],
                     'mcp_hosts': {'git': ['forge.example.test']}}
            path.write_text(json.dumps(value))
            self.assertEqual(local_settings.load(path), value)
            for invalid in ({'token': 'synthetic'}, {'network_hosts': ['https://forge.example.test']},
                            {'mcp_hosts': {'unknown': []}}, {'ai_box': 'relative'}):
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    local_settings.load(path)
            path.unlink()
            path.symlink_to(Path(directory)/'absent')
            # Даже отсутствующая цель не должна превращать symlink в допустимый конфиг.
            with self.assertRaises(ValueError):
                local_settings.load(path)

    def test_clean_installation_uses_no_corporate_defaults_in_python_or_node(self):
        with tempfile.TemporaryDirectory(prefix='kit-clean-settings-') as directory:
            scripts = Path(directory)/'scripts'
            scripts.mkdir()
            for name in ('local_settings.py', 'network_policy.py', 'mcp-runtime.mjs'):
                shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts'/name, scripts/name)
            python = subprocess.run([sys.executable, '-c',
                'import local_settings,json; print(json.dumps([local_settings.network_hosts(),local_settings.mcp_hosts("jira")]))'],
                cwd=scripts, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(python.stdout), [['api.github.com', 'github.com'], []])
            node = subprocess.run(['node', '--input-type=module', '-e',
                'import {allowedHosts} from "./mcp-runtime.mjs"; console.log(JSON.stringify([allowedHosts("git"),allowedHosts("jira")]))'],
                cwd=scripts, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(node.stdout), ['github.com,api.github.com', ''])
