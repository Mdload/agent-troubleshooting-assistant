"""按可信账号保存会话文字和服务器签发的修复建议引用。"""
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from legacy.conversation_topic import validate_conversation_id


class HistoryError(ValueError):
    pass


PROPOSAL_ID = re.compile(r'[a-f0-9]{32}')


def transcript(value, proposal_ids=()):
    if not isinstance(value,list) or not 1<=len(value)<=200:raise ValueError('invalid_transcript')
    clean=[]
    for message in value:
        if (not isinstance(message,dict) or not {'role','content'}<=set(message)
            or set(message)-{'role','content','context','contextRevision','proposed_changes'}):raise ValueError('invalid_message')
        if 'context' in message and type(message['context']) is not bool:raise ValueError('invalid_message')
        if 'contextRevision' in message and (type(message['contextRevision']) is not int or message['contextRevision']<0):
            raise ValueError('invalid_message')
        role=message['role'];text=message['content']
        if role not in ('user','assistant') or not isinstance(text,str) or not 1<=len(text)<=16000:
            raise ValueError('invalid_message')
        item={'role':role,'content':text}
        for key in ('context','contextRevision'):
            if key in message:item[key]=message[key]
        if 'proposed_changes' in message:
            references = message['proposed_changes']
            if (role != 'assistant' or not isinstance(references, list)
                    or any(not isinstance(ident, str) or not PROPOSAL_ID.fullmatch(ident)
                           or ident not in proposal_ids for ident in references)):
                raise ValueError('invalid_proposal_reference')
            item['proposed_changes'] = list(dict.fromkeys(references))
        clean.append(item)
    raw=json.dumps(clean,ensure_ascii=False)
    if len(raw.encode('utf-8'))>300000:raise HistoryError('history_size_limit')
    return raw


class HistoryStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.clock=clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS chat_histories ('
                'owner TEXT NOT NULL,id TEXT NOT NULL,title TEXT NOT NULL,messages TEXT NOT NULL,'
                'revision INTEGER NOT NULL,updated REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,'
                'PRIMARY KEY(owner,id))')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            # 与其他存储连接使用相同的锁等待时间。
            db.execute('PRAGMA busy_timeout=5000')
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def row(self,db,owner,ident):
        validate_conversation_id(ident)
        row=db.execute('SELECT * FROM chat_histories WHERE owner=? AND id=? AND deleted=0',(owner,ident)).fetchone()
        if not row:raise HistoryError('history_not_found')
        return row

    def public(self,row,full=False):
        result={key:row[key] for key in ('id','title','revision','updated')}
        result['updated']*=1000
        if full:
            result.update(messages=json.loads(row['messages']),kind='historical_browser_transcript',
                note='恢复本人保存的对话与修复建议状态。历史正文与复查属于旧记录。')
        return result

    def list(self,owner):
        with self.connect() as db:
            return [self.public(row) for row in db.execute(
                'SELECT * FROM chat_histories WHERE owner=? AND deleted=0 ORDER BY updated DESC LIMIT 50',(owner,))]

    def get(self,owner,ident,cards=()):
        with self.connect() as db:
            result = self.public(self.row(db,owner,ident),True)
        by_id = {card['proposal_id']: card for card in cards}
        found = set()
        for message in result['messages']:
            if 'proposed_changes' in message:
                refs = [ref for ref in message['proposed_changes'] if ref in by_id and ref not in found]
                message['proposed_changes'] = [by_id[ref] for ref in refs]
                found.update(refs)
        if by_id.keys() - found:
            result['messages'].append({'role': 'assistant', 'content': '本对话保存的修复建议',
                                       'context': False, 'proposed_changes': [
                                           card for ref, card in by_id.items() if ref not in found
                                       ]})
        return result

    def put(self,owner,data,proposal_ids=()):
        if set(data)!={'id','title','messages','expected_revision'}:raise ValueError('invalid_history_fields')
        ident=validate_conversation_id(data['id']);title=data['title'];revision=data['expected_revision']
        if not isinstance(title,str) or not 1<=len(title.strip())<=80 or type(revision) is not int or revision<0:
            raise ValueError('invalid_history_fields')
        raw=transcript(data['messages'],proposal_ids)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT revision,deleted,messages FROM chat_histories WHERE owner=? AND id=?',(owner,ident)).fetchone()
            if old and old['deleted']:raise HistoryError('history_not_found')
            if (old['revision'] if old else 0)!=revision:raise HistoryError('history_changed')
            if not old and db.execute('SELECT count(*) FROM chat_histories WHERE owner=? AND deleted=0',(owner,)).fetchone()[0]>=50:
                raise HistoryError('history_count_limit')
            if not old and db.execute('SELECT count(*) FROM chat_histories WHERE owner=?',(owner,)).fetchone()[0]>=500:
                raise HistoryError('history_count_limit')
            if old and proposal_ids:
                messages = json.loads(raw)
                included = {ref for message in messages for ref in message.get('proposed_changes', [])}
                for old_message in json.loads(old['messages']):
                    missing = [ref for ref in old_message.get('proposed_changes', [])
                               if ref in proposal_ids and ref not in included]
                    if missing:
                        matched = next((message for message in reversed(messages) if message['role'] == 'assistant'
                                        and message['content'] == old_message['content']), None)
                        if matched is None:
                            matched = {'role': 'assistant', 'content': old_message['content'], 'context': False}
                            messages.append(matched)
                        matched.setdefault('proposed_changes', []).extend(missing)
                        included.update(missing)
                raw = transcript(messages, proposal_ids)
            db.execute('INSERT OR REPLACE INTO chat_histories VALUES (?,?,?,?,?,?,0)',
                (owner,ident,title.strip(),raw,revision+1,self.clock()))
            return self.public(self.row(db,owner,ident))

    def record_proposal_turn(self, owner, ident, question, answer, refs):
        """Persist proposal messages even when the browser disconnects before saving."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT messages,deleted FROM chat_histories WHERE owner=? AND id=?',
                             (owner, ident)).fetchone()
            if row and row['deleted']:
                return
            messages = json.loads(row['messages']) if row else []
            known = {ref for message in messages for ref in message.get('proposed_changes', [])}
            refs = [ref for ref in refs if ref not in known]
            if not refs:
                return
            if question and (not messages or messages[-1]['content'] != question):
                messages.append({'role': 'user', 'content': question, 'context': False})
            messages.append({'role': 'assistant', 'content': answer[:16000], 'context': False,
                             'proposed_changes': refs})
            raw = json.dumps(messages, ensure_ascii=False)
            if row:
                db.execute('UPDATE chat_histories SET messages=?,updated=? WHERE owner=? AND id=?',
                           (raw, self.clock(), owner, ident))
            else:
                db.execute('INSERT INTO chat_histories VALUES (?,?,?,?,0,?,0)',
                           (owner, ident, (question or '修复建议')[:30], raw, self.clock()))

    def delete(self,owner,ident,revision):
        if type(revision) is not int:raise ValueError('invalid_revision')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,owner,ident)
            if row['revision']!=revision:raise HistoryError('history_changed')
            # 留删除代次，拒绝另一浏览器的迟到保存重新生成该聊天。
            db.execute("UPDATE chat_histories SET deleted=1,messages='[]',title='',revision=revision+1,updated=? "
                       'WHERE owner=? AND id=?',(self.clock(),owner,ident))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_memories'").fetchone():
                db.execute('DELETE FROM task_memories WHERE owner=? AND conversation=?',(owner,ident))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rolling_contexts'").fetchone():
                db.execute('DELETE FROM rolling_contexts WHERE owner=? AND conversation=?',(owner,ident))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_evidence'").fetchone():
                db.execute('DELETE FROM public_evidence WHERE owner=? AND conversation=?',(owner,ident))
        return {'status':'deleted','id':ident}
