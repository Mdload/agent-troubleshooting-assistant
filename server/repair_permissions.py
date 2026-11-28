"""网页维护申请：管理员批准固定动作，设备端独立许可仍是执行上限。"""
import json,re,secrets
from server.approval_store import TaskError
from server.repair_control import TARGET,ACTION,READ_SCOPE

class RepairPermissions:
    def __init__(self,manager):self.manager=manager
    def schema(self,db):
        db.execute('CREATE TABLE IF NOT EXISTS repair_windows ('
            'id TEXT PRIMARY KEY,owner TEXT NOT NULL,task TEXT UNIQUE NOT NULL,state TEXT NOT NULL,'
            'expires REAL NOT NULL,lease TEXT UNIQUE,body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS repair_events ('
            'id INTEGER PRIMARY KEY,created REAL NOT NULL,object_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL)')
    def event(self,db,ident,actor,action):
        db.execute('INSERT INTO repair_events(created,object_id,actor,action) VALUES (?,?,?,?)',
                   (self.manager.clock(),ident,actor,action))
    def public(self,row):
        value={'id':row['id'],'task':row['task'],'state':row['state'],'expires':row['expires'],**json.loads(row['body'])}
        if value['state'] in ('pending','active') and value['expires']<=self.manager.clock():value['state']='expired'
        return value
    def get(self,ident,owner=None):
        with self.manager.db() as db:
            self.schema(db)
            row=db.execute('SELECT * FROM repair_windows WHERE id=?'+(' AND owner=?' if owner is not None else ''),
                           (ident,owner) if owner is not None else (ident,)).fetchone()
        if row is None:raise TaskError('not_found')
        return self.public(row)
    def list(self,owner=None):
        with self.manager.db() as db:
            self.schema(db)
            rows=db.execute('SELECT * FROM repair_windows'+(' WHERE owner=?' if owner is not None else '')+' ORDER BY rowid DESC LIMIT 30',
                            (owner,) if owner is not None else ()).fetchall()
        return [self.public(row) for row in rows]
    def task(self,task_id,owner):
        if self.manager.task_loader is None:raise TaskError('repair_task_unavailable')
        task=self.manager.task_loader(task_id,owner)
        if (task['status'] not in ('pending','approved') or task['expires']<=self.manager.clock()
                or any(not set(checks)<=set(task['scope'].get(device,[])) for device,checks in READ_SCOPE.items())):
            raise TaskError('repair_task_not_pending')
        return task
    def request(self,owner,task_id):
        if not isinstance(task_id,str) or not re.fullmatch('[a-f0-9]{32}',task_id):raise ValueError('invalid_task')
        task=self.task(task_id,owner);ident=secrets.token_hex(16)
        body={'device':TARGET,'action':ACTION,'unit':'keepalived.service','max_writes':1,
              'question':task['question'][:240],'duration_minutes':None}
        with self.manager.db() as db:
            self.schema(db)
            db.execute('INSERT OR IGNORE INTO repair_windows VALUES (?,?,?,?,?,?,?)',
                       (ident,owner,task_id,'pending',min(task['expires'],self.manager.clock()+900),None,json.dumps(body,ensure_ascii=False)))
            row=db.execute('SELECT id FROM repair_windows WHERE task=? AND owner=?',(task_id,owner)).fetchone()
            if not row:raise TaskError('not_found')
            if row['id']==ident:self.event(db,ident,owner,'requested')
        return self.get(row['id'],owner)
    def grant(self,ident,actor,minutes,guard=lambda:None):
        if type(minutes) is not int or minutes not in (5,10,15):raise ValueError('invalid_duration')
        with self.manager.db() as db:
            self.schema(db);row=db.execute('SELECT * FROM repair_windows WHERE id=?',(ident,)).fetchone()
        if row is None:raise TaskError('not_found')
        if row['state']!='pending' or row['expires']<=self.manager.clock():raise TaskError('repair_window_not_pending')
        self.task(row['task'],row['owner']);guard()
        if self.manager.inspector is None:raise TaskError('repair_inspector_unavailable')
        report=self.manager.inspector({'operation':'inspect_keepalived','lease':'0'*32,'id':'0'*32})
        permit=report.get('permit',{}) if (report.get('ok') is True and report.get('id')=='0'*32 and report.get('operation')=='inspect_keepalived') else {}
        remaining=permit.get('remaining_seconds',0);lease=permit.get('lease','')
        if (permit.get('ready') is not True or permit.get('unused') is not True or type(remaining) not in (int,float)
                or remaining<30 or not re.fullmatch('[a-f0-9]{32}',str(lease))):raise TaskError('repair_device_not_armed')
        guard();task=self.task(row['task'],row['owner'])
        expires=min(self.manager.clock()+minutes*60,task['expires'],self.manager.clock()+min(remaining,900)-5)
        with self.manager.db() as db:
            self.schema(db);db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT id FROM repair_windows WHERE lease=?',(lease,)).fetchone():raise TaskError('repair_device_lease_used')
            body=json.loads(row['body']);body['duration_minutes']=minutes
            changed=db.execute("UPDATE repair_windows SET state='active',expires=?,lease=?,body=? WHERE id=? AND state='pending' AND expires>?",
                               (expires,lease,json.dumps(body,ensure_ascii=False),ident,self.manager.clock())).rowcount
            if not changed:raise TaskError('repair_window_not_pending')
            self.event(db,ident,actor,'granted')
        return self.get(ident)
    def revoke(self,ident,actor,owner=None):
        self.get(ident,owner)
        with self.manager.db() as db:
            self.schema(db);db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM repair_windows WHERE id=?',(ident,)).fetchone()
            db.execute("UPDATE repair_windows SET state='revoked' WHERE id=? AND state IN ('pending','active')",(ident,))
            db.execute("UPDATE repair_plans SET state='cancelled' WHERE task=? AND state IN ('pending','approved')",(row['task'],))
            db.execute("UPDATE repair_plans SET state='cancel_requested' WHERE task=? AND state='running'",(row['task'],))
            self.event(db,ident,actor,'revoked')
        return self.get(ident,owner)
    def policy(self,owner,task):
        if not self.manager.path.exists():return False,None
        with self.manager.db() as db:
            self.schema(db);row=db.execute('SELECT * FROM repair_windows WHERE owner=? AND task=?',(owner,task)).fetchone()
        if row is None:return False,None
        if row['state']!='active' or row['expires']<=self.manager.clock():return True,None
        if self.manager.task_loader is not None:
            try:task_state=self.manager.task_loader(task,owner)
            except (TaskError,KeyError):return True,None
            if task_state['status'] not in ('pending','approved','running','completed'):return True,None
        return True,{'enabled':True,'owner':owner,'task':task,'target':TARGET,'lease':row['lease'],'expires':row['expires']}
    def revoke_owner(self,owner):
        if not self.manager.path.exists():return
        for item in self.list(owner):
            if item['state'] in ('pending','active'):self.revoke(item['id'],owner,owner)
    def available(self):
        if not self.manager.path.exists():return False
        with self.manager.db() as db:
            self.schema(db);return db.execute("SELECT id FROM repair_windows WHERE state='active' AND expires>? LIMIT 1",(self.manager.clock(),)).fetchone() is not None
