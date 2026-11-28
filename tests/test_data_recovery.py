"""Recovery contracts using real temporary SQLite files and synthetic records."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.approval_store import ApprovalStore, TaskError
from server.chat_history import HistoryStore
from server.recovery_bundle import (DATABASES, RecoveryError, create_bundle, verify_bundle,
                             restore_bundle, digest, snapshot)
from tests.recovery_fixture import seed_fixture, SYNTHETIC_PASSWORD
from legacy.task_memory import TaskMemory
from server.user_accounts import AccountStore


class DataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.live = self.root/'synthetic-source'
        self.fixture = seed_fixture(self.live)
        self.bundle = self.root/'backup'

    def tearDown(self):
        self.temp.cleanup()

    def backup(self):
        return create_bundle(self.live/'data', self.bundle, True)

    def test_store_connections_use_busy_timeout_and_close_on_exit(self):
        from devices.diagnostic_core import AuditStore
        from legacy.experience_store import ExperienceStore
        from server.text_materials import MaterialStore

        stores = (ApprovalStore, HistoryStore, AccountStore, ExperienceStore, MaterialStore, AuditStore)
        for factory in stores:
            with self.subTest(store=factory.__name__):
                store = factory(self.root / (factory.__name__ + '.sqlite3'))
                with store.connect() as db:
                    self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute('SELECT 1')

    def test_roundtrip_preserves_history_ownership_ttl_audit_and_revokes_authority(self):
        memory=TaskMemory(HistoryStore(self.live/'data/chats.sqlite3'))
        rolling=memory.rolling
        self.assertEqual(rolling.configure(self.fixture['owner'],'recovery-chat',{'enabled':True,'expected_revision':0})['state'],'active')
        source_answer=next(m['content'] for m in reversed(self.fixture['messages']) if m['role']=='assistant')
        memory.public_evidence.save(self.fixture['owner'],'recovery-chat',{'status':'answered','answer':source_answer,
            'public_lookup':{'status':'succeeded'},'sources':[{'id':'WEB-1','kind':'public_web',
            'url':'https://pypi.org/project/fastapi/','fetched_at':'2026-10-01T00:00:00Z','published_at':None}]})
        before = {name: digest(self.live/'data'/name) for name in DATABASES}
        self.backup()
        saved = verify_bundle(self.bundle)
        target = self.root/'restored'
        report = restore_bundle(self.bundle, target)
        self.assertEqual(report['unfinished_tasks_cancelled'], 3)
        self.assertEqual(report['account_sessions_revoked'], 1)
        self.assertFalse((target/'.secrets').exists())
        self.assertEqual(report['services_started'], 0)
        owner = self.fixture['owner']
        self.assertEqual(TaskMemory(HistoryStore(target/'data/chats.sqlite3')).rolling.get(owner,'recovery-chat')['state'],'off')
        self.assertEqual(rolling.get(owner,'recovery-chat')['state'],'active')
        accounts = AccountStore(target/'data/accounts.sqlite3')
        self.assertIsNone(accounts.resolve(self.fixture['token']))
        new_token, account = accounts.login('recovery_preview', SYNTHETIC_PASSWORD)
        self.assertEqual(account['id'], self.fixture['account']['id'])
        self.assertIsNotNone(accounts.resolve(new_token))
        history = HistoryStore(target/'data/chats.sqlite3')
        self.assertEqual(history.get(owner, 'recovery-chat')['messages'], self.fixture['messages'])
        sources=TaskMemory(history).public_evidence.background(owner,'recovery-chat','你从哪里得到的信息？',self.fixture['messages'])
        self.assertEqual(sources['context']['status'],'historical')
        self.assertEqual(sources['context']['sources'][0]['fetched_at'],'2026-10-01T00:00:00Z')
        self.assertEqual(sources['context']['events'],[])
        self.assertEqual(history.get('other-owner', 'recovery-chat')['messages'][0]['content'], 'OTHER_OWNER')
        self.assertEqual(TaskMemory(history).get(owner, 'recovery-chat')['expires'],
                         TaskMemory(HistoryStore(self.live/'data/chats.sqlite3')).get(owner, 'recovery-chat')['expires'])
        store = ApprovalStore(target/'data/workflow.sqlite3')
        for status in ('pending', 'approved', 'running'):
            ident = self.fixture['tasks'][status]
            self.assertEqual(store.get(ident, owner)['status'], 'cancelled')
            self.assertTrue(all(stage['status']!='running' for stage in store.get(ident,owner)['investigation_stages']))
            with self.assertRaises(TaskError): store.claim(ident, owner)
        completed = store.get(self.fixture['tasks']['completed'], owner)
        self.assertEqual(completed['result']['answer'], 'SYNTHETIC_HISTORICAL_RESULT')
        self.assertEqual(completed['attempts'], 1)
        self.assertFalse(store.has_session(owner))
        self.assertEqual(store.interaction(owner, 'recovery-chat')['mode'], 'qa')
        with sqlite3.connect(str(target/'data/diagnostics.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        with sqlite3.connect(str(target/'data/experiences.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT status FROM experiences').fetchone()[0], 'needs_review')
        self.assertEqual(verify_bundle(self.bundle), saved)
        self.assertEqual({name: digest(self.live/'data'/name) for name in DATABASES}, before)

    def test_requires_quiesced_input_and_complete_fixed_database_set(self):
        with self.assertRaisesRegex(RecoveryError, 'confirmation'):
            create_bundle(self.live/'data', self.bundle)
        self.assertFalse(self.bundle.exists())
        (self.live/'data/materials.sqlite3').unlink()
        with self.assertRaises(RecoveryError): self.backup()
        self.assertFalse(self.bundle.exists())

    def test_snapshot_contains_committed_wal_and_does_not_mutate_source(self):
        path = self.live/'data/chats.sqlite3'
        connection = sqlite3.connect(str(path))
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("UPDATE chat_histories SET title='COMMITTED_WAL'")
            connection.commit()
            self.assertGreater(Path(str(path)+'-wal').stat().st_size, 0)
            self.backup()
            verify_bundle(self.bundle)
            with sqlite3.connect(str(self.bundle/'data/chats.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT DISTINCT title FROM chat_histories').fetchall(), [('COMMITTED_WAL',)])
        finally:
            connection.close()

    def test_corrupt_or_changed_bundle_refuses_restore_before_creating_target(self):
        self.backup()
        with sqlite3.connect(str(self.bundle/'data/chats.sqlite3')) as db:
            db.execute("UPDATE chat_histories SET title='TAMPERED'")
        with self.assertRaises(RecoveryError): restore_bundle(self.bundle, self.root/'restored')
        self.assertFalse((self.root/'restored').exists())
        (self.bundle/'data/chats.sqlite3').write_bytes(b'not a database')
        with self.assertRaises((RecoveryError, sqlite3.Error)): verify_bundle(self.bundle)

    def test_existing_or_nested_destination_is_never_overwritten(self):
        self.backup()
        occupied = self.root/'occupied'; occupied.mkdir(); (occupied/'marker').write_text('keep')
        for target in (occupied, self.bundle/'nested', self.root):
            with self.subTest(target=target), self.assertRaises(RecoveryError):
                restore_bundle(self.bundle, target)
        self.assertEqual((occupied/'marker').read_text(), 'keep')
        with self.assertRaises(RecoveryError): create_bundle(self.live/'data', self.live/'data/new', True)

    def test_partial_backup_or_restore_never_has_success_marker(self):
        with patch('server.recovery_bundle.snapshot', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError): self.backup()
        self.assertTrue((self.bundle/'INCOMPLETE.json').is_file())
        self.assertFalse((self.bundle/'BACKUP.json').exists())
        with self.assertRaises(RecoveryError): verify_bundle(self.bundle)
        clean = self.root/'good'
        create_bundle(self.live/'data', clean, True)
        with patch('server.recovery_bundle.quarantine_authority', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError): restore_bundle(clean, self.root/'partial-restore')
        self.assertTrue((self.root/'partial-restore/INCOMPLETE.json').is_file())
        self.assertFalse((self.root/'partial-restore/RESTORE-REPORT.json').exists())

    def test_manifest_schema_and_file_allowlist_fail_closed(self):
        self.backup()
        path = self.bundle/'BACKUP.json'
        original = path.read_text(encoding='utf-8')
        for bad in ([], {'format': 999}, dict(json.loads(original), files={'../outside': {}})):
            path.write_text(json.dumps(bad), encoding='utf-8')
            with self.subTest(bad=bad), self.assertRaises(RecoveryError): verify_bundle(self.bundle)
        path.write_text(original, encoding='utf-8')
        (self.bundle/'unexpected').write_text('must reject')
        with self.assertRaises(RecoveryError): verify_bundle(self.bundle)

    def test_snapshot_has_finite_busy_or_copy_deadline(self):
        with patch('server.recovery_bundle.time.monotonic', side_effect=[0, 100]):
            with self.assertRaisesRegex(RecoveryError, 'limit'):
                snapshot(self.live/'data/chats.sqlite3', self.root/'limited.sqlite3', 'chats.sqlite3', timeout=1)


if __name__ == '__main__': unittest.main()
