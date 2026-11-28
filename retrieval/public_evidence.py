"""本人对话最近一次公开出处；保存历史来源，不恢复联网、设备或模型批准。"""
import json
import re
from legacy.learning_intent import source_question
from retrieval.public_lookup import plan

TTL = 7 * 86400


class PublicEvidenceError(ValueError):
    pass


class PublicEvidence:
    def __init__(self, history):
        self.history = history
        with history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS public_evidence ('
                       'owner TEXT,conversation TEXT,revision INTEGER,answer TEXT,sources TEXT,'
                       'expires REAL,updated REAL,PRIMARY KEY(owner,conversation))')

    def save(self, owner, conversation, result):
        if result.get('status') != 'answered' or not result.get('public_lookup'):
            return
        sources = [s for s in result.get('sources', []) if s.get('kind') == 'public_web']
        if not sources:
            return
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.history.row(db, owner, conversation)  # 删除代次不可被迟到回答复活。
            now = self.history.clock()
            db.execute('DELETE FROM public_evidence WHERE expires<=?', (now,))
            old = db.execute('SELECT * FROM public_evidence WHERE owner=? AND conversation=?',
                             (owner, conversation)).fetchone()
            historical = result['public_lookup']['status'] == 'historical'
            if historical and not old:
                return
            expires = old['expires'] if historical else now + TTL
            revision = old['revision'] + 1 if old else 1
            db.execute('INSERT OR REPLACE INTO public_evidence VALUES (?,?,?,?,?,?,?)',
                       (owner, conversation, revision, result['answer'], json.dumps(sources[:2], ensure_ascii=False), expires, now))
            db.execute('DELETE FROM public_evidence WHERE owner=? AND conversation NOT IN '
                       '(SELECT conversation FROM public_evidence WHERE owner=? ORDER BY updated DESC LIMIT 50)', (owner, owner))

    def background(self, owner, conversation, question, history):
        if not (source_question(question) or re.search(r'从哪(?:里)?(?:来|得|获)|哪(?:里)?来的',question)):
            return None
        fresh=plan(question)
        if fresh and fresh['requests']:
            return None
        previous = next((m['content'] for m in reversed(history) if m['role'] == 'assistant'), None)
        if previous is None:
            return None
        with self.history.connect() as db:
            transcript=json.loads(self.history.row(db, owner, conversation)['messages'])
            saved_previous=next((m['content'] for m in reversed(transcript) if m['role']=='assistant'),None)
            row = db.execute('SELECT * FROM public_evidence WHERE owner=? AND conversation=?',
                             (owner, conversation)).fetchone()
            if not row or row['expires'] <= self.history.clock() or row['answer'] != previous or row['answer']!=saved_previous:
                return None
            return {'binding': {'revision': row['revision']},
                    'context': {'status': 'historical', 'sources': [dict(s, freshness='historical_public')
                        for s in json.loads(row['sources'])], 'events': [], 'topic_limit_reached': False,
                        'query_policy': 'previous_public_sources_only_no_network'}}

    def assert_active(self, owner, conversation, binding):
        with self.history.connect() as db:
            transcript=json.loads(self.history.row(db, owner, conversation)['messages'])
            previous=next((m['content'] for m in reversed(transcript) if m['role']=='assistant'),None)
            row = db.execute('SELECT revision,expires,answer FROM public_evidence WHERE owner=? AND conversation=?',
                             (owner, conversation)).fetchone()
            if not row or row['revision'] != binding['revision'] or row['expires'] <= self.history.clock() or row['answer']!=previous:
                raise PublicEvidenceError('public_sources_inactive')
