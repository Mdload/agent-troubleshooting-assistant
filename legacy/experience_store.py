"""所选纠正的草稿、审核与发布；不调用模型、不收集整段聊天。"""
import copy
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from legacy.experience_learning import safe_text, validate_conditions, source_snapshot, fingerprint, utc_now
from legacy.conversation_topic import validate_conversation_id


class ExperienceError(ValueError):
    pass


EDIT_FIELDS = {'corrected_answer','reason','conditions','reference_id','quote'}
SUBMIT_FIELDS = EDIT_FIELDS | {'conversation_id','question_id','answer_id','question','original_answer','consent'}
PAYLOAD_FIELDS = ('question','candidate_answer','correction_reason','conditions','verification_sources','reference_id')


class ExperienceStore:
    def __init__(self, path, root=None):
        self.path=Path(path)
        self.root=Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS experiences ('
                'id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation TEXT NOT NULL,'
                'candidate_hash TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,'
                'UNIQUE(owner,candidate_hash))')
            db.execute('CREATE INDEX IF NOT EXISTS experience_status ON experiences(status)')
            db.execute('CREATE TABLE IF NOT EXISTS experience_events ('
                'seq INTEGER PRIMARY KEY,id TEXT NOT NULL,event TEXT NOT NULL,at TEXT NOT NULL,hash TEXT NOT NULL)')

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

    def reference(self, ident):
        import retrieval.qa as qa
        reference=next((r for r in qa.KNOWLEDGE if r['id']==ident),None)
        if reference is None: raise ValueError('Unknown reference')
        source=qa.SOURCE_MAP.get('DOC-'+ident)
        if source is None: raise ValueError('Unknown reference document')
        return reference,source

    def material(self, question, data):
        if set(data)!=EDIT_FIELDS: raise ValueError('Invalid experience fields')
        reference,source=self.reference(data['reference_id'])
        quote=safe_text(data['quote'],500)
        if quote not in reference['historical_finding']: raise ValueError('Quote must belong to reference section')
        snapshot=source_snapshot({'path':source['path'],'quote':quote},self.root)
        conditions=validate_conditions(data['conditions'])
        conditions['software_versions']=list(dict.fromkeys('v'+v.lstrip('v') for v in conditions['software_versions']))
        for field,source_field in (('operating_modes','operating_modes'),('device_groups','device_groups'),
                                   ('software_versions','software_versions')):
            allowed=reference.get(source_field,[])
            if field=='software_versions':allowed=['v'+v.lstrip('v') for v in allowed]
            if allowed and (not conditions[field] or not set(conditions[field])<=set(allowed)):
                raise ValueError('Experience scope exceeds reference')
        if (conditions['lesson_type']=='historical_case' and reference.get('evidence_date') and
                conditions['evidence_date']!=reference['evidence_date']):
            raise ValueError('Historical date must match reference evidence date')
        return {'question':safe_text(question,1000),'candidate_answer':safe_text(data['corrected_answer'],3000),
                'correction_reason':safe_text(data['reason'],1000),'conditions':conditions,
                'verification_sources':[snapshot],'reference_id':reference['id']}

    def valid(self, item):
        if (item.get('sharing_consent') or {}).get('uses')!=['admin_review','shared_knowledge','inferera_answer_context']:
            raise ExperienceError('experience_consent_required')
        payload={key:item[key] for key in PAYLOAD_FIELDS}
        if fingerprint(payload)!=item['candidate_hash']: raise ExperienceError('experience_changed')
        try:
            recreated=self.material(item['question'],{'corrected_answer':item['candidate_answer'],
                'reason':item['correction_reason'],'conditions':item['conditions'],
                'reference_id':item['reference_id'],'quote':item['verification_sources'][0]['quote']})
        except (ValueError,OSError):raise ExperienceError('experience_source_changed') from None
        if recreated!=payload: raise ExperienceError('experience_source_changed')
        return True

    def row(self,db,ident,owner=None):
        row=db.execute('SELECT owner,payload FROM experiences WHERE id=?',(ident,)).fetchone()
        if row is None or owner is not None and row['owner']!=owner: raise ExperienceError('experience_not_found')
        return json.loads(row['payload'])

    def write(self,db,item):
        db.execute('UPDATE experiences SET candidate_hash=?,status=?,payload=? WHERE id=?',
            (item['candidate_hash'],item['status'],json.dumps(item,ensure_ascii=False),item['id']))

    def event(self,db,item,event):
        db.execute('INSERT INTO experience_events(id,event,at,hash) VALUES (?,?,?,?)',
                   (item['id'],event,utc_now(),item['candidate_hash']))

    def public(self,item):
        result=copy.deepcopy(item)
        try: self.valid(item); result['source_current']=True
        except (ValueError,OSError,KeyError,TypeError): result['source_current']=False
        if not result['source_current']:
            result['effective_status']='review_invalid'
        else: result['effective_status']=item['status']
        return result

    def list(self,owner=None):
        with self.connect() as db:
            rows=db.execute('SELECT payload FROM experiences'+(' WHERE owner=?' if owner else '')+
                ' ORDER BY rowid DESC LIMIT 100',(owner,) if owner else ()).fetchall()
        return [self.public(json.loads(r['payload'])) for r in rows]

    def get(self,ident,owner=None):
        with self.connect() as db:return self.public(self.row(db,ident,owner))

    def submit(self,owner,data):
        if set(data)!=SUBMIT_FIELDS: raise ValueError('Invalid experience submission')
        if data['consent'] is not True: raise ExperienceError('experience_consent_required')
        conversation=validate_conversation_id(data['conversation_id'])
        for field in ('question_id','answer_id'):
            if not isinstance(data[field],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',data[field]):
                raise ValueError('Invalid message ID')
        if data['question_id']==data['answer_id']: raise ValueError('Question and answer IDs differ')
        original=safe_text(data['original_answer'],6000)
        payload=self.material(data['question'],{key:data[key] for key in EDIT_FIELDS})
        digest=fingerprint(payload)
        provenance={'conversation_id':conversation,'question_id':data['question_id'],'answer_id':data['answer_id'],
                    'original_answer_sha256':fingerprint(original),'origin':'user_selected_browser_history_unverified'}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT payload FROM experiences WHERE owner=? AND candidate_hash=?',(owner,digest)).fetchone()
            if prior:
                item=json.loads(prior['payload'])
                if provenance not in item['provenance']:
                    item['provenance'].append(provenance);self.write(db,item);self.event(db,item,'deduplicated')
                return self.public(item)
            if db.execute('SELECT count(*) FROM experiences WHERE owner=?',(owner,)).fetchone()[0]>=100:
                raise ExperienceError('experience_limit')
            if db.execute('SELECT count(*) FROM experiences').fetchone()[0]>=5000:
                raise ExperienceError('experience_limit')
            item=dict(payload,id=uuid.uuid4().hex,candidate_hash=digest,status='needs_review',review=None,
                      publication=None,provenance=[provenance],created_at_utc=utc_now(),
                      sharing_consent={'uses':['admin_review','shared_knowledge','inferera_answer_context'],'at_utc':utc_now()},
                      current_state_supported=False,grants_permissions=False)
            db.execute('INSERT INTO experiences VALUES (?,?,?,?,?,?)',
                (item['id'],owner,conversation,digest,item['status'],json.dumps(item,ensure_ascii=False)))
            self.event(db,item,'submitted')
        return self.public(item)

    def update(self,ident,owner,expected_hash,data):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');item=self.row(db,ident,owner)
            if item['candidate_hash']!=expected_hash: raise ExperienceError('experience_changed')
            material=self.material(item['question'],data)
            item.update(material,candidate_hash=fingerprint(material),status='needs_review',review=None,publication=None)
            try:self.write(db,item)
            except sqlite3.IntegrityError:raise ExperienceError('experience_duplicate') from None
            self.event(db,item,'edited_review_invalidated')
        return self.public(item)

    def review(self,ident,decision,expected_hash,note,regression_question,actor):
        if actor!='authenticated-admin': raise ExperienceError('admin_required')
        if decision not in ('approve','reject'):raise ValueError('Invalid review decision')
        note=safe_text(note,1000)
        if decision=='approve':regression_question=safe_text(regression_question,1000)
        elif regression_question is not None:raise ValueError('Reject has no regression question')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');item=self.row(db,ident)
            if item['candidate_hash']!=expected_hash:raise ExperienceError('experience_changed')
            if item['status']!='needs_review':raise ExperienceError('experience_not_pending')
            if decision=='approve':self.valid(item)
            item['status']='approved' if decision=='approve' else 'rejected'
            item['review']={'decision':decision,'reviewer':actor,'note':note,'candidate_hash':expected_hash,
                            'regression_question':regression_question,'reviewed_at_utc':utc_now()}
            self.write(db,item);self.event(db,item,'review_'+decision)
        return self.public(item)

    def record(self,item):
        """只构造来源范围内的派生资料；不赋予新的主题、现场状态或工具权限。"""
        from retrieval.knowledge_coverage import record_coverage
        self.valid(item)
        review=item.get('review') or {}
        if (review.get('decision')!='approve' or review.get('reviewer')!='authenticated-admin' or
                review.get('candidate_hash')!=item['candidate_hash']):
            raise ExperienceError('experience_not_approved')
        reference,_=self.reference(item['reference_id'])
        coverage=copy.deepcopy(record_coverage(reference))
        if item['conditions']['lesson_type']=='historical_case':
            coverage['supports']={topic:['history'] for topic in coverage['supports']}
        else:
            # 原理归纳没有复制整篇原文；不继承清单、配置、端口或示例操作的覆盖。
            coverage['supports']={topic:['mechanism'] for topic,aspects in coverage['supports'].items()
                                  if 'mechanism' in aspects}
        coverage.update(scope='reviewed_derivation',current_state_supported=False)
        return {'id':'EXP-'+item['id'],'kind':'reviewed_experience','title':item['question'][:100],
            'symptom':item['question'],'historical_finding':item['candidate_answer'],'historical_outcome':'',
            'operating_modes':item['conditions']['operating_modes'],'device_groups':item['conditions']['device_groups'],
            'software_versions':item['conditions']['software_versions'],'evidence_date':item['conditions']['evidence_date'],
            'documentation_date':item['created_at_utc'][:10],'verification_limits':'管理员审核的经验归纳；不能证明当前设备状态或授予操作权限。',
            'reference_id':reference['id'],'verification_sources':item['verification_sources'],
            'knowledge_domain':'basics' if reference['kind']=='lab_basics' else 'topology',
            'knowledge_libraries':reference['knowledge_libraries'],'device_types':reference.get('device_types',[]),
            'deployment_environments':reference.get('deployment_environments',[]),'version_status':reference.get('version_status','unconfirmed'),
            'source_scope':'public_principle' if reference['kind']=='lab_basics' else 'reviewed_derivation',
            'knowledge_coverage':coverage,'sources':reference['sources'],
            'current_state_supported':False,'grants_permissions':False,'repair_enabled':False}

    def regression(self,item):
        import retrieval.qa as qa
        from retrieval.knowledge_coverage import assess
        from retrieval.lab_basics import is_basics_query
        record=self.record(item)
        question=item['review']['regression_question']
        scope=item['conditions']
        mode=scope['operating_modes'][0] if scope['operating_modes'] else 'unknown'
        group=scope['device_groups'][0] if scope['device_groups'] else 'unknown'
        results={}
        for label,text in (('original_coverage',item['question']),('regression_coverage',question)):
            results[label]=assess(text,[record],mode=mode,group=group,basics=is_basics_query(text))['status']=='supported'
        results['retrieval_match']=any(hit['case']['id']==record['id'] for hit in
            qa.retrieve(question,mode=mode,group=group,extra_records=[record]))
        results['applicability_match']=self.applicable(item,question)
        return dict(results,passed=all(results.values()),model_calls=0,semantic_verified=False,
                    note='只核对范围、检索与引用兼容性；答案事实由管理员对照来源审核。')

    def publish(self,ident,expected_hash,actor):
        if actor!='authenticated-admin':raise ExperienceError('admin_required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');item=self.row(db,ident)
            if item['candidate_hash']!=expected_hash:raise ExperienceError('experience_changed')
            if item['status']!='approved':raise ExperienceError('experience_not_approved')
            if db.execute("SELECT count(*) FROM experiences WHERE status='published'").fetchone()[0]>=500:
                raise ExperienceError('experience_limit')
            check=self.regression(item)
            if not check['passed']:raise ExperienceError('experience_regression_failed')
            item.update(status='published',publication={'candidate_hash':expected_hash,'at_utc':utc_now(),'checks':check})
            self.write(db,item);self.event(db,item,'published')
        return self.public(item)

    def withdraw(self,ident,expected_hash,actor):
        if actor!='authenticated-admin':raise ExperienceError('admin_required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');item=self.row(db,ident)
            if item['candidate_hash']!=expected_hash:raise ExperienceError('experience_changed')
            if item['status']!='published':raise ExperienceError('experience_not_published')
            item.update(status='needs_review',review=None,publication=None)
            self.write(db,item);self.event(db,item,'withdrawn')
        return self.public(item)

    def applicable(self,item,question):
        from retrieval.knowledge_coverage import requirements
        needs=requirements(question)
        modes={need['mode'] for need in needs}
        groups={group for need in needs for group in need['groups']}
        text=question.lower()
        versions=set(re.findall(r'(?<![a-z0-9])v\d+(?:\.\d+)+(?![a-z0-9])',text))
        versions.update('v'+v for v in re.findall(r'(?:版本\s*[:：]?\s*|croc\s*)(\d+(?:\.\d+)+)',text))
        conditions=item['conditions']
        if conditions['operating_modes'] and (not modes or not modes<=set(conditions['operating_modes'])):return False
        if conditions['device_groups'] and (not groups or not groups<=set(conditions['device_groups'])):return False
        allowed_versions=set(conditions['software_versions'])
        return not allowed_versions or bool(versions) and versions<=allowed_versions

    def records(self,question=None):
        """每次检索重新核对来源和审核，适用条件不明确时不猜测。"""
        with self.connect() as db:
            rows=db.execute("SELECT payload FROM experiences WHERE status='published' LIMIT 500").fetchall()
        records=[]
        for row in rows:
            item=json.loads(row['payload'])
            publication=item.get('publication') or {}
            if (item.get('status')!='published' or publication.get('candidate_hash')!=item['candidate_hash'] or
                    (publication.get('checks') or {}).get('passed') is not True):continue
            if question is not None and not self.applicable(item,question):continue
            try:records.append(self.record(item))
            except (ValueError,OSError,KeyError,TypeError):continue
        return records

    def assert_active_sources(self,sources):
        """已缓存的经验也须在调用前后核对，防止撤回/修改后继续纠正。"""
        selected=[s for s in sources if s.get('kind')=='reviewed_experience']
        if not selected:return
        with self.connect() as db:
            for source in selected:
                ident=source['id'][4:]
                row=db.execute("SELECT payload FROM experiences WHERE id=? AND status='published'",(ident,)).fetchone()
                if not row:raise ExperienceError('experience_source_changed')
                item=json.loads(row['payload']);publication=item.get('publication') or {}
                if publication.get('candidate_hash')!=item['candidate_hash'] or (publication.get('checks') or {}).get('passed') is not True:
                    raise ExperienceError('experience_source_changed')
                current=self.record(item)
                if fingerprint({key:current.get(key) for key in source})!=fingerprint(source):
                    raise ExperienceError('experience_source_changed')

    def references(self):
        import retrieval.qa as qa
        return [{'id':r['id'],'title':r['title'],'text':r['historical_finding'],
                 'operating_modes':r['operating_modes'],'device_groups':r['device_groups'],
                 'evidence_date':r['evidence_date']} for r in qa.KNOWLEDGE]
