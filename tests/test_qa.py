"""Real local Engine tests are opt-in; no mocks and no image pulls."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from qa_worker import QA, QAError, process_identity


class QABoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kit-qa-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        self.qa = QA(self.project, self.root/'ledger')

    def test_project_cannot_host_its_own_authority(self):
        with self.assertRaises(QAError):
            QA(self.project, self.project/'ledger')

    def test_unknown_operations_and_invalid_contracts_do_not_contact_engine(self):
        with self.assertRaises(QAError):
            self.qa.dispatch({'action': 'start', 'id': 'run', 'command': ['/bin/bash']})
        with self.assertRaises(QAError):
            self.qa.dispatch({'action': 'preview', 'contract': {'id': 'run', 'bind': '/host'}})
        with self.assertRaises(QAError):
            self.qa.dispatch({'action': 'status', 'id': '../escape'})
        self.assertEqual(list((self.root/'ledger').glob('*.json')), [])

    def test_symlink_ledger_and_foreign_mode_are_rejected(self):
        alias = self.root/'alias'
        alias.symlink_to(self.root/'ledger')
        with self.assertRaises(QAError):
            QA(self.project, alias)
        (self.root/'ledger').chmod(0o755)
        with self.assertRaises(QAError):
            QA(self.project, self.root/'ledger')


@unittest.skipUnless(os.environ.get('KIT_QA_INTEGRATION') == '1', 'Explicit QA lifecycle approval and local image required')
class QAEngineTests(unittest.TestCase):
    def test_large_container_output_is_bounded_and_not_retried(self):
        root = Path(tempfile.mkdtemp(prefix='kit-qa-output-'))
        project = root/'project'
        project.mkdir()
        qa = QA(project, root/'ledger')
        contract = {'id': 'output-fixture', 'image': os.environ['KIT_QA_IMAGE'],
                    'env': {'POSTGRES_HOST_AUTH_METHOD': 'trust'}, 'command': [],
                    'storage_target': '/var/lib/postgresql/data', 'ttl_seconds': 3600,
                    'phases': {'test': [['/bin/sh', '-c', 'head -c 2097152 /dev/zero']]}}
        preview = qa.dispatch({'action': 'preview', 'contract': contract})
        qa.dispatch({'action': 'grant', 'contract': contract, 'confirm': True, 'confirmation_token': preview['confirmation_token']})
        try:
            qa.dispatch({'action': 'start', 'id': contract['id']})
            with self.assertRaisesRegex(QAError, 'output limit'):
                qa.dispatch({'action': 'check', 'id': contract['id'], 'phase': 'test'})
            status = qa.dispatch({'action': 'status', 'id': contract['id']})
            self.assertEqual(status['pending_checks'], ['test:default'])
            with self.assertRaisesRegex(QAError, 'outcome unknown'):
                qa.dispatch({'action': 'check', 'id': contract['id'], 'phase': 'test'})
        finally:
            qa.dispatch({'action': 'cleanup', 'id': contract['id']})
        shutil.rmtree(root)

    def test_full_lifecycle_ownership_expiry_replay_and_no_foreign_file_deletion(self):
        root = Path(tempfile.mkdtemp(prefix='kit-qa-engine-'))
        project = root/'project'
        project.mkdir()
        unrelated = project/'user-data.txt'
        unrelated.write_text('keep user data')
        qa = QA(project, root/'ledger')
        image = os.environ['KIT_QA_IMAGE']
        contract = {'id': 'postgres-fixture', 'image': image, 'command': [], 'env': {'POSTGRES_HOST_AUTH_METHOD': 'trust'},
            'storage_target': '/var/lib/postgresql/data', 'ttl_seconds': 3600,
            'phases': {
                'seed': [['psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', 'CREATE TABLE kit_fixture (value integer); INSERT INTO kit_fixture VALUES (7);']],
                'migrate': [['psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', 'ALTER TABLE kit_fixture ADD COLUMN checked boolean DEFAULT true;']],
                'test': [['psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM kit_fixture WHERE value=7 AND checked) THEN RAISE EXCEPTION 'fixture failed'; END IF; END $$;"]]}}
        run = contract['id']
        moved = None
        def call(action, **args):
            return qa.dispatch({'action': action, 'id': run, **args})
        def ready():
            record = qa.load(run)
            for _ in range(50):
                if qa.docker('exec', record['container'], 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', check=False).returncode == 0:
                    return
                time.sleep(.2)
            self.fail('QA PostgreSQL did not become ready; ledger preserved at '+str(root))
        preview = qa.dispatch({'action': 'preview', 'contract': contract})
        bad = copy.deepcopy(contract)
        bad['command'] = ['unexpected']
        with self.assertRaises(QAError):
            qa.dispatch({'action': 'grant', 'contract': bad, 'confirm': True, 'confirmation_token': preview['confirmation_token']})
        with self.assertRaises(QAError):
            qa.dispatch({'action': 'grant', 'contract': contract, 'confirmation_token': preview['confirmation_token']})
        qa.dispatch({'action': 'grant', 'contract': contract, 'confirm': True, 'confirmation_token': preview['confirmation_token']})
        try:
            call('start')
            record = qa.load(run)
            self.assertEqual(call('start')['container'], record['container'])
            ready()
            for phase in ('seed', 'migrate', 'test'):
                self.assertTrue(call('check', phase=phase)['passed'])
            self.assertTrue(call('check', phase='seed')['replayed'])
            call('restart')
            ready()
            self.assertTrue(call('check', phase='test', attempt='after-restart')['passed'])
            saved = qa.load(run)
            with qa.lock(run):
                with self.assertRaises(QAError):
                    call('status')
            # A second, stopped test container references the owned volume.
            # It must block cleanup, and must never be removed by the backend.
            foreign = qa.docker('create', '--pull=never', '--name', 'kit-qa-test-foreign-'+uuid.uuid4().hex,
                '--network=none', '--read-only', '--cap-drop=ALL',
                '--mount', 'type=volume,source='+saved['volume']+',target=/var/lib/postgresql/data',
                image, '/bin/true').stdout.decode().strip()
            try:
                with self.assertRaises(QAError):
                    call('cleanup')
                self.assertEqual(qa.inspect('container', foreign)['Id'], foreign)
            finally:
                qa.docker('rm', foreign)
            self.assertTrue(call('status')['running'])
            # Protected metadata tampering exercises actual Engine ownership checks.
            for field, value in [('nonce', 'b'*32), ('volume', '0'*64), ('engine', 'foreign-engine'), ('uid', -1)]:
                altered = copy.deepcopy(saved)
                altered[field] = value
                qa.save(altered)
                with self.assertRaises(QAError):
                    call('stop')
                qa.save(saved)
                self.assertTrue(call('status')['running'])
            actor = subprocess.Popen(['/usr/bin/sleep', '60'])
            try:
                altered = copy.deepcopy(saved)
                altered['owner'] = process_identity(actor.pid)
                qa.save(altered)
                with self.assertRaises(QAError):
                    call('stop')
                with self.assertRaises(QAError):
                    call('renew_preview')
            finally:
                actor.terminate()
                actor.wait(timeout=5)
            takeover = call('renew_preview')
            call('renew', confirm=True, confirmation_token=takeover['confirmation_token'])
            saved = qa.load(run)
            # A reused PID with a different start time does not inherit a grant.
            altered = copy.deepcopy(saved)
            altered['owner']['start_time'] += 1
            qa.save(altered)
            with self.assertRaises(QAError):
                call('stop')
            qa.save(saved)
            altered = copy.deepcopy(saved)
            altered['expires_at'] = 0
            qa.save(altered)
            with self.assertRaises(QAError):
                call('stop')
            renewed = call('renew_preview')
            with self.assertRaises(QAError):
                call('renew', confirmation_token=renewed['confirmation_token'])
            call('renew', confirm=True, confirmation_token=renewed['confirmation_token'])
            with self.assertRaises(QAError):
                call('check', phase='test', command=['rm', '-rf', '/host'])
            call('stop')
            self.assertFalse(call('stop')['running'])
            call('start')
            ready()
            self.assertTrue(call('check', phase='test', attempt='after-stop-start')['passed'])
            moved = root/'moved-project'
            project.rename(moved)
            self.assertFalse(call('status')['project_matches'])
            with self.assertRaises(QAError):
                call('start')
            with self.assertRaises(QAError):
                call('check', phase='test', attempt='missing-root')
            call('stop')
        finally:
            call('cleanup')
        self.assertEqual(call('cleanup')['phase'], 'cleaned')
        self.assertEqual(((moved/'user-data.txt') if moved else unrelated).read_text(), 'keep user data')
        for kind, key in [('container', 'container'), ('network', 'network'), ('volume', 'volume')]:
            self.assertNotEqual(qa.docker(kind, 'inspect', record[key], check=False).returncode, 0)
        shutil.rmtree(root)


if __name__ == '__main__':
    unittest.main()
