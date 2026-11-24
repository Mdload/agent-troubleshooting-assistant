"""按可信账号归属保存文字聊天；不接收工具结果、审批或客户端owner。"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from conversation_topic import validate_conversation_id


class HistoryError(ValueError):
    pass


def transcript(value):
    if not isinstance(value,list) or not 1<=len(value)<=200:raise ValueError('invalid_transcript')
    clean=[]
    for message in value:
        if (not isinstance(message,dict) or not {'role','content'}<=set(message)
            or set(message)-{'role','content','context','contextRevision'}):raise ValueError('invalid_message')
        if 'context' in message and type(message['context']) is not bool:raise ValueError('invalid_message')
        if 'contextRevision' in message and (type(message['contextRevision']) is not int or message['contextRevision']<0):
            raise ValueError('invalid_message')
        role=message['role'];text=message['content']
        if role not in ('user','assistant') or not isinstance(text,str) or not 1<=len(text)<=16000:
            raise ValueError('invalid_message')
        item={'role':role,'content':text}
        for key in ('context','contextRevision'):
            if key in message:item[key]=message[key]
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
                note='恢复的是本人保存的文字对话，不认证旧回答，不恢复现场OBS、审批、管理员登录或数据使用同意。')
        return result

    def list(self,owner):
        with self.connect() as db:
            return [self.public(row) for row in db.execute(
                'SELECT * FROM chat_histories WHERE owner=? AND deleted=0 ORDER BY updated DESC LIMIT 50',(owner,))]

    def get(self,owner,ident):
        with self.connect() as db:return self.public(self.row(db,owner,ident),True)

    def put(self,owner,data):
        if set(data)!={'id','title','messages','expected_revision'}:raise ValueError('invalid_history_fields')
        ident=validate_conversation_id(data['id']);title=data['title'];revision=data['expected_revision']
        if not isinstance(title,str) or not 1<=len(title.strip())<=80 or type(revision) is not int or revision<0:
            raise ValueError('invalid_history_fields')
        raw=transcript(data['messages'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT revision,deleted FROM chat_histories WHERE owner=? AND id=?',(owner,ident)).fetchone()
            if old and old['deleted']:raise HistoryError('history_not_found')
            if (old['revision'] if old else 0)!=revision:raise HistoryError('history_changed')
            if not old and db.execute('SELECT count(*) FROM chat_histories WHERE owner=? AND deleted=0',(owner,)).fetchone()[0]>=50:
                raise HistoryError('history_count_limit')
            if not old and db.execute('SELECT count(*) FROM chat_histories WHERE owner=?',(owner,)).fetchone()[0]>=500:
                raise HistoryError('history_count_limit')
            db.execute('INSERT OR REPLACE INTO chat_histories VALUES (?,?,?,?,?,?,0)',
                (owner,ident,title.strip(),raw,revision+1,self.clock()))
            return self.public(self.row(db,owner,ident))

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
