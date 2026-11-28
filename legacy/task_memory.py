"""本人确认的原话摘录；不把聊天文字认证为设备事实或授权。"""
import hashlib
import json
from legacy.rolling_context import RollingContext
from retrieval.public_evidence import PublicEvidence

TTL=7*86400
NOTE='用户确认的历史原话，仅供任务背景；不是核实事实、知识来源或设备权限。'
KINDS={'goal':'目标','constraint':'约束','question':'待解决问题'}


class MemoryError(ValueError):
    pass


def fingerprint(messages):
    # 文本来源不因新增上下文资格字段而变化；保持旧记忆哈希兼容。
    text=[{'role':m['role'],'content':m['content']} for m in messages]
    return hashlib.sha256(json.dumps(text,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


class TaskMemory:
    def __init__(self,history):
        self.history=history
        self.rolling=RollingContext(history)
        self.public_evidence=PublicEvidence(history)
        with history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS task_memories ('
                'owner TEXT,conversation TEXT,revision INTEGER,source_count INTEGER,source_hash TEXT,'
                'entries TEXT,updated REAL,expires REAL,withdrawn INTEGER,PRIMARY KEY(owner,conversation))')

    def source(self,db,owner,conversation):
        row=self.history.row(db,owner,conversation)
        return row,json.loads(row['messages'])

    def record(self,db,owner,conversation):
        return db.execute('SELECT * FROM task_memories WHERE owner=? AND conversation=?',(owner,conversation)).fetchone()

    def public(self,row,messages):
        if not row:return {'state':'empty','revision':0,'entries':[],'note':NOTE}
        state=('withdrawn' if row['withdrawn'] else 'expired' if self.history.clock()>=row['expires']
            else 'source_changed' if len(messages)<row['source_count'] or fingerprint(messages[:row['source_count']])!=row['source_hash']
            else 'active')
        return {'state':state,'revision':row['revision'],'entries':json.loads(row['entries']) if state=='active' else [],
            'updated':row['updated'],'expires':row['expires'],'source_count':row['source_count'],'note':NOTE}

    def get(self,owner,conversation):
        with self.history.connect() as db:
            source,messages=self.source(db,owner,conversation)
            result=self.public(self.record(db,owner,conversation),messages)
            result['history_revision']=source['revision'];return result

    def draft(self,owner,conversation):
        # 只摘录用户原话，不额外调用模型；长原话显示截取边界，不称自动语义摘要。
        with self.history.connect() as db:
            source,messages=self.source(db,owner,conversation)
            candidates=[{'message_index':i,'quote':m['content'][:400],'truncated':len(m['content'])>400,
                'outside_recent_window':i<max(0,len(messages)-8)} for i,m in enumerate(messages) if m['role']=='user']
            return {'history_revision':source['revision'],'candidates':candidates,'note':NOTE}

    def save(self,owner,conversation,data):
        if set(data)!={'expected_revision','history_revision','entries','confirmed'}:raise ValueError('invalid_memory_fields')
        if data['confirmed'] is not True:raise MemoryError('memory_confirmation_required')
        if any(type(data[k]) is not int or data[k]<0 for k in ('expected_revision','history_revision')):raise ValueError('invalid_revision')
        entries=data['entries']
        if not isinstance(entries,list) or not 1<=len(entries)<=6:raise ValueError('invalid_memory_entries')
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE');source,messages=self.source(db,owner,conversation)
            old=self.record(db,owner,conversation)
            if (old['revision'] if old else 0)!=data['expected_revision']:raise MemoryError('memory_changed')
            if source['revision']!=data['history_revision']:raise MemoryError('memory_source_changed')
            seen=set()
            for entry in entries:
                if not isinstance(entry,dict) or set(entry)!={'kind','message_index','quote'}:raise ValueError('invalid_memory_entry')
                index=entry['message_index'];quote=entry['quote']
                if entry['kind'] not in KINDS or type(index) is not int or not 0<=index<len(messages) or index in seen:
                    raise ValueError('invalid_memory_entry')
                if not isinstance(quote,str) or not 1<=len(quote)<=400 or messages[index]['role']!='user' or not messages[index]['content'].startswith(quote):
                    raise MemoryError('memory_source_changed')
                seen.add(index)
            now=self.history.clock()
            db.execute('INSERT OR REPLACE INTO task_memories VALUES (?,?,?,?,?,?,?,?,0)',
                (owner,conversation,data['expected_revision']+1,len(messages),fingerprint(messages),
                 json.dumps(entries,ensure_ascii=False),now,now+TTL))
            return self.public(self.record(db,owner,conversation),messages)

    def withdraw(self,owner,conversation,revision):
        if type(revision) is not int:raise ValueError('invalid_revision')
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.source(db,owner,conversation);row=self.record(db,owner,conversation)
            if not row or row['revision']!=revision:raise MemoryError('memory_changed')
            db.execute("UPDATE task_memories SET revision=revision+1,entries='[]',withdrawn=1 WHERE owner=? AND conversation=?",(owner,conversation))
            return {'state':'withdrawn','revision':revision+1,'entries':[],'note':NOTE}

    def require(self,owner,conversation,revision):
        if type(revision) is not int:raise ValueError('invalid_revision')
        result=self.get(owner,conversation)
        if result['state']!='active' or result['revision']!=revision:raise MemoryError('memory_not_active')
        return {'kind':'user_confirmed_excerpts','note':NOTE,'entries':result['entries'],
            'revision':revision,'updated':result['updated'],'expires':result['expires']}
