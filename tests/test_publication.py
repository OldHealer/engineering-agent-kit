from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_publication import check


class PublicationTests(unittest.TestCase):
    def test_ignored_settings_are_not_candidates_and_public_examples_are_allowed(self):
        with tempfile.TemporaryDirectory(prefix='kit-publication-check-') as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'.gitignore').write_text('.engineering-kit/\n')
            (root/'.engineering-kit').mkdir()
            (root/'.engineering-kit/local.json').write_text('invalid synthetic local config')
            (root/'example.txt').write_text('GITHUB_TOKEN=<YOUR_API_KEY>')
            files, findings = check(root)
            self.assertNotIn('.engineering-kit/local.json', files)
            self.assertEqual(findings, [])

    def test_tracked_protected_names_and_embedded_auth_are_reported_without_values(self):
        with tempfile.TemporaryDirectory(prefix='kit-publication-check-') as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'example.key').write_text('synthetic fixture')
            (root/'source.txt').write_text('https://' + 'fixture:dummy@example.test/path')
            _, findings = check(root)
            self.assertEqual(len(findings), 2)
            self.assertNotIn('dummy', str(findings))
