"""Read-only launch diagnostics. No trust, permission, cleanup or runtime changes."""

import os
from pathlib import Path
import shutil
import subprocess

import autonomy
import codex_runtime
import permissions


def inspect(project, directory=None):
    project = Path(project).absolute()
    directory = directory or permissions.codex_home()
    result = {'project': str(project), 'checks': [], 'ready': False, 'changes_applied': False}

    def check(code, status, detail, **fields):
        result['checks'].append({'code': code, 'status': status, 'detail': detail, **fields})

    if not project.is_dir():
        check('PROJECT_MISSING', 'blocked', 'Selected cwd is missing; restart from the original project path. No directory was recreated.')
        return result
    if project.resolve() != project:
        check('PROJECT_ALIAS', 'blocked', 'Select the real project path; symlinks are not silently followed.')
        return result
    try:
        permissions.preflight(project, directory)
        check('PREFLIGHT', 'passed', 'Configuration structure accepted')
    except (permissions.ProfileError, OSError, ValueError) as error:
        code = 'CONFIGURATION_BLOCKED'
        if isinstance(error, permissions.ProfileError) and 'sandbox' in str(error):
            code = 'LEGACY_SANDBOX'
        detail = str(error) if isinstance(error, permissions.ProfileError) else 'Configuration unreadable; values hidden'
        check(code, 'blocked', detail, next_step='Review migration preview; no settings changed')
    env = {'PATH': os.defpath, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_OPTIONAL_LOCKS': '0'}
    for repo in permissions.repository_roots(project):
        process = subprocess.run(['/usr/bin/git', 'worktree', 'list', '--porcelain', '-z'], cwd=repo,
                                 env=env, capture_output=True, timeout=15)
        if process.returncode:
            check('GIT_REGISTRY', 'blocked', 'Git registration unavailable; no prune/reset attempted', repository=str(repo))
            continue
        for field in process.stdout.split(b'\0'):
            if field.startswith(b'worktree '):
                path = Path(os.fsdecode(field[9:]))
                if not path.is_dir():
                    check('STALE_WORKTREE', 'warning', 'Registered worktree missing; preserve registry and inspect active session cwd', path=str(path))
    try:
        policy = autonomy.load(directory) or {}
        if policy.get('runtime'):
            executable = codex_runtime.approved_runtime(policy['runtime'], directory)
            source = 'approved snapshot'
        else:
            executable = shutil.which('codex')
            source = 'system'
        if not executable:
            check('RUNTIME_MISSING', 'blocked', 'Codex executable unavailable')
        else:
            process = subprocess.run([str(executable), '--version'], env=permissions.launch_environment({}),
                                     capture_output=True, text=True, timeout=10)
            version = process.stdout.strip()
            if process.returncode or not version.startswith('codex-cli '):
                check('RUNTIME_VERSION', 'blocked', 'Runtime version could not be verified')
            elif version not in codex_runtime.SUPPORTED_VERSIONS:
                check('SCANNER_COMPATIBILITY', 'warning', 'Closed-directory/module adapter does not support this version; do not silently bypass its guard', version=version, source=source)
            else:
                check('SCANNER_COMPATIBILITY', 'passed', 'Version supported; native startup/sandbox smoke remains a separate check', version=version, source=source)
        profile = permissions.load_structure(Path(directory)/(permissions.NAME+'.config.toml'))
        if not profile:
            check('PROFILE_MISSING', 'blocked', 'Install/configure profile explicitly; doctor does not install it')
    except (OSError, ValueError, codex_runtime.ScannerRuntimeError, autonomy.PolicyError, subprocess.SubprocessError):
        check('RUNTIME_POLICY', 'blocked', 'Runtime snapshot/policy unavailable or changed; no automatic fallback')
    result['ready'] = not any(item['status'] == 'blocked' for item in result['checks'])
    result['scope'] = 'Read-only preflight and metadata; not a successful model turn or full native permission probe'
    return result
