"""持久任务与批准范围；提问者确认本人任务，不接受网页传入身份或范围。"""
import json
import sqlite3
import time
import uuid
from devices.device_tools import DEVICES,CHECKS,DEVICE_LABELS
from legacy.unified_chat import DIAGNOSTIC_GOALS, ToolRejection
from legacy.interaction_mode import validate_selection, allows_scope, SCENES
from devices.device_registry import CHECK_LABELS,allows_check
from devices.diagnostic_intake import READ_ONLY_PROFILE
from legacy.conversation_topic import validate_conversation_id, join_diagnostic_question
from legacy.investigation_limits import legacy_limits, requested_limits, validate_limits, stage_budget

SINGLE_APPROVAL = 'single_scope_v1'
DIAGNOSTIC_DATA_USE = 'inferera_diagnostic_v1'
MAX_TASK_INPUTS = 6
ANY_TOPIC = object()
AUTO_READ_ACTOR = 'registered-read-only-policy'
LOCAL_READ_USE = 'local_read_only_v1'
PASSIVE_CHECKS = frozenset(READ_ONLY_PROFILE) | {'routes'}


def passive_scope(scope):
    return bool(scope) and all(checks and set(checks)<=PASSIVE_CHECKS for checks in scope.values())


class TaskError(ValueError):
    pass


def validate_scope(scope):
    if not isinstance(scope,dict) or not scope or len(scope)>4:
        raise TaskError('invalid_scope')
    for device,checks in scope.items():
        if (device not in DEVICES or not isinstance(checks,list) or not checks or
            len(checks)!=len(set(checks)) or any(not allows_check(device,c) for c in checks)):
            raise TaskError('invalid_scope')
    return json.loads(json.dumps(scope))


class ApprovalConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        # 保留 Connection 接口与事务行为，退出事务后立即释放文件句柄。
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class ApprovalStore:
    def __init__(self,path,clock=time.time):
        self.path=str(path);self.clock=clock
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_tasks ('
                'id TEXT PRIMARY KEY, owner TEXT NOT NULL, question TEXT NOT NULL, history TEXT NOT NULL,'
                'scope TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL, approved_scope TEXT,'
                'approver TEXT, result TEXT, attempts INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_events ('
                'task TEXT, kind TEXT, body TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_reads ('
                'task TEXT, device TEXT, check_name TEXT, UNIQUE(task,device,check_name))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_progress ('
                'task TEXT PRIMARY KEY, started REAL NOT NULL, updated REAL NOT NULL, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_inputs ('
                       'task TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, UNIQUE(task,id))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_sessions (owner TEXT PRIMARY KEY,expires REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_data_permissions ('
                'task TEXT PRIMARY KEY, flow TEXT NOT NULL, requested_use TEXT NOT NULL, approved_use TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_limits ('
                'task TEXT PRIMARY KEY, requested TEXT NOT NULL, approved TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_stages ('
                'task TEXT, number INTEGER, reads INTEGER NOT NULL, started REAL NOT NULL,'
                'updated REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(task,number))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_topics ('
                'owner TEXT NOT NULL, conversation TEXT NOT NULL, question TEXT NOT NULL,'
                'domain TEXT NOT NULL, intent TEXT NOT NULL, target_hint TEXT NOT NULL, expires REAL NOT NULL,'
                'PRIMARY KEY(owner,conversation))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_intake ('
                'owner TEXT NOT NULL, conversation TEXT NOT NULL, topic_expires REAL NOT NULL, '
                'body TEXT NOT NULL, PRIMARY KEY(owner,conversation))')
            db.execute('CREATE TRIGGER IF NOT EXISTS workflow_delete_intake AFTER DELETE ON workflow_topics '
                'BEGIN DELETE FROM workflow_intake WHERE owner=old.owner AND conversation=old.conversation; END')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_modes ('
                'owner TEXT NOT NULL, conversation TEXT NOT NULL, mode TEXT NOT NULL,'
                'scene TEXT, revision INTEGER NOT NULL, PRIMARY KEY(owner,conversation))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_investigation_turns ('
                'owner TEXT NOT NULL, conversation TEXT NOT NULL, scene TEXT NOT NULL,'
                'revision INTEGER NOT NULL, kind TEXT NOT NULL, question TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS investigation_turn_scope ON workflow_investigation_turns '
                       '(owner,conversation,scene,revision,kind)')
            # 旧审批没有对话/入口绑定，保留历史但不继续执行旧授权。
            columns = {r['name'] for r in db.execute('PRAGMA table_info(workflow_tasks)')}
            for name, definition in (('conversation', "TEXT NOT NULL DEFAULT ''"),
                                      ('scene', 'TEXT'), ('mode_revision', 'INTEGER'),
                                      ('server_owned', 'INTEGER NOT NULL DEFAULT 0'), ('diagnostic_goal', 'TEXT')):
                if name not in columns:
                    db.execute('ALTER TABLE workflow_tasks ADD COLUMN '+name+' '+definition)
            old = db.execute("SELECT id FROM workflow_tasks WHERE conversation='' "
                             "AND status IN ('pending','approved','running')").fetchall()
            for row in old:
                db.execute("UPDATE workflow_tasks SET status='cancelled' WHERE id=?", (row['id'],))
                self.event(db, row['id'], 'cancelled', {'reason': 'interaction_upgrade'})

    def interaction_row(self, db, owner, conversation):
        row = db.execute('SELECT mode,scene,revision FROM workflow_modes WHERE owner=? AND conversation=?',
                         (owner, conversation)).fetchone()
        return dict(row) if row else {'mode': 'qa', 'scene': None, 'revision': 0}

    def interaction(self, owner, conversation, include_active=False):
        with self.connect() as db:
            if include_active:
                db.execute('BEGIN')
            state=self.interaction_row(db,owner,conversation)
            if include_active and state['mode'] in ('diagnostic','assistant'):
                scene=state['scene'] if state['mode']=='diagnostic' else 'ha_topology'
                rows=db.execute(
                    "SELECT t.* FROM workflow_tasks t JOIN workflow_data_permissions p ON p.task=t.id "
                    "WHERE t.owner=? AND t.conversation=? AND t.scene=? AND t.mode_revision=? "
                    "AND t.expires>? AND p.flow=? AND (t.status='pending' "
                    "OR (t.server_owned=1 AND t.status IN ('approved','running'))) ORDER BY t.rowid LIMIT 5",
                    (owner,conversation,scene,state['revision'],self.clock(),SINGLE_APPROVAL)).fetchall()
                if rows:
                    state['active_tasks']=[self.public(row,db) for row in rows]
            return state

    def interactions(self, owner):
        with self.connect() as db:
            return {row['conversation']:{key:row[key] for key in ('mode','scene','revision')}
                    for row in db.execute('SELECT conversation,mode,scene,revision FROM workflow_modes WHERE owner=?',(owner,))}

    def set_interaction(self, owner, conversation, mode, scene=None):
        validate_selection(mode, scene)
        if mode == 'diagnostic' and not SCENES[scene]['devices']:
            raise TaskError('scene_unavailable')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.interaction_row(db, owner, conversation)
            if old['mode'] == mode and old['scene'] == scene:
                return dict(old, cancelled_tasks=[])
            exists = db.execute('SELECT 1 FROM workflow_modes WHERE owner=? AND conversation=?',
                                (owner, conversation)).fetchone()
            if not exists and db.execute('SELECT count(*) FROM workflow_modes WHERE owner=?', (owner,)).fetchone()[0] >= 50:
                raise TaskError('conversation_limit')
            tasks = db.execute("SELECT id FROM workflow_tasks WHERE owner=? AND conversation=? "
                               "AND status IN ('pending','approved','running')", (owner, conversation)).fetchall()
            for row in tasks:
                db.execute("UPDATE workflow_tasks SET status='cancelled' WHERE id=?", (row['id'],))
                self.end_progress(db,row['id'],'cancelled')
                self.event(db, row['id'], 'cancelled', {'reason': 'interaction_changed'})
            db.execute('DELETE FROM workflow_topics WHERE owner=? AND conversation=?', (owner, conversation))
            db.execute('DELETE FROM workflow_investigation_turns WHERE owner=? AND conversation=?',(owner,conversation))
            revision = old['revision'] + 1
            db.execute('INSERT OR REPLACE INTO workflow_modes VALUES (?,?,?,?,?)',
                       (owner, conversation, mode, scene, revision))
            return {'mode': mode, 'scene': scene, 'revision': revision,
                    'cancelled_tasks': [r['id'] for r in tasks]}

    def task_interaction_active(self, db, row):
        current = self.interaction_row(db, row['owner'], row['conversation'])
        return (bool(row['conversation']) and current['mode'] in ('diagnostic','assistant')
                and (('ha_topology' if current['mode']=='assistant' else current['scene']) == row['scene']) and current['revision'] == row['mode_revision']
                and allows_scope(row['scene'], json.loads(row['scope'])))

    def topic_row(self,db,owner,conversation):
        row=db.execute('SELECT t.question,t.domain,t.intent,t.target_hint,i.body FROM workflow_topics t '
            'LEFT JOIN workflow_intake i ON i.owner=t.owner AND i.conversation=t.conversation '
            'AND i.topic_expires=t.expires WHERE t.owner=? AND t.conversation=? AND t.expires>?',
            (owner,conversation,self.clock())).fetchone()
        if row is None:return None
        topic=dict(row)
        body=topic.pop('body')
        if body is not None:topic['intake']=json.loads(body)
        return topic

    def topic(self,owner,conversation):
        with self.connect() as db: return self.topic_row(db,owner,conversation)

    def last_probe(self,owner,conversation,revision):
        """Find the latest completed ICMP task in this conversation for method questions."""
        from devices.device_registry import PING_TARGETS, PROBE_CHECKS
        with self.connect() as db:
            rows=db.execute("SELECT id,scope,result,status FROM workflow_tasks WHERE owner=? AND conversation=? AND mode_revision=? "
                            "AND status IN ('completed','incomplete') AND result IS NOT NULL ORDER BY rowid DESC LIMIT 12",
                            (owner,conversation,revision))
            for row in rows:
                attempts = json.loads(row['result']).get('evidence', [])
                if row['status'] == 'incomplete' and any(item.get('check') in PROBE_CHECKS for item in attempts):
                    return None
                evidence=[item for item in attempts
                          if item.get('ok') is True and item.get('origin')=='device_read'
                          and item.get('freshness')=='current' and item.get('check') in PROBE_CHECKS]
                if evidence:
                    scope=json.loads(row['scope'])
                    # Reuse direction only. Approval and observations stay on the original task.
                    ping_scope={device:[check for check in checks if check in PROBE_CHECKS]
                                for device,checks in scope.items()}
                    ping_scope={device:checks for device,checks in ping_scope.items() if checks}
                    pairs={(item['device'],item['check']) for item in evidence}
                    if pairs!={(device,check) for device,checks in ping_scope.items() for check in checks}:
                        ping_scope={}
                    return {'id':row['id'],'evidence':evidence,'scope':ping_scope,'devices':sorted({device for device in scope if device in DEVICES}
                            | {PING_TARGETS[item['check']] for item in evidence if item['check'] in PING_TARGETS})}
        return None

    def topic_observations(self,owner,conversation,revision,topic):
        """Find recent interface evidence from this topic's most recent investigation."""
        intake=topic['intake']
        brief=intake['brief']
        with self.connect() as db:
            row=db.execute('SELECT id,status,scope,result,diagnostic_goal,expires FROM workflow_tasks '
                'WHERE owner=? AND conversation=? AND mode_revision=? AND question=? ORDER BY rowid DESC LIMIT 1',
                (owner,conversation,revision,topic['question'])).fetchone()
            if (row is None or row['status']!='completed' or not row['result'] or row['expires']<=self.clock()
                    or row['diagnostic_goal']!=brief['goal'] or intake.get('task_id',row['id'])!=row['id']):
                return {},[]
            scope=json.loads(row['scope'])
            if set(scope)!=set(brief['devices']):return {},[]
            return scope,json.loads(row['result']).get('evidence',[])

    def forget_topic(self,owner,conversation):
        with self.connect() as db:db.execute('DELETE FROM workflow_topics WHERE owner=? AND conversation=?',(owner,conversation))

    def remember_topic(self,owner,conversation,question,domain,intent,target_hint='',revision=None,
                       expected=ANY_TOPIC,intake=None):
        # 话题与结构化调查目标同期限保存，均不携带设备授权或观测。
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if revision is not None and self.interaction_row(db,owner,conversation)['revision'] != revision:
                raise TaskError('mode_changed')
            # 一次模型往返期间用户可能已提了新问题，旧申请不得覆盖新话题。
            if expected is not ANY_TOPIC and self.topic_row(db,owner,conversation)!=expected:
                return
            now=self.clock()
            expires=now+900
            db.execute('DELETE FROM workflow_topics WHERE expires<=?',(now,))
            db.execute('INSERT OR REPLACE INTO workflow_topics VALUES (?,?,?,?,?,?,?)',
                (owner,conversation,question,domain,intent,target_hint,expires))
            db.execute('DELETE FROM workflow_intake WHERE owner=? AND conversation=?',(owner,conversation))
            if intake is not None:
                db.execute('INSERT INTO workflow_intake VALUES (?,?,?,?)',
                    (owner,conversation,expires,json.dumps(intake,ensure_ascii=False)))
            db.execute('DELETE FROM workflow_topics WHERE owner=? AND conversation NOT IN '
                '(SELECT conversation FROM workflow_topics WHERE owner=? ORDER BY expires DESC,rowid DESC LIMIT 50)',(owner,owner))

    def add_session(self,owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM workflow_sessions WHERE expires>?',(self.clock(),)).fetchone()[0]>=1000:
                raise TaskError('session_limit')
            db.execute('INSERT INTO workflow_sessions VALUES (?,?)',(owner,self.clock()+86400))

    def has_session(self,owner):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM workflow_sessions WHERE owner=? AND expires>?',(owner,self.clock())).fetchone() is not None

    def end_identity(self,owner):
        """退出/切换账号时撤销该身份未结束调查；保留已发生的证据和审计。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            tasks=db.execute("SELECT id FROM workflow_tasks WHERE owner=? AND status IN ('pending','approved','running')",(owner,)).fetchall()
            for row in tasks:
                db.execute("UPDATE workflow_tasks SET status='cancelled' WHERE id=?",(row['id'],))
                self.end_progress(db,row['id'],'cancelled');self.event(db,row['id'],'cancelled',{'reason':'identity_ended'})
            db.execute('DELETE FROM workflow_topics WHERE owner=?',(owner,))
            db.execute('DELETE FROM workflow_investigation_turns WHERE owner=?',(owner,))
            db.execute('DELETE FROM workflow_sessions WHERE owner=?',(owner,))
            db.execute("UPDATE workflow_modes SET mode='qa',scene=NULL,revision=revision+1 WHERE owner=?",(owner,))
            return len(tasks)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, factory=ApprovalConnection)
        try:
            db.execute('PRAGMA busy_timeout=5000')
            db.row_factory = sqlite3.Row
            return db
        except BaseException:
            db.close()
            raise

    def event(self,db,task,kind,body):
        db.execute('INSERT INTO workflow_events VALUES (?,?,?,?)',
                   (task,kind,json.dumps(body,ensure_ascii=False),self.clock()))

    def row(self,db,task,owner=None):
        row=db.execute('SELECT * FROM workflow_tasks WHERE id=?',(task,)).fetchone()
        if row is None or (owner is not None and row['owner']!=owner): raise TaskError('not_found')
        if row['status'] in ('pending','approved','running') and self.clock()>=row['expires']:
            db.execute("UPDATE workflow_tasks SET status='expired' WHERE id=?",(task,))
            self.end_progress(db,task,'expired')
            self.event(db,task,'expired',{})
            row=db.execute('SELECT * FROM workflow_tasks WHERE id=?',(task,)).fetchone()
        return row

    def public(self,row,db=None):
        if db is None:
            with self.connect() as connection: return self.public(row,connection)
        result={'id':row['id'],'question':row['question'],'scope':json.loads(row['scope']),
                'conversation_id':row['conversation'], 'scene':row['scene'], 'interaction_revision':row['mode_revision'],
                'status':row['status'],'expires':row['expires'],'attempts':row['attempts'],
                'result':json.loads(row['result']) if row['result'] else None}
        if row['diagnostic_goal']:result['diagnostic_goal']=row['diagnostic_goal']
        if row['approver']==AUTO_READ_ACTOR:result['automatic_read']=True
        result['execution_limits']=self.execution_limits(db,row)
        result['investigation_stages']=[dict(item) for item in db.execute(
            'SELECT number,reads,started,updated,status FROM workflow_stages WHERE task=? ORDER BY number',
            (row['id'],))]
        permission=db.execute('SELECT flow,requested_use,approved_use FROM workflow_data_permissions WHERE task=?',(row['id'],)).fetchone()
        if permission:
            result.update(approval_flow=permission['flow'],data_use=LOCAL_READ_USE if row['approver']==AUTO_READ_ACTOR else permission['requested_use'],
                          data_use_approved=permission['approved_use']==DIAGNOSTIC_DATA_USE)
        if row['server_owned']:
            result.update(server_owned=True, task_inputs=self.task_inputs(row['id'],db))
        progress=db.execute('SELECT * FROM workflow_progress WHERE task=?',(row['id'],)).fetchone()
        if progress:
            body=json.loads(progress['body'])
            result['execution_steps']=body.pop('execution_steps',[])
            if isinstance(result['result'],dict):
                final_steps=result['result'].get('execution_steps')
                if body.get('stage')=='interrupted':
                    result['execution_steps']=final_steps or [dict(step,freshness='historical')
                        for step in result['execution_steps'] if isinstance(step,dict)]
                if not final_steps:result['result']['execution_steps']=result['execution_steps']
            if body.get('stage')=='reading' and body.get('device') and body.get('check'):
                from legacy.execution_workflow import check_request
                result['active_check']=check_request(body['device'],body['check'])
            finish= self.clock() if row['status']=='running' else progress['updated']
            result['progress']=dict(body,started_at=progress['started'],updated_at=progress['updated'],
                                    elapsed_seconds=round(max(0,finish-progress['started']),1),
                                    reads=row['attempts'])
        from legacy.execution_workflow import readable_steps
        if 'execution_steps' in result:result['execution_steps']=readable_steps(result['execution_steps'])
        if isinstance(result['result'],dict) and 'execution_steps' in result['result']:
            result['result']['execution_steps']=readable_steps(result['result']['execution_steps'])
        return result

    def end_progress(self,db,task,stage,end_stages=True):
        if end_stages:
            db.execute("UPDATE workflow_stages SET status=?,updated=? WHERE task=? AND status='running'",
                       (stage,self.clock(),task))
        record=db.execute('SELECT body FROM workflow_progress WHERE task=?',(task,)).fetchone()
        if record:
            body=json.loads(record['body']);body['stage']=stage
            body.pop('question',None)
            db.execute('UPDATE workflow_progress SET updated=?,body=? WHERE task=?',
                       (self.clock(),json.dumps(body),task))

    def update_progress(self,task,owner,stage,step=0,device=None,check=None,observation=None,question=None):
        if stage not in ('retrieving','thinking','analyzing','reading','read_complete','awaiting_input'):
            raise ValueError('Invalid progress stage')
        if stage=='awaiting_input' and (not isinstance(question,str) or not 2<=len(question)<=120):
            raise ValueError('Invalid clarification')
        if type(step) is not int or not 0<=step<=16: raise ValueError('Invalid progress step')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,task,owner)
            if row['status']!='running': return False
            limits=self.execution_limits(db,row,approved=True)
            if step>limits['max_steps']:raise ValueError('Invalid progress step')
            record=db.execute('SELECT body FROM workflow_progress WHERE task=?',(task,)).fetchone()
            old=json.loads(record['body']) if record else {}
            steps=old.get('execution_steps',[])
            body={'stage':stage,'step':step,'execution_steps':steps}
            if stage=='awaiting_input':body['question']=question
            if device is not None or check is not None:
                if check not in json.loads(row['approved_scope']).get(device,[]):
                    raise TaskError('scope_changed')
                body.update(device=device,check=check)
            if observation is not None:
                if (stage!='read_complete' or not isinstance(observation,dict)
                        or observation.get('device')!=device or observation.get('check')!=check):
                    raise ValueError('Invalid completed observation')
                reserved=db.execute('SELECT 1 FROM workflow_reads WHERE task=? AND device=? AND check_name=?',
                                    (task,device,check)).fetchone()
                if not reserved:raise TaskError('not_approved')
                from legacy.execution_workflow import observation_step
                entry=observation_step(observation)
                if not any((s['device'],s['check'])==(device,check) for s in steps):
                    if len(steps)>=limits['max_reads']:raise ValueError('Too many execution steps')
                    steps.append(entry)
            db.execute('UPDATE workflow_progress SET updated=?,body=? WHERE task=?',
                       (self.clock(),json.dumps(body),task))
        return True

    def create(self,owner,question,history,scope,conversation='default',revision=None,approval_flow=None,goal=None,
               brief=None,expected_topic=ANY_TOPIC,replace_active=False,required_checks=None,runbook=None):
        if approval_flow not in (None,SINGLE_APPROVAL):raise ValueError('Invalid approval flow')
        if goal is not None and (not isinstance(goal,str) or goal not in DIAGNOSTIC_GOALS):
            raise ValueError('Invalid diagnostic goal')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected_topic is not ANY_TOPIC and self.topic_row(db,owner,conversation)!=expected_topic:
                raise TaskError('topic_changed')
            if replace_active and approval_flow==SINGLE_APPROVAL:
                rows=db.execute("SELECT t.id FROM workflow_tasks t JOIN workflow_data_permissions p ON p.task=t.id "
                    "WHERE t.owner=? AND t.conversation=? AND t.mode_revision=? AND p.flow=? "
                    "AND t.status IN ('pending','approved','running') AND t.expires>?",
                    (owner,conversation,revision,SINGLE_APPROVAL,self.clock())).fetchall()
                for row in rows:
                    db.execute("UPDATE workflow_tasks SET status='cancelled' WHERE id=?",(row['id'],))
                    self.end_progress(db,row['id'],'cancelled')
                    self.event(db,row['id'],'cancelled',{'reason':'task_replaced'})
            task=self.create_pending(db,owner,question,history,scope,conversation,revision,approval_flow,goal,brief,
                                     required_checks=required_checks,runbook=runbook)
        return self.get(task,owner)

    def create_pending(self,db,owner,question,history,scope,conversation,revision,approval_flow,goal,brief=None,
                       required_checks=None,runbook=None):
        task=uuid.uuid4().hex
        scope=validate_scope(scope)
        interaction = self.interaction_row(db, owner, conversation)
        if interaction['mode'] not in ('diagnostic','assistant'): raise TaskError('diagnostic_required')
        scene='ha_topology' if interaction['mode']=='assistant' else interaction['scene']
        if revision != interaction['revision']: raise TaskError('mode_changed')
        if not allows_scope(scene, scope): raise TaskError('scene_scope_mismatch')
        # 不把审批队列做成无限增长的入口。
        pending=db.execute("SELECT count(*) FROM workflow_tasks WHERE owner=? AND status IN ('pending','approved','running') AND expires>?",
                           (owner,self.clock())).fetchone()[0]
        if pending>=5: raise TaskError('too_many_pending')
        db.execute('INSERT INTO workflow_tasks (id,owner,question,history,scope,status,expires,conversation,scene,mode_revision,diagnostic_goal) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (task,owner,question,json.dumps(history,ensure_ascii=False),json.dumps(scope,sort_keys=True),
             'pending',self.clock()+900,conversation,scene,interaction['revision'],goal))
        if approval_flow==SINGLE_APPROVAL:
            db.execute('INSERT INTO workflow_data_permissions VALUES (?,?,?,NULL)',
                       (task,SINGLE_APPROVAL,DIAGNOSTIC_DATA_USE))
        requested={'scope':scope,'approval_flow':approval_flow}
        if runbook is not None:
            from legacy.ha_runbooks import RUNBOOKS
            if (not isinstance(runbook,str) or runbook not in RUNBOOKS or goal!=RUNBOOKS[runbook][1]
                    or any(set(checks)!=set(RUNBOOKS[runbook][2]) for checks in scope.values())):
                raise TaskError('invalid_scope')
            requested['runbook']=runbook
        if brief is not None:requested['brief']=brief
        if required_checks:
            required_checks=validate_scope(required_checks)
            if any(not set(checks)<=set(scope.get(device,())) for device,checks in required_checks.items()):
                raise ValueError('Required checks must be inside the requested scope')
            requested['required_checks']=required_checks
        self.event(db,task,'requested',requested)
        limits=requested_limits(scope,staged=approval_flow==SINGLE_APPROVAL)
        if limits!=legacy_limits():
            db.execute('INSERT INTO workflow_limits VALUES (?,?,NULL)',
                       (task,json.dumps(limits,sort_keys=True)))
            self.event(db,task,'limits_requested',limits)
        return task


    def get(self,task,owner=None):
        with self.connect() as db: return self.public(self.row(db,task,owner),db)

    def list(self,owner=None):
        with self.connect() as db:
            rows=db.execute('SELECT id FROM workflow_tasks'+(' WHERE owner=?' if owner else '')+
                            ' ORDER BY rowid DESC LIMIT 50',(owner,) if owner else ()).fetchall()
            return [self.public(self.row(db,r['id'],owner),db) for r in rows]

    def decide(self,task,approved,actor,data_use=None,owner=None,server_owned=False):
        # 本人确认由HTTP处理器传入已验证身份；旧本地审批只能处理历史流程。
        automatic=actor==AUTO_READ_ACTOR
        requester=actor in ('task-owner',AUTO_READ_ACTOR) and isinstance(owner,str) and bool(owner)
        if (not requester and (actor not in ('local-admin','authenticated-admin') or owner is not None)
                or type(approved) is not bool):
            raise TaskError('admin_required')
        if server_owned and not (requester and approved):raise TaskError('data_scope_required')
        if automatic and not server_owned:raise TaskError('data_scope_required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.row(db,task,owner)
            if row['status']!='pending': raise TaskError('not_pending')
            if approved and not self.task_interaction_active(db, row): raise TaskError('diagnostic_required')
            permission=db.execute('SELECT * FROM workflow_data_permissions WHERE task=?',(task,)).fetchone()
            if permission and permission['flow']==SINGLE_APPROVAL and not requester:
                raise TaskError('requester_required')
            if requester and (not permission or permission['flow']!=SINGLE_APPROVAL):
                raise TaskError('data_scope_required')
            scope=validate_scope(json.loads(row['scope']))
            if automatic and (row['scene']!='ha_topology' or not passive_scope(scope)):
                raise TaskError('invalid_scope')
            limits=self.execution_limits(db,row)
            db.execute('UPDATE workflow_limits SET approved=? WHERE task=?',
                       (json.dumps(limits,sort_keys=True) if approved else None,task))
            if permission:
                expected_use=LOCAL_READ_USE if automatic else permission['requested_use']
                if approved and data_use!=expected_use:raise TaskError('data_scope_required')
                if not approved and data_use is not None:raise TaskError('data_scope_required')
                db.execute('UPDATE workflow_data_permissions SET approved_use=? WHERE task=?',
                           (data_use if approved else None,task))
            elif data_use is not None:raise TaskError('data_scope_required')
            db.execute('UPDATE workflow_tasks SET status=?,approved_scope=?,approver=?,expires=?,server_owned=? WHERE id=?',
                ('approved' if approved else 'rejected',json.dumps(scope,sort_keys=True) if approved else None,
                 actor,self.clock()+300,int(server_owned),task))
            self.event(db,task,'approved' if approved else 'rejected',{'actor':actor,'scope':scope,'data_use':data_use,
                                                                    'execution_limits':limits})
        return self.get(task,owner)

    def require_automatic_start(self,task,owner,server_owned=False):
        with self.connect() as db:
            row=self.row(db,task,owner)
            permission=db.execute('SELECT * FROM workflow_data_permissions WHERE task=?',(task,)).fetchone()
            if (row['status']!='approved' or bool(row['server_owned'])!=server_owned
                    or not permission or permission['flow']!=SINGLE_APPROVAL
                    or permission['approved_use']!=(LOCAL_READ_USE if row['approver']==AUTO_READ_ACTOR else DIAGNOSTIC_DATA_USE)):
                raise TaskError('data_scope_required')
            return True

    def cancel(self,task,owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,task,owner)
            if row['status'] not in ('pending','approved','running'): raise TaskError('not_cancellable')
            db.execute("UPDATE workflow_tasks SET status='cancelled' WHERE id=?",(task,))
            self.end_progress(db,task,'cancelled')
            self.event(db,task,'cancelled',{})
        return self.get(task,owner)

    def claim(self,task,owner,server_owned=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,task,owner)
            if row['status']!='approved' or bool(row['server_owned'])!=server_owned: raise TaskError('not_approved')
            if not self.task_interaction_active(db, row): raise TaskError('diagnostic_required')
            permission=db.execute('SELECT approved_use FROM workflow_data_permissions WHERE task=?',(task,)).fetchone()
            if permission and permission['approved_use']!=(LOCAL_READ_USE if row['approver']==AUTO_READ_ACTOR else DIAGNOSTIC_DATA_USE):
                raise TaskError('data_scope_required')
            scope=validate_scope(json.loads(row['approved_scope']))
            if scope!=json.loads(row['scope']): raise TaskError('scope_changed')
            if row['approver']==AUTO_READ_ACTOR and not passive_scope(scope):raise TaskError('scope_changed')
            limits=self.execution_limits(db,row,approved=True)
            db.execute("UPDATE workflow_tasks SET status='running' WHERE id=?",(task,))
            now=self.clock()
            db.execute('INSERT INTO workflow_progress VALUES (?,?,?,?)',
                       (task,now,now,json.dumps({'stage':'thinking','step':0})))
            db.execute('INSERT INTO workflow_stages VALUES (?,?,?,?,?,?)',(task,1,0,now,now,'running'))
            self.event(db,task,'started',{})
            request=self.task_request(db,task)
            return {'id':task,'question':row['question'],'history':json.loads(row['history']),'scope':scope,
                    'scene':row['scene'],'execution_limits':limits,'diagnostic_goal':row['diagnostic_goal'],
                    'covered_scope':self.covered_scope(db,row),'brief':request.get('brief'),
                    'required_checks':request.get('required_checks',{}),'automatic_read':row['approver']==AUTO_READ_ACTOR,
                    'runbook':request.get('runbook')}

    def task_request(self,db,task):
        row=db.execute("SELECT body FROM workflow_events WHERE task=? AND kind='requested' LIMIT 1",(task,)).fetchone()
        return json.loads(row['body']) if row else {}

    def covered_scope(self,db,row):
        linked=db.execute("SELECT body FROM workflow_events WHERE task=? AND kind='scope_followup'",
                          (row['id'],)).fetchone()
        prior=json.loads(linked['body'])['covered_scope'] if linked else {}
        return {device: sorted(set(prior.get(device,())) | set(checks))
                for device,checks in {**prior,**json.loads(row['scope'])}.items()}

    def execution_limits(self,db,row,approved=False):
        """旧任务无阶段记录时保留四读；新任务必须与已批准的冻结策略一致。"""
        record=db.execute('SELECT requested,approved FROM workflow_limits WHERE task=?',(row['id'],)).fetchone()
        if record is None:return legacy_limits()
        try:
            limits=validate_limits(json.loads(record['requested']),json.loads(row['scope']))
            if approved:
                frozen=validate_limits(json.loads(record['approved']),json.loads(row['scope']))
                if frozen!=limits:raise ValueError()
        except (ValueError,TypeError):raise TaskError('execution_limits_changed') from None
        return limits

    def authorize_read(self,task,owner,device,check):
        # 每次实际读取前重查状态，取消/过期会阻止后续检查。
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,task,owner)
            if row['status']!='running': raise ToolRejection('approval_not_active')
            if not self.task_interaction_active(db, row): raise ToolRejection('diagnostic_required')
            scope=json.loads(row['approved_scope'])
            if check not in scope.get(device,[]): raise ToolRejection('check_not_approved')
            if row['approver']==AUTO_READ_ACTOR and check not in PASSIVE_CHECKS:
                raise ToolRejection('check_not_approved')
            limits=self.execution_limits(db,row,approved=True)
            if row['attempts']>=limits['max_reads']: raise ToolRejection('read_budget_reached')
            try: db.execute('INSERT INTO workflow_reads VALUES (?,?,?)',(task,device,check))
            except sqlite3.IntegrityError: raise ToolRejection('duplicate_read')
            number=stage_budget(limits,row['attempts'])['number']
            now=self.clock()
            if db.execute('SELECT 1 FROM workflow_stages WHERE task=? AND number=?',(task,number)).fetchone() is None:
                db.execute("UPDATE workflow_stages SET status='completed',updated=? WHERE task=? AND status='running'",
                           (now,task))
                db.execute('INSERT INTO workflow_stages VALUES (?,?,?,?,?,?)',(task,number,0,now,now,'running'))
                self.event(db,task,'stage_started',{'number':number,'trigger':'host_read_partition'})
            db.execute('UPDATE workflow_stages SET reads=reads+1,updated=? WHERE task=? AND number=?',(now,task,number))
            db.execute('UPDATE workflow_tasks SET attempts=attempts+1 WHERE id=?',(task,))
            self.event(db,task,'read_reserved',{'device':device,'check':check,'investigation_stage':number})

    def assert_active(self,task,owner):
        """模型/检索每轮也检查任务生命周期，不占用设备读取次数。"""
        with self.connect() as db:
            row=self.row(db,task,owner)
            if row['status']!='running' or not self.task_interaction_active(db,row):
                raise ToolRejection('investigation_inactive')
            try:self.execution_limits(db,row,approved=True)
            except TaskError:raise ToolRejection('investigation_inactive') from None

    def task_inputs(self,task,db=None):
        if db is None:
            with self.connect() as connection:return self.task_inputs(task,connection)
        return [row['text'] for row in db.execute('SELECT text FROM workflow_inputs WHERE task=? ORDER BY rowid',(task,))]

    def add_task_input(self,task,owner,message_id,text):
        validate_conversation_id(message_id)
        if not isinstance(text,str) or not 1<=len(text.strip())<=500:raise ValueError('Invalid task input')
        text=text.strip()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.row(db,task,owner)
            if (not row['server_owned'] or row['approver']==AUTO_READ_ACTOR or row['status'] not in ('approved','running')
                    or not self.task_interaction_active(db,row)):
                raise TaskError('task_input_closed')
            progress=db.execute('SELECT body FROM workflow_progress WHERE task=?',(task,)).fetchone()
            if progress and json.loads(progress['body']).get('stage')=='finalizing':
                raise TaskError('task_input_closed')
            existing=db.execute('SELECT text FROM workflow_inputs WHERE task=? AND id=?',(task,message_id)).fetchone()
            if existing:
                if existing['text']!=text:raise TaskError('task_input_changed')
                return
            count=len(self.task_inputs(task,db))
            if count>=MAX_TASK_INPUTS:raise TaskError('task_input_limit')
            db.execute('INSERT INTO workflow_inputs VALUES (?,?,?)',(task,message_id,text))
            self.event(db,task,'input_received',{'number':count+1})

    def seal_task_inputs(self,task,owner,count):
        # 结论发布和接收补充使用同一写锁，已接收的新消息必须进入下一步。
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.row(db,task,owner)
            if not row['server_owned'] or row['status']!='running' or not self.task_interaction_active(db,row):
                raise ToolRejection('investigation_inactive')
            if len(self.task_inputs(task,db))!=count:return False
            self.end_progress(db,task,'finalizing',end_stages=False)
        return True

    def finish(self,task,owner,result):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.row(db,task,owner)
            from legacy.execution_workflow import observation_steps
            result=dict(result,execution_steps=observation_steps(result.get('evidence',[])))
            record=db.execute('SELECT body FROM workflow_progress WHERE task=?',(task,)).fetchone()
            if not result['execution_steps'] and record:
                result['execution_steps']=json.loads(record['body']).get('execution_steps',[])
            status=row['status']
            extension=result.pop('scope_extension',None)
            if extension and row['server_owned'] and status=='running' and self.task_interaction_active(db,row):
                inputs=self.task_inputs(task,db)
                covered=self.covered_scope(db,row)
                scope={device:[check for check in checks if check not in covered.get(device,())]
                       for device,checks in validate_scope(extension['scope']).items()}
                scope={device:checks for device,checks in scope.items() if checks}
                refs=extension['refs']
                from legacy.unified_chat import observation_refs
                current=observation_refs(result.get('evidence',[]),True)
                if scope and (inputs or refs) and set(refs)<=current:
                    try:
                        goal=extension['goal'] or row['diagnostic_goal']
                        brief=self.task_request(db,task).get('brief')
                        if brief is not None:brief=dict(brief,devices=sorted(scope),goal=goal)
                        question=join_diagnostic_question(row['question'],inputs[-1]) if inputs else row['question']
                        followup=self.create_pending(db,owner,question,[],scope,row['conversation'],
                            row['mode_revision'],SINGLE_APPROVAL,goal,brief)
                    except TaskError:
                        result['answer']+='\n\n暂时无法创建新增检查申请，请稍后在本对话中重新提交。'
                    else:
                        self.event(db,followup,'scope_followup',
                                   {'parent':task,'covered_scope':covered,'parent_refs':refs})
                        result['followup_task']=self.public(self.row(db,followup,owner),db)
            if status=='running':
                assessment = result.get('icmp_assessment') or result.get('goal_assessment') or {}
                measurements = assessment.get('measurements', [])
                all_unavailable = measurements and all(item['outcome'] == 'unavailable' for item in measurements)
                status = 'completed' if result['status']=='answered' and not all_unavailable else 'incomplete'
            db.execute('UPDATE workflow_tasks SET status=?,result=? WHERE id=?',
                       (status,json.dumps(result,ensure_ascii=False),task))
            self.end_progress(db,task,status)
            self.event(db,task,'finished',{'status':status})
        return self.get(task,owner)

    def recover_interrupted(self):
        """启动时从已提交的读取记录恢复历史证据，并结束原任务。"""
        from legacy.execution_workflow import observation_step

        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT id FROM workflow_tasks WHERE status='running' "
                            "OR (status='approved' AND server_owned=1)").fetchall()
            # WorkflowService 的审计与审批共用数据库。早期或独立的审批库可能没有审计表。
            has_audit=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
            for row in rows:
                task=row['id']
                reservations=db.execute('SELECT device,check_name FROM workflow_reads WHERE task=? ORDER BY rowid',
                                        (task,)).fetchall()
                missing={(read['device'],read['check_name']):'OBS-'+str(index)
                         for index,read in enumerate(reservations,1)}
                evidence,steps=[],[]
                if has_audit and missing:
                    for event in db.execute("SELECT body FROM events WHERE task=? AND kind='observation' ORDER BY rowid",
                                            (task,)):
                        try:
                            observation=json.loads(event['body'])
                            key=(observation['device'],observation['check'])
                            if missing.get(key)!=observation['id']:continue
                            observation['freshness']='historical'
                            step=observation_step(observation)
                        except (ValueError,KeyError,TypeError):
                            continue
                        evidence.append(observation)
                        steps.append(step)
                        del missing[key]

                answer='调查因服务中断而停止。'
                result={'status':'incomplete','reason':'investigation_interrupted','evidence':evidence,'execution_steps':steps}
                if evidence:
                    answer+='已保存的检查结果可在执行过程中回看。'
                    result['evidence_scope']={'kind':'historical','current_reads':0}
                if missing:
                    labels=[DEVICE_LABELS.get(device,device)+'的'+CHECK_LABELS.get(check,check) for device,check in missing]
                    answer+='、'.join(labels)+'结果未知，审计中没有对应的返回记录。'
                result['answer']=answer+'继续检查需发起新调查。'
                db.execute("UPDATE workflow_tasks SET status='incomplete',result=? WHERE id=?",
                           (json.dumps(result,ensure_ascii=False),task))
                self.event(db,task,'interrupted',{})
                self.end_progress(db,task,'interrupted')
