#!/usr/bin/env python3
"""Проверить кандидаты Git без открытия игнорируемых локальных настроек."""

from pathlib import Path
import re
import subprocess
import sys


NAME = re.compile(r'password|credential|secret|парол', re.I)
CONTENT = [re.compile(pattern) for pattern in (
    r'gh[pousr]_[A-Za-z0-9_]{20,}', r'github_pat_[A-Za-z0-9_]{20,}',
    r'AKIA[0-9A-Z]{16}', r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
)]
AUTH_URL = re.compile(r'https?://[^\s/\'"]+:[^\s/\'"]+@')
SYNTHETIC_URLS = {'tests/test_network_policy.py': (
    'https://' + 'user:fixture@example.com/path',
    'https://' + 'user:SYNTHETIC_PASSWORD@auth.example/a.tgz',
)}


def check(root):
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=root, capture_output=True, check=True)
    files = sorted(set(item.decode() for item in result.stdout.split(b'\0') if item))
    findings = []
    for name in files:
        path = root/name
        if (any(NAME.search(part) or part.startswith('.env') for part in Path(name).parts)
                or Path(name).suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.crt', '.cer'}
                or '.engineering-kit' in Path(name).parts or name in {'config/local.json', 'config/connections.env'}):
            findings.append((name, 'protected filename; content not read'))
            continue
        if path.is_symlink() or not path.is_file():
            findings.append((name, 'symlink or unavailable file; content not read'))
            continue
        data = path.read_bytes()
        if b'\0' in data:
            findings.append((name, 'binary requires manual review'))
            continue
        text = data.decode('utf-8', errors='replace')
        auth_text = text
        for fixture in SYNTHETIC_URLS.get(name, ()):
            auth_text = auth_text.replace(fixture, '')
        if any(pattern.search(text) for pattern in CONTENT) or AUTH_URL.search(auth_text):
            findings.append((name, 'possible embedded authentication; value hidden'))
    return files, findings


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    files, findings = check(root)
    for name, reason in findings:
        print(f'{name}: {reason}')
    print(f'Publication check: {len(files)} files, {len(findings)} findings; ignored files not read')
    sys.exit(bool(findings))
