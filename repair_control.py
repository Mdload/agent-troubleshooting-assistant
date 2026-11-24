"""一次维护任务内的固定热备恢复；默认关闭，不接收命令文本。"""
import datetime
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from approval_store import TaskError

TARGET='remote-backup'
PEER='remote-primary'
ACTION='start_keepalived'
UNIT='keepalived.service'
VIP='198.51.100.29'
READ_SCOPE={TARGET:['services','interfaces'],PEER:['services','interfaces']}

def service_state(output,unit):
    for block in output.strip().split('\n\n'):
        fields=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if fields.get('Id')==unit:return fields.get('ActiveState')
    return None

def pair_state(evidence,clock=time.time,max_age=90):
    selected={}
    for item in evidence:
        key=(item.get('device'),item.get('check'))
        if key[0] not in READ_SCOPE or key[1] not in ('services','interfaces'):continue
        if item.get('ok') is not True or not isinstance(item.get('output'),str):continue
        try:
            stamp=datetime.datetime.strptime(item['collected_at'][:19],'%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc).timestamp()
        except (ValueError,TypeError,KeyError):continue
        if not -3<=clock()-stamp<=max_age:continue
        selected[key]=item
    if len(selected)!=4:raise TaskError('repair_fresh_pair_required')
    target=selected[(TARGET,'services')]['output'];peer=selected[(PEER,'services')]['output']
    return {'target_active':service_state(target,UNIT), 'peer_active':service_state(peer,UNIT),
            'target_croc':service_state(target,'croc2.0smr.service'),
            'target_routes':service_state(target,'remote-ha-routes.service'),
            'target_vip':VIP+'/' in selected[(TARGET,'interfaces')]['output'],
            'peer_vip':VIP+'/' in selected[(PEER,'interfaces')]['output'],
            'refs':[item.get('id') for item in selected.values() if item.get('id')]}

def may_start(state):
    return (state['target_active'] in ('inactive','failed') and state['peer_active']=='active'
            and state['target_croc']=='inactive' and state['target_routes']=='inactive'
            and not state['target_vip'] and state['peer_vip'])

def restored(state):
    return (state['target_active']=='active' and state['peer_active']=='active'
            and state['target_croc']=='inactive' and state['target_routes']=='inactive'
            and not state['target_vip'] and state['peer_vip'])

def service_assessment(evidence):
    """从既有四项观测分清业务待机与热备服务停止，不增加设备读取。"""
    try:state=pair_state(evidence)
    except TaskError:return None
    if restored(state):
        return {'finding':'normal_standby','summary':'备机 Keepalived 正在运行；Croc 和业务路由停止符合当前待机状态。',
                'repair_needed':False,'refs':state['refs'],'full_ha_verified':False,'business_verified':False}
    if may_start(state):
        return {'finding':'standby_keepalived_stopped','summary':'备机 Keepalived 未运行，与 Croc 和业务路由的正常待机停止不同；是否恢复仍需结合维护意图及本次权限。',
                'repair_needed':None,'repair_candidate':True,'refs':state['refs'],'full_ha_verified':False,'business_verified':False}
    return {'finding':'conditions_not_met','summary':'当前主备服务或 VIP 分布不满足固定恢复条件，不能直接启动备机服务。',
            'repair_needed':None,'refs':state['refs'],'full_ha_verified':False,'business_verified':False}

def check_recommendation(value,catalog,evidence):
    if (not catalog or not isinstance(value,dict) or set(value)!={'device','action','refs'}
            or value.get('device')!=TARGET or value.get('action')!=ACTION
            or not isinstance(value.get('refs'),list) or not value['refs']
            or any(not isinstance(ref,str) for ref in value['refs'])):
        raise TaskError('repair_action_not_allowed')
    state=pair_state(evidence)
    if not may_start(state) or not set(state['refs'])<=set(value['refs']):raise TaskError('repair_evidence_required')
    return dict(value)

class RepairManager:
    def __init__(self,path,policy_path,probe,executor=None,clock=time.time,sleep=time.sleep,inspector=None,task_loader=None):
        self.path=Path(path);self.policy_path=Path(policy_path);self.probe=probe
        self.executor=executor;self.clock=clock;self.sleep=sleep
        self.inspector=inspector;self.task_loader=task_loader
        from repair_permissions import RepairPermissions
        self.permissions=RepairPermissions(self)

    def available(self):
        if self.executor is not None and self.permissions.available():return True
        try:policy=json.loads(self.policy_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return False
        return self.executor is not None and self.policy(policy.get('owner'),policy.get('task')) is not None

    def policy(self,owner,task):
        present,policy=self.permissions.policy(owner,task)
        if present:return policy
        try:policy=json.loads(self.policy_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return None
        if (policy.get('enabled') is not True or policy.get('owner')!=owner or policy.get('task')!=task
                or policy.get('target')!=TARGET or type(policy.get('expires')) not in (int,float)
                or not self.clock()<policy['expires'] or policy['expires']-self.clock()>1800
                or not re.fullmatch('[a-f0-9]{32}',str(policy.get('lease','')))):return None
        return policy

    def catalog(self,owner,task):
        if not self.policy(owner,task['id']):return None
        if any(not set(checks)<=set(task['scope'].get(device,[])) for device,checks in READ_SCOPE.items()):return None
        return {TARGET:{'action':ACTION,'unit':UNIT,'approval_required':True,
                'requirements':'先读取远端主备的服务与接口/VIP共4项。仅备机Keepalived未运行、Croc和业务路由停止、VIP在主机时可建议启动；正常待机不修复。'}}

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(str(self.path),timeout=3)
        try:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS repair_plans (id TEXT PRIMARY KEY, owner TEXT, task TEXT UNIQUE, lease TEXT, expires REAL, state TEXT, body TEXT, result TEXT)')
            with db:yield db
        finally:db.close()

    def get(self,ident,owner):
        with self.db() as db:row=db.execute('SELECT * FROM repair_plans WHERE id=? AND owner=?',(ident,owner)).fetchone()
        if not row:raise TaskError('not_found')
        return {'id':row['id'],'task':row['task'],'expires':row['expires'],'state':row['state'],
                'plan':json.loads(row['body']),'result':json.loads(row['result']) if row['result'] else None}

    def propose(self,owner,task,result):
        recommendation=result.get('repair_recommendation');catalog=self.catalog(owner,task)
        if not recommendation or not catalog:return None
        check_recommendation(recommendation,catalog,result.get('evidence',[]))
        policy=self.policy(owner,task['id']);ident=secrets.token_hex(16)
        body={'device':TARGET,'peer':PEER,'action':ACTION,'unit':UNIT,'refs':recommendation['refs'],
              'reason':'备机热备服务未运行；主机仍持业务VIP。仅启动备机Keepalived，随后复查主备服务与VIP。',
              'read_scope':READ_SCOPE,'max_writes':1,'business_verification':'未提供端到端业务探针，不能据此宣称业务恢复。'}
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO repair_plans VALUES (?,?,?,?,?,?,?,?)',
                (ident,owner,task['id'],policy['lease'],min(policy['expires'],self.clock()+300),'pending',json.dumps(body,ensure_ascii=False),None))
            row=db.execute('SELECT id FROM repair_plans WHERE task=?',(task['id'],)).fetchone()
        return self.get(row['id'],owner)

    def decide(self,ident,owner,approved):
        if type(approved) is not bool:raise ValueError('Invalid approval')
        item=self.get(ident,owner)
        if not self.policy(owner,item['task']):raise TaskError('repair_window_inactive')
        with self.db() as db:
            changed=db.execute('UPDATE repair_plans SET state=? WHERE id=? AND owner=? AND state=? AND expires>?',
                ('approved' if approved else 'cancelled',ident,owner,'pending',self.clock())).rowcount
        if not changed:raise TaskError('repair_not_pending')
        return self.get(ident,owner)

    def cancel(self,ident,owner):
        self.get(ident,owner)
        with self.db() as db:
            db.execute("UPDATE repair_plans SET state='cancelled' WHERE id=? AND owner=? AND state IN ('pending','approved')",(ident,owner))
            db.execute("UPDATE repair_plans SET state='cancel_requested' WHERE id=? AND owner=? AND state='running'",(ident,owner))
        return self.get(ident,owner)

    def recover_interrupted(self):
        if not self.path.exists():return
        result={'status':'unknown','write_dispatched':True,'service_verified':False,'business_verified':False,'reason':'repair_process_interrupted'}
        with self.db() as db:
            db.execute("UPDATE repair_plans SET state='unknown',result=? WHERE state IN ('running','cancel_requested','checking')",(json.dumps(result),))

    def reconcile(self,ident,owner,identity_guard=lambda:None):
        item=self.get(ident,owner);identity_guard()
        if item['state'] not in ('unknown','completed','reviewed','refused'):raise TaskError('repair_not_reviewable')
        if self.inspector is None:raise TaskError('repair_inspector_unavailable')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT id FROM repair_plans WHERE state IN ('running','cancel_requested','checking')").fetchone():raise TaskError('busy')
            row=db.execute('SELECT lease FROM repair_plans WHERE id=? AND owner=?',(ident,owner)).fetchone()
            changed=db.execute('UPDATE repair_plans SET state=? WHERE id=? AND state=?',('checking',ident,item['state'])).rowcount
            if not changed:raise TaskError('busy')
        try:
            identity_guard();report=self.inspector({'operation':'inspect_keepalived','lease':row['lease'],'id':ident})
            if report.get('ok') is not True or report.get('id')!=ident or report.get('operation')!='inspect_keepalived':raise TaskError('repair_inspection_unconfirmed')
            receipt=report.get('record');confirmed=bool(report.get('record_current_boot') is True and isinstance(receipt,dict) and receipt.get('id')==ident and receipt.get('status')=='dispatched' and receipt.get('ok') is True)
            samples=[];evidence=[]
            for n in range(3):
                if n:self.sleep(3)
                evidence=[]
                for device,checks in READ_SCOPE.items():
                    for check in checks:identity_guard();evidence.append(self.probe(device,check))
                samples.append(restored(pair_state(evidence,clock=self.clock)))
                if not samples[-1]:break
            healthy=len(samples)==3 and all(samples)
            result={'status':('service_restored_confirmed' if confirmed else 'state_verified_execution_unconfirmed') if healthy else 'verification_incomplete',
                    'service_verified':healthy,'execution_confirmed':confirmed,'write_dispatched':(item.get('result') or {}).get('write_dispatched',False),
                    'reconciled':True,'business_verified':False,'ha_fully_verified':False,'record_status':receipt.get('status') if isinstance(receipt,dict) else 'not_recorded',
                    'evidence':evidence,'verification_samples':len(samples),'previous_status':(item.get('result') or {}).get('status')}
            state='reviewed'
        except Exception as error:
            result={'status':'unknown','service_verified':False,'business_verified':False,'reconciled':True,
                    'reason':str(error) if isinstance(error,TaskError) else 'repair_inspection_interrupted'};state='unknown'
        with self.db() as db:
            db.execute("UPDATE repair_plans SET state=?,result=? WHERE id=?",(state,json.dumps(result,ensure_ascii=False),ident))
        return self.get(ident,owner)

    def execute(self,ident,owner,identity_guard=lambda:None):
        item=self.get(ident,owner);policy=self.policy(owner,item['task'])
        if not policy:raise TaskError('repair_window_inactive')
        with self.db() as db:bound_lease=db.execute('SELECT lease FROM repair_plans WHERE id=?',(ident,)).fetchone()[0]
        if bound_lease!=policy['lease']:raise TaskError('repair_window_changed')
        if self.executor is None:raise TaskError('repair_executor_unavailable')
        identity_guard()
        # BEGIN IMMEDIATE serializes claims. The device additionally consumes the lease once.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT id FROM repair_plans WHERE state IN ('running','cancel_requested','checking')").fetchone():raise TaskError('busy')
            changed=db.execute("UPDATE repair_plans SET state='running' WHERE id=? AND owner=? AND state='approved' AND expires>?",(ident,owner,self.clock())).rowcount
            if not changed:raise TaskError('repair_not_approved')
        dispatched=False
        try:
            def guard():
                identity_guard()
                if not self.policy(owner,item['task']):raise TaskError('repair_window_inactive')
                if self.get(ident,owner)['state']!='running':raise TaskError('repair_cancelled')
            def observe():
                evidence=[]
                for device,checks in READ_SCOPE.items():
                    for check in checks:guard();evidence.append(self.probe(device,check))
                return pair_state(evidence,clock=self.clock),evidence
            before,evidence=observe()
            if restored(before):result={'status':'no_action_needed','write_dispatched':False,'service_verified':True,'evidence':evidence}
            else:
                if not may_start(before):raise TaskError('repair_precondition_changed')
                guard();dispatched=True
                operation=self.executor({'operation':ACTION,'lease':policy['lease'],'id':ident})
                if operation.get('ok') is not True or operation.get('id')!=ident:
                    if operation.get('id')==ident and operation.get('write_dispatched') is False:
                        dispatched=False;raise TaskError('repair_device_refused')
                    raise TaskError('repair_execution_unknown')
                samples=[]
                for n in range(3):
                    if n:self.sleep(3)
                    after,evidence=observe();samples.append(restored(after))
                    if not samples[-1]:break
                result={'status':'service_restored' if len(samples)==3 and all(samples) else 'verification_incomplete',
                        'write_dispatched':True,'service_verified':len(samples)==3 and all(samples),
                        'ha_fully_verified':False,'business_verified':False,'operation':operation,'evidence':evidence}
            state='completed'
        except Exception as error:
            state='unknown' if dispatched else 'refused'
            result={'status':state,'write_dispatched':dispatched,'service_verified':False,'business_verified':False,
                    'reason':str(error) if isinstance(error,TaskError) else 'repair_interrupted'}
        with self.db() as db:db.execute('UPDATE repair_plans SET state=?,result=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),ident))
        return self.get(ident,owner)
