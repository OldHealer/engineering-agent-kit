#!/usr/bin/env python3
"""QA-контейнеры по неизменному grant. Произвольных host exec/mount/pull нет."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class QAError(ValueError):
    pass


def require(value, message):
    if not value:
        raise QAError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value), 'Invalid run ID')
    return value


def process_identity(pid):
    directory = Path('/proc')/str(pid)
    require(directory.stat().st_uid == os.getuid(), 'QA owner has a different UID')
    fields = (directory/'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_time': int(fields[19]),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def owner_active(owner):
    try:
        return process_identity(owner['pid']) == owner
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, QAError):
        return None


class QA:
    def __init__(self, project, store, owner_pid=None):
        self.owner = process_identity(owner_pid or os.getpid())
        self.project = Path(project).absolute()
        require('..' not in self.project.parts, 'Project path must be canonical')
        self.store = Path(store).absolute()
        require(not self.store.is_relative_to(self.project) and not self.project.is_relative_to(self.store), 'Ledger must be outside project')
        require(self.store.resolve() == self.store, 'Ledger path contains symlinks')
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.store.stat()
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Ledger ownership/mode mismatch')

    def project_identity(self):
        try:
            if self.project.resolve() != self.project:
                return None
            info = self.project.stat()
            return [info.st_dev, info.st_ino] if stat.S_ISDIR(info.st_mode) else None
        except OSError:
            return None

    def docker(self, *args, check=True):
        # Ограничение regular-file output действует в kernel самого Docker CLI:
        # capture_output не должен позволять тесту заполнить память host backend.
        limit = 1024*1024
        def bound_output():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(['/usr/bin/docker', '--config', '/nonexistent', '--host', 'unix:///var/run/docker.sock', *args],
                env={'PATH': os.defpath, 'HOME': '/nonexistent', 'LANG': 'C.UTF-8'},
                stdout=stdout, stderr=stderr, timeout=60, preexec_fn=bound_output)
            require(stdout.tell() < limit and stderr.tell() < limit, 'QA output limit reached; inspect pending check before retry')
            stdout.seek(0)
            stderr.seek(0)
            result.stdout = stdout.read(limit)
            result.stderr = stderr.read(limit)
        if check:
            require(result.returncode == 0, 'Docker operation refused; inspect QA status (output withheld)')
        return result

    def inspect(self, kind, identity):
        result = self.docker(kind, 'inspect', identity, check=False)
        require(result.returncode == 0, 'Recorded resource missing; no resource adoption/deletion by name')
        return json.loads(result.stdout)[0]

    def engine(self):
        info = json.loads(self.docker('info', '--format', '{{json .}}').stdout)
        require(info.get('OSType') == 'linux' and info.get('ID'), 'Linux Docker Engine required')
        require(int(str(info.get('ServerVersion', '0')).split('.')[0]) >= 28, 'Docker 28+ required for isolated bridge without host address')
        return digest([info['ID'], 'unix:///var/run/docker.sock'])

    def file(self, run):
        return self.store/(digest([str(self.project), identifier(run)])+'.json')

    def save(self, record):
        path = self.file(record['contract']['id'])
        with tempfile.NamedTemporaryFile(dir=self.store, mode='w', delete=False) as stream:
            temp = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temp.unlink()
                raise
        os.replace(temp, path)

    def load(self, run):
        path = self.file(run)
        require(path.is_file() and not path.is_symlink(), 'No grant for run')
        info = path.stat()
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'Unsafe grant record')
        record = json.loads(path.read_text())
        require(isinstance(record.get('owner'), dict) and set(record['owner']) == {'pid', 'start_time', 'boot_id'}, 'Unknown grant owner')
        require(record.get('version') == 1 and re.fullmatch(r'[a-f0-9]{32}', record.get('nonce', '')), 'Invalid grant metadata')
        for key in ('container', 'network', 'volume'):
            require(not record.get(key) or re.fullmatch(r'[a-f0-9]{64}', record[key]), 'Invalid resource identity')
        require(record['project'] == str(self.project) and record['uid'] == os.getuid(), 'Grant owner mismatch')
        require(record['contract_digest'] == digest(record['contract']), 'Contract changed')
        require(record['engine'] == self.engine(), 'Docker Engine changed')
        return record

    @contextmanager
    def lock(self, run):
        path = self.store/(self.file(run).stem+'.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe QA lease')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QAError('Another QA operation holds the lease') from None
            yield
        finally:
            os.close(fd)

    def contract(self, value):
        require(isinstance(value, dict) and set(value) == {'id', 'image', 'env', 'command', 'storage_target', 'phases', 'ttl_seconds'}, 'Unknown contract fields')
        identifier(value['id'])
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', value['image'] or ''), 'Use an existing immutable image ID')
        require(type(value['ttl_seconds']) is int and 60 <= value['ttl_seconds'] <= 86400, 'TTL must be 60..86400 seconds')
        target = value['storage_target']
        require(isinstance(target, str) and re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+', target), 'Invalid storage target')
        require(target.startswith(('/qa/', '/var/lib/')) or target == '/qa', 'Only QA/application data targets allowed')
        require(isinstance(value['env'], dict) and len(value['env']) <= 32, 'Invalid environment')
        for key, item in value['env'].items():
            require(re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', key) and not re.search(r'PASSWORD|TOKEN|SECRET|KEY|CREDENTIAL', key), 'Secrets are not accepted in QA contract')
            require(isinstance(item, str) and len(item) <= 1024 and '\0' not in item, 'Invalid environment value')
        def argv(args, empty=False):
            require(isinstance(args, list) and (bool(args) or empty) and len(args) <= 64, 'Invalid command array')
            require(all(isinstance(arg, str) and arg and len(arg) <= 8192 and '\0' not in arg for arg in args), 'Invalid command argument')
            require(not args or not args[0].startswith('-'), 'Invalid executable')
        argv(value['command'], empty=True)
        require(isinstance(value['phases'], dict) and set(value['phases']).issubset({'seed', 'migrate', 'test'}) and 'test' in value['phases'], 'Choose seed/migrate/test phases')
        for commands in value['phases'].values():
            require(isinstance(commands, list) and 1 <= len(commands) <= 10, 'Invalid phase')
            for command in commands:
                argv(command)
        image = self.inspect('image', value['image'])
        require(image['Id'] == value['image'] and set(image['Config'].get('Volumes') or {}).issubset({target}), 'Image declares additional volumes')
        return value

    def preview(self, contract):
        require(self.project_identity(), 'Project missing or symlinked; no new QA grant')
        contract = self.contract(contract)
        require(not self.file(contract['id']).exists(), 'Run already exists; use status')
        return {'project': str(self.project), 'project_identity': self.project_identity(), 'uid': os.getuid(), 'engine': self.engine(),
                'contract_digest': digest(contract), 'owner': self.owner, 'image': contract['image'], 'id': contract['id'],
                'env_keys': sorted(contract['env']), 'phases': contract['phases'], 'command': contract['command'],
                'storage_target': contract['storage_target'], 'ttl_seconds': contract['ttl_seconds'],
                'resources': 'one new container, internal isolated bridge without host address, and anonymous volume',
                'cleanup': 'stop/remove only recorded container and its anonymous volume; no host file removal',
                'limits': {'memory_mb': 256, 'cpus': 1, 'pids': 128, 'read_only_root': True, 'external_dns': False, 'command_timeout_seconds': 60,
                           'output_limit_bytes_per_stream': 1048576, 'volume_disk_quota': None,
                           'capabilities': ['CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID'], 'host_mounts': False, 'published_ports': False}}

    def labels(self, record):
        return {'engineering-agent-kit.qa': record['nonce'], 'engineering-agent-kit.owner': str(record['uid'])}

    def owned_network(self, record):
        network = self.inspect('network', record['network'])
        require(network['Id'] == record['network'] and network.get('Internal') is True
                and all((network.get('Labels') or {}).get(key) == value for key, value in self.labels(record).items()), 'Network ownership/configuration mismatch')
        require(network.get('Driver') == 'bridge' and not network.get('EnableIPv6')
                and all((network.get('Options') or {}).get(key) == 'isolated' for key in
                        ('com.docker.network.bridge.gateway_mode_ipv4', 'com.docker.network.bridge.gateway_mode_ipv6')),
                'Network isolation mode changed')
        return network

    def owned(self, record):
        require(record.get('container'), 'No recorded container; check partial creation')
        container = self.inspect('container', record['container'])
        require(container['Id'] == record['container'] and container['Image'] == record['contract']['image']
                and all((container['Config'].get('Labels') or {}).get(key) == value for key, value in self.labels(record).items()), 'Container ownership mismatch')
        host = container['HostConfig']
        require(host.get('ReadonlyRootfs') and not host.get('Privileged') and not host.get('Binds')
                and not host.get('Devices') and not host.get('PortBindings')
                and host.get('Dns') == ['127.0.0.1'] and not host.get('ExtraHosts') and not host.get('PidMode')
                and host.get('NetworkMode') == record['network'] and host.get('Memory') == 256*1024*1024
                and host.get('PidsLimit') == 128 and host.get('NanoCpus') == 1000000000,
                'Container isolation configuration changed')
        require(set(host.get('CapDrop') or []) == {'ALL'}
                and {item.removeprefix('CAP_') for item in (host.get('CapAdd') or [])} == {'CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID'}
                and host.get('SecurityOpt') == ['no-new-privileges:true'], 'Container privilege mismatch')
        require(host.get('RestartPolicy', {}).get('Name') == 'no'
                and host.get('Tmpfs') == {'/tmp': 'rw,nosuid,nodev,size=33554432',
                                          '/var/run/postgresql': 'rw,nosuid,nodev,size=8388608'},
                'Container restart/tmpfs configuration mismatch')
        mounts = container.get('Mounts', [])
        require(len(mounts) == 1 and mounts[0].get('Type') == 'volume'
                and mounts[0]['Name'] == record['volume'] and mounts[0]['Destination'] == record['contract']['storage_target'],
                'Container volume changed')
        volume = self.inspect('volume', record['volume'])
        require('com.docker.volume.anonymous' in (volume.get('Labels') or {})
                and digest(volume) == record['volume_digest'], 'Volume generation mismatch')
        users = self.docker('ps', '-aq', '--no-trunc', '--filter', 'volume='+record['volume']).stdout.decode().split()
        require(users == [record['container']], 'Volume has foreign consumers')
        network = self.owned_network(record)
        require(set(network.get('Containers') or {}).issubset({record['container']}), 'Network has foreign consumers')
        require(set(container['NetworkSettings'].get('Networks') or {}) == {network['Name']}, 'Unexpected container network')
        return container

    def status(self, record):
        state = {'id': record['contract']['id'], 'phase': record['phase'], 'expires_at': record['expires_at'],
                 'expired': time.time() >= record['expires_at'], 'project_matches': self.project_identity() == record['project_identity'], 'owner_matches': record['owner'] == self.owner,
                 'owner_active': owner_active(record['owner']), 'container': record.get('container'),
                 'network': record.get('network'), 'volume': record.get('volume'), 'blockers': []}
        if record.get('container') and not record.get('container_removed') and record['phase'] != 'cleaned':
            try:
                container = self.owned(record)
                state['running'] = container['State']['Running']
            except (QAError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                state['blockers'].append(str(error) if isinstance(error, QAError) else 'Resource state unavailable')
        if record['phase'] in {'creating', 'cleaning', 'failed'}:
            state['blockers'].append('Interrupted/failed transition; inspect recorded IDs, no adoption by name')
        state['pending_checks'] = [key for key, value in record.get('operations', {}).items() if value['state'] != 'complete']
        return state

    def start(self, record):
        if record.get('container'):
            container = self.owned(record)
            if not container['State']['Running']:
                self.docker('start', record['container'])
            record['phase'] = 'running'
            self.save(record)
            return self.status(record)
        require(record['phase'] == 'granted', 'Creation already attempted; reconcile before retry')
        record['phase'] = 'creating'
        self.save(record)
        labels = [arg for key, value in self.labels(record).items() for arg in ('--label', key+'='+value)]
        prefix = 'kit-qa-test-'+record['nonce'][:24]
        network = self.docker('network', 'create', '--internal', '--ipv6=false',
            '--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated',
            '--opt', 'com.docker.network.bridge.gateway_mode_ipv6=isolated', *labels, prefix).stdout.decode().strip()
        require(re.fullmatch(r'[a-f0-9]{64}', network), 'Invalid network ID')
        record['network'] = network
        self.save(record)
        contract = record['contract']
        environment = [arg for key, value in contract['env'].items() for arg in ('--env', key+'='+value)]
        result = self.docker('create', '--name', prefix, '--pull=never', '--network', network,
            '--read-only', '--dns=127.0.0.1', '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=DAC_OVERRIDE', '--cap-add=SETUID', '--cap-add=SETGID',
            '--security-opt=no-new-privileges:true', '--memory=256m', '--cpus=1', '--pids-limit=128',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=33554432',
            '--tmpfs=/var/run/postgresql:rw,nosuid,nodev,size=8388608',
            '--mount', 'type=volume,target='+contract['storage_target']+',volume-nocopy',
            *labels, *environment, contract['image'], *contract['command'])
        container_id = result.stdout.decode().strip()
        require(re.fullmatch(r'[a-f0-9]{64}', container_id), 'Invalid container ID')
        # Сохраняем ID до любых последующих действий: неизвестные ресурсы не удаляются.
        record['container'] = container_id
        self.save(record)
        container = self.inspect('container', container_id)
        mounts = container.get('Mounts', [])
        require(len(mounts) == 1 and mounts[0]['Type'] == 'volume', 'Unexpected mounts at creation')
        record['volume'] = mounts[0]['Name']
        record['volume_digest'] = digest(self.inspect('volume', record['volume']))
        self.save(record)
        self.owned(record)
        self.docker('start', container_id)
        record['phase'] = 'running'
        self.save(record)
        return self.status(record)

    def cleanup(self, record):
        if record['phase'] == 'cleaned':
            # Повтор cleanup ничего не удаляет, даже если кто-то заново занял старое имя.
            return self.status(record)
        if record.get('container') and not record.get('container_removed'):
            self.owned(record)
            self.docker('stop', '--time=10', record['container'])
            self.owned(record)
        elif record.get('network') and not record.get('network_removed'):
            require(record['phase'] in {'creating', 'cleaning'}, 'Missing container; no deletion by name')
            require(not self.owned_network(record).get('Containers'), 'Network has consumers')
        record['phase'] = 'cleaning'
        self.save(record)
        if record.get('container') and not record.get('container_removed'):
            # Один Engine API удаляет контейнер и только его anonymous volumes.
            # Отдельного volume rm по повторно использованному имени нет.
            self.docker('rm', '--volumes', record['container'])
            record['container_removed'] = True
            self.save(record)
        if record.get('network') and not record.get('network_removed'):
            require(not self.owned_network(record).get('Containers'), 'Network has foreign consumers')
            self.docker('network', 'rm', record['network'])
            record['network_removed'] = True
            self.save(record)
        record['phase'] = 'cleaned'
        self.save(record)
        return self.status(record)

    def dispatch(self, request):
        action = request['action']
        run = request.get('id') or request.get('contract', {}).get('id')
        allowed = ({'action', 'contract', 'confirm', 'confirmation_token'} if action in {'preview', 'grant'}
                   else {'action', 'id', 'confirm', 'confirmation_token'} if action in {'renew_preview', 'renew'}
                   else {'action', 'id', 'phase', 'attempt'} if action == 'check' else {'action', 'id'})
        require(set(request).issubset(allowed), 'Unknown operation arguments; contract cannot be overridden')
        with self.lock(run):
            if action in {'preview', 'grant'}:
                preview = self.preview(request['contract'])
                if action == 'preview':
                    return {**preview, 'confirmation_token': digest(preview)}
                require(request.get('confirm') is True and request.get('confirmation_token') == digest(preview), 'Grant requires confirmed current preview')
                now = time.time()
                record = {'version': 1, 'project': str(self.project), 'project_identity': preview['project_identity'], 'uid': os.getuid(), 'engine': preview['engine'],
                          'nonce': uuid.uuid4().hex, 'owner': self.owner, 'contract': request['contract'], 'contract_digest': preview['contract_digest'],
                          'phase': 'granted', 'expires_at': now+request['contract']['ttl_seconds']}
                self.save(record)
                return self.status(record)
            record = self.load(run)
            if action == 'status':
                return self.status(record)
            if action in {'renew_preview', 'renew'}:
                status = self.status(record)
                require(record['owner'] == self.owner or status['owner_active'] is False, 'Previous owner is active or unverifiable; no lease takeover')
                require(not status['blockers'] and record['phase'] != 'cleaned', 'Unsafe run cannot be renewed')
                preview = {'id': run, 'project': str(self.project), 'contract_digest': record['contract_digest'],
                           'status': {key: value for key, value in status.items() if key != 'expired'}, 'new_owner': self.owner, 'ttl_seconds': record['contract']['ttl_seconds']}
                if action == 'renew_preview':
                    return {**preview, 'confirmation_token': digest(preview)}
                require(request.get('confirm') is True and request.get('confirmation_token') == digest(preview),
                        'Renewal needs a fresh confirmed preview')
                record['owner'] = self.owner
                record['expires_at'] = time.time()+record['contract']['ttl_seconds']
                self.save(record)
                return self.status(record)
            require(record['owner'] == self.owner, 'Different session owner; confirmed renewal required')
            require(time.time() < record['expires_at'], 'Grant expired; new approval required')
            require(record['phase'] != 'cleaned' or action == 'cleanup', 'Run already cleaned')
            if action in {'start', 'restart', 'check'}:
                require(self.project_identity() == record['project_identity'], 'Project missing/replaced; only status/stop/cleanup permitted')
            if action == 'start':
                return self.start(record)
            if action == 'cleanup':
                return self.cleanup(record)
            container = self.owned(record)
            if action == 'stop':
                if container['State']['Running']:
                    self.docker('stop', '--time=10', record['container'])
                record['phase'] = 'stopped'
                self.save(record)
                return self.status(record)
            if action == 'restart':
                self.docker('restart', '--time=10', record['container'])
                record['phase'] = 'running'
                self.save(record)
                return self.status(record)
            require(action == 'check' and request.get('phase') in record['contract']['phases'], 'Unsupported operation; no arbitrary exec')
            require(container['State']['Running'], 'Container is not running')
            attempt = identifier(request.get('attempt', 'default'))
            key = request['phase']+':'+attempt
            operations = record.setdefault('operations', {})
            if key in operations:
                require(operations[key]['state'] == 'complete', 'Previous check outcome unknown; inspect/stop before retry')
                return {**operations[key]['result'], 'replayed': True}
            require(len(operations) < 100 and all(item['state'] == 'complete' for item in operations.values()),
                    'Pending check or operation limit; inspect run')
            operations[key] = {'state': 'running'}
            self.save(record)
            outcomes = []
            for command in record['contract']['phases'][request['phase']]:
                self.owned(record)
                result = self.docker('exec', record['container'], *command, check=False)
                outcomes.append({'exit_code': result.returncode, 'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
                                 'stderr_sha256': hashlib.sha256(result.stderr).hexdigest()})
                if result.returncode:
                    break
            result = {'phase': request['phase'], 'attempt': attempt, 'passed': all(item['exit_code'] == 0 for item in outcomes), 'checks': outcomes}
            operations[key] = {'state': 'complete', 'result': result}
            self.save(record)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--owner-pid', type=int)
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        result = QA(args.project, args.store, args.owner_pid or os.getppid()).dispatch(request)
        print(json.dumps({'ok': True, 'result': result}))
        return 0
    except QAError as error:
        print(json.dumps({'ok': False, 'message': str(error)}))
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'ok': False, 'message': 'QA operation refused; check grant/ownership/status. Output withheld.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
