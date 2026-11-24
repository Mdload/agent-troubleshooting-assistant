"""Small synthetic six-store fixture for recovery drills; no user or device data."""
import base64
import json
from pathlib import Path
import sqlite3
import time

from approval_store import ApprovalStore
from chat_history import HistoryStore
from diagnostic_core import AuditStore
from experience_store import ExperienceStore
from task_memory import TaskMemory
from text_materials import MaterialStore
from user_accounts import AccountStore

SYNTHETIC_PASSWORD = 'offline-recovery-only-123'


def seed_fixture(root):
    data = Path(root)/'data'; data.mkdir(parents=True)
    accounts = AccountStore(data/'accounts.sqlite3')
    token, account = accounts.register('recovery_preview', SYNTHETIC_PASSWORD, True)
    owner = accounts.owner(account)
    history = HistoryStore(data/'chats.sqlite3')
    messages = [{'role': 'user', 'content': 'RECOVERY_GOAL: keep the original learning goal', 'context': True},
                {'role': 'assistant', 'content': 'SYNTHETIC_ANSWER: historical text only', 'context': False}]
    history.put(owner, {'id': 'recovery-chat', 'title': 'Synthetic recovery conversation',
                        'messages': messages, 'expected_revision': 0})
    history.put('other-owner', {'id': 'recovery-chat', 'title': 'Other owner isolated',
                               'messages': [{'role': 'user', 'content': 'OTHER_OWNER'}], 'expected_revision': 0})
    memory = TaskMemory(history)
    memory.save(owner, 'recovery-chat', {'expected_revision': 0, 'history_revision': 1,
                'entries': [{'kind': 'goal', 'message_index': 0, 'quote': messages[0]['content']}], 'confirmed': True})
    workflow = ApprovalStore(data/'workflow.sqlite3')
    workflow.add_session(owner)
    mode = workflow.set_interaction(owner, 'recovery-chat', 'diagnostic', 'ha_topology')
    tasks = {}
    for status in ('pending', 'approved', 'running', 'completed'):
        task = workflow.create(owner, 'Synthetic recovery task', [], {'local-backup': ['services']}, 'recovery-chat', mode['revision'])
        if status != 'pending': workflow.decide(task['id'], True, 'authenticated-admin')
        if status in ('running', 'completed'):
            workflow.claim(task['id'], owner)
            workflow.authorize_read(task['id'], owner, 'local-backup', 'services')
        if status == 'completed':
            workflow.finish(task['id'], owner, {'status': 'answered', 'answer': 'SYNTHETIC_HISTORICAL_RESULT',
                                              'observations': [{'id': 'OBS-1', 'kind': 'synthetic'}]})
        tasks[status] = task['id']
    AuditStore(data/'diagnostics.sqlite3').add('synthetic-audit', 1, 'read', {'synthetic': True})
    ExperienceStore(data/'experiences.sqlite3')
    with sqlite3.connect(str(data/'experiences.sqlite3')) as db:
        db.execute('INSERT INTO experiences VALUES (?,?,?,?,?,?)',
                   ('withdrawn-fixture', owner, 'recovery-chat', 'fixture-hash', 'needs_review',
                    json.dumps({'note': 'SYNTHETIC_WITHDRAWN_EXPERIENCE', 'publication': None})))
    MaterialStore(data/'materials.sqlite3').add(owner, {'conversation_id': 'recovery-chat', 'name': 'synthetic.log',
                'data_base64': base64.b64encode(b'FAKE LOG ONLY\nno device reads\n').decode('ascii')})
    # Configuration is intentionally outside the fixed database allowlist.
    (Path(root)/'.secrets').mkdir()
    (Path(root)/'.secrets/model.json').write_text('{"synthetic":true}', encoding='utf-8')
    return {'owner': owner, 'token': token, 'account': account, 'tasks': tasks,
            'messages': messages, 'seeded_at': time.time()}
