"""有归属、期限和容量上限的纯文本材料；不解析PDF、图片或可执行文件。"""
import base64
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from legacy.conversation_topic import validate_conversation_id

MAX_BYTES=256*1024
OWNER_BYTES=1024*1024
TOTAL_BYTES=32*1024*1024
LIFETIME=24*3600


class MaterialError(ValueError):pass


def decode_upload(data):
    if not isinstance(data,dict) or set(data)!={'conversation_id','name','data_base64'}:raise ValueError('invalid_material')
    conversation=validate_conversation_id(data['conversation_id']);name=data['name'];encoded=data['data_base64']
    if (not isinstance(name,str) or not 1<=len(name)<=120 or name!=name.strip()
        or re.search(r'[/\\:\x00-\x1f\x7f]',name) or not name.lower().endswith(('.txt','.log'))):
        raise MaterialError('material_type_not_supported')
    if not isinstance(encoded,str) or len(encoded)>4*((MAX_BYTES+2)//3):raise MaterialError('material_too_large')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError):raise MaterialError('material_invalid_text') from None
    if not raw or len(raw)>MAX_BYTES:raise MaterialError('material_too_large')
    if raw.startswith((b'%PDF-',b'PK\x03\x04',b'\x89PNG',b'\xff\xd8\xff')):raise MaterialError('material_type_not_supported')
    try:text=raw.decode('utf-8-sig')
    except UnicodeError:raise MaterialError('material_invalid_text') from None
    if not text.strip() or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',text):
        raise MaterialError('material_invalid_text')
    return conversation,name,raw,text


def chunks(text):
    """保留原始行号；超长单行按列分段，每段仍能定位原文。"""
    result=[];rows=[];size=0
    for number,line in enumerate(text.splitlines(),1):
        for offset in range(0,max(1,len(line)),1000):
            part=line[offset:offset+1000]
            label='L'+str(number)
            if len(line)>1000:label+=':C'+str(offset+1)+'-C'+str(offset+len(part))
            display=label+' | '+part
            if rows and size+len(display)+1>1800:
                result.append(rows);rows=[];size=0
            rows.append({'line':number,'column':offset+1,'text':part,'display':display})
            size+=len(display)+1
    if rows:result.append(rows)
    return result


def select_excerpts(material,question):
    parts=chunks(material['text'])
    terms=set(re.findall(r'[a-zA-Z0-9_./:-]{2,}',question.lower()))
    for phrase in re.findall(r'[\u4e00-\u9fff]+',question):
        terms.update(phrase[i:i+2] for i in range(len(phrase)-1))
    terms-={'分析','材料','日志','这份','请帮','帮我','这个','一下','什么','文件','找出','异常','关键信息','指出'}
    scores=[]
    for rows in parts:
        text='\n'.join(row['text'] for row in rows).lower()
        score=sum(min(text.count(term),3) for term in terms)
        score+=0.1*sum(word in text for word in ('error','failed','exception','panic','timeout','错误','失败'))
        scores.append(score)
    best=max(range(len(parts)),key=lambda i:scores[i])
    if scores[best]>0:
        indices=sorted({max(0,best-1),best,min(len(parts)-1,best+1)})
    else:indices=sorted({0,len(parts)//2,len(parts)-1})
    sources=[]
    for index in indices:
        rows=parts[index];first,last=rows[0],rows[-1]
        start,end=first['line'],last['line']
        location='第{}–{}行'.format(start,end)
        if start==end:location='第{}行（起始列{}）'.format(start,first['column'])
        sources.append({'id':'MAT-'+str(len(sources)+1),'title':material['name']+' · '+location,
            'kind':'user_material','origin':'user_upload','source_scope':'user_provided_unverified',
            'material_id':material['id'],'sha256':material['sha256'],'line_start':start,'line_end':end,
            'column_start':first['column'],'historical_finding':'\n'.join(row['display'] for row in rows),
            'verification_limits':'用户提供的材料，不是本轮设备读取；不能确认当前状态或故障根因。'})
    return {'sources':sources,'selection_complete':len(indices)==len(parts),
        'selected_chunks':len(indices),'total_chunks':len(parts),'total_lines':len(material['text'].splitlines()),
        'selection_method':'lexical_window_with_adjacent_context_or_head_middle_tail',
        'note':'只分析所列原文片段；词面选段不保证覆盖全部相关内容，未被选中的原文仍可查看。'}


class MaterialStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.clock=clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS text_materials ('
                'id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation TEXT NOT NULL,name TEXT NOT NULL,'
                'raw BLOB NOT NULL,text TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL)')

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

    def purge(self,db):db.execute('DELETE FROM text_materials WHERE created<=?',(self.clock()-LIFETIME,))

    def public(self,row):
        return {
            'id':row['id'],'conversation_id':row['conversation'],'name':row['name'],'bytes':row['bytes'],
            'sha256':row['sha256'],'created_at':row['created'],'expires_at':row['created']+LIFETIME,
            'lines':len(row['text'].splitlines()),'origin':'user_upload','current_state_verified':False}

    def add(self,owner,data):
        conversation,name,raw,text=decode_upload(data);ident=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.purge(db)
            count,size=db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM text_materials WHERE owner=?',(owner,)).fetchone()
            if count>=10 or size+len(raw)>OWNER_BYTES:raise MaterialError('material_owner_limit')
            total=db.execute('SELECT coalesce(sum(bytes),0) FROM text_materials').fetchone()[0]
            if total+len(raw)>TOTAL_BYTES:raise MaterialError('material_store_full')
            db.execute('INSERT INTO text_materials VALUES (?,?,?,?,?,?,?,?,?)',
                (ident,owner,conversation,name,raw,text,len(raw),hashlib.sha256(raw).hexdigest(),self.clock()))
            return self.public(self.row(db,owner,ident))

    def row(self,db,owner,ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):raise MaterialError('material_not_found')
        row=db.execute('SELECT * FROM text_materials WHERE owner=? AND id=? AND created>?',
                       (owner,ident,self.clock()-LIFETIME)).fetchone()
        if not row:raise MaterialError('material_not_found')
        return row

    def get(self,owner,ident):
        with self.connect() as db:
            row=self.row(db,owner,ident);result=self.public(row)
            result.update(text=row['text'],data_base64=base64.b64encode(row['raw']).decode('ascii'))
            return result

    def list(self,owner,conversation):
        validate_conversation_id(conversation)
        with self.connect() as db:
            return [self.public(row) for row in db.execute(
                'SELECT * FROM text_materials WHERE owner=? AND conversation=? AND created>? ORDER BY created DESC LIMIT 10',
                (owner,conversation,self.clock()-LIFETIME))]

    def delete(self,owner,ident):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.row(db,owner,ident)
            db.execute('DELETE FROM text_materials WHERE owner=? AND id=?',(owner,ident))
        return {'status':'deleted'}

    def select(self,owner,ident,conversation,question):
        material=self.get(owner,ident)
        if material['conversation_id']!=conversation:raise MaterialError('material_not_found')
        return self.public_material(material),select_excerpts(material,question)

    def require_active(self,owner,ident,conversation):
        """每次模型步骤前后重验删除/到期；不再次提取或发送全文。"""
        with self.connect() as db:
            row=self.row(db,owner,ident)
            if row['conversation']!=conversation:raise MaterialError('material_not_found')

    def public_material(self,material):
        return {key:value for key,value in material.items() if key not in ('text','data_base64')}
