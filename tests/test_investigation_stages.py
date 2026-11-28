"""同一批准下的阶段取证；临时SQLite/HTTP和确定性模型、设备替身。"""
if not __package__:
    import _bootstrap

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing
from approval_store import ApprovalStore, TaskError, SINGLE_APPROVAL, DIAGNOSTIC_DATA_USE
from chat_model import ModelFailure
from device_registry import CHECK_NAMES
from investigation_limits import legacy_limits, requested_limits, validate_limits
from unified_chat import ToolRejection, run
from diagnostic_core import AuditStore
from tests.ha_case_test_support import case_probe, case_planner
from tests.workflow_test_support import RunningApp

PAIR={device:list(CHECK_NAMES) for device in ('local-primary','local-backup')}
ALL={device:list(CHECK_NAMES) for device in ('local-primary','local-backup','remote-primary','remote-backup')}


def fixture(device,check):
    return case_probe('owner_running' if device.endswith('primary') else 'nonowner_stopped')(device,check)


def request(client,question='局端主机和备机当前Croc业务不通，请核对VIP、路由和日志。'):
    state=client.call('/api/conversations/stages/mode',{'mode':'assistant','scene':None})[1]
    code,response=client.call('/api/chat',{'question':question,
        'history':[],'allow_context':False,'conversation_id':'stages','interaction_mode':'assistant',
        'interaction_revision':state['revision'],'approval_flow':SINGLE_APPROVAL})
    assert code==200 and response['status']=='approval_required',response
    return response['task']


def approve(client,task):
    client.login()
    return client.call('/api/tasks/'+task['id']+'/decision',{'approved':True,'data_use':DIAGNOSTIC_DATA_USE})


def start(client,task):
    return client.call('/api/tasks/'+task['id']+'/start',{},timeout=15)


class StageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'tasks.db'
        self.store=ApprovalStore(self.path)
        self.mode=self.store.set_interaction('owner','stages','assistant')

    def tearDown(self):self.temp.cleanup()

    def task(self,scope=PAIR,flow=SINGLE_APPROVAL):
        task=self.store.create('owner','Synthetic stage fixture',[],scope,'stages',self.mode['revision'],flow)
        self.store.decide(task['id'],True,'task-owner' if flow else 'local-admin',
                          DIAGNOSTIC_DATA_USE if flow else None,owner='owner' if flow else None)
        self.store.claim(task['id'],'owner')
        return task['id']

    def test_new_scope_policy_is_capped_and_legacy_or_small_scopes_keep_four(self):
        self.assertEqual(requested_limits(PAIR,True)['max_reads'],8)
        self.assertEqual(requested_limits(ALL,True)['max_reads'],12)
        self.assertEqual(requested_limits(PAIR,False),legacy_limits())
        self.assertEqual(requested_limits({'local-primary':list(CHECK_NAMES)},True),legacy_limits())
        bad=dict(requested_limits(PAIR,True),max_reads=999)
        with self.assertRaises(ValueError):validate_limits(bad,PAIR)
        with self.assertRaises(ValueError):validate_limits(dict(legacy_limits(),max_stages=True),PAIR)
        ident=self.task(ALL,flow=None)
        for check in CHECK_NAMES:self.store.authorize_read(ident,'owner','local-primary',check)
        with self.assertRaisesRegex(ToolRejection,'read_budget_reached'):
            self.store.authorize_read(ident,'owner','local-backup','interfaces')
        self.assertEqual(self.store.get(ident)['execution_limits'],legacy_limits())

    def test_global_cap_scope_and_unique_reads_persist_across_three_stages(self):
        ident=self.task(ALL)
        choices=[(device,check) for device in ALL for check in CHECK_NAMES]
        for device,check in choices[:12]:self.store.authorize_read(ident,'owner',device,check)
        with self.assertRaisesRegex(ToolRejection,'read_budget_reached'):
            self.store.authorize_read(ident,'owner',*choices[12])
        with self.assertRaisesRegex(ToolRejection,'check_not_approved'):
            self.store.authorize_read(ident,'owner','router-1','interfaces')
        task=self.store.get(ident)
        self.assertEqual(task['attempts'],12)
        self.assertEqual([stage['reads'] for stage in task['investigation_stages']],[4,4,4])
        self.assertEqual([stage['status'] for stage in task['investigation_stages']],['completed','completed','running'])
        with self.assertRaises(TaskError):self.store.claim(ident,'owner')

    def test_concurrent_same_check_at_boundary_reserves_only_once(self):
        ident=self.task()
        for check in CHECK_NAMES:self.store.authorize_read(ident,'owner','local-primary',check)
        def attempt(_):
            try:self.store.authorize_read(ident,'owner','local-backup','interfaces');return 'reserved'
            except ToolRejection as error:return str(error)
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(attempt,range(2)))
        self.assertEqual(sorted(results),['duplicate_read','reserved'])
        self.assertEqual(self.store.get(ident)['attempts'],5)
        self.assertEqual([stage['reads'] for stage in self.store.get(ident)['investigation_stages']],[4,1])

    def test_limits_are_frozen_and_interrupted_stages_are_not_replayed(self):
        ident=self.task()
        choices=[(device,check) for device in PAIR for check in CHECK_NAMES]
        for device,check in choices[:5]:self.store.authorize_read(ident,'owner',device,check)
        with closing(self.store.connect()) as db,db:
            db.execute('UPDATE workflow_limits SET requested=? WHERE task=?',
                       (json.dumps(legacy_limits()),ident))
        with self.assertRaises(ToolRejection):self.store.assert_active(ident,'owner')
        with self.assertRaises(TaskError):self.store.authorize_read(ident,'owner',*choices[5])
        # 恢复审批时冻结的策略，仅用于继续检查重启恢复，不恢复任务执行。
        with closing(self.store.connect()) as db,db:
            db.execute('UPDATE workflow_limits SET requested=approved WHERE task=?',(ident,))
        reopened=ApprovalStore(self.path);reopened.recover_interrupted()
        task=reopened.get(ident)
        self.assertEqual(task['status'],'incomplete');self.assertEqual(task['attempts'],5)
        self.assertEqual(task['investigation_stages'][-1]['status'],'interrupted')
        with self.assertRaises(TaskError):reopened.claim(ident,'owner')


class StageHttpTests(unittest.TestCase):
    def test_four_nodes_continue_three_stages_with_one_approval_and_keep_full_local_evidence(self):
        reads=[]
        def probe(device,check):reads.append((device,check));return fixture(device,check)
        with RunningApp(case_planner,probe,lookup=lambda q:[]) as client:
            task=request(client,'局端主机、局端备机、远端主机和远端备机当前Croc业务不通，请详细核对四台VIP、路由和日志。')
            self.assertEqual(set(task['scope']),set(ALL))
            self.assertEqual(task['execution_limits']['max_reads'],12)
            self.assertEqual(approve(client,task)[0],200)
            code,done=start(client,task)
        self.assertEqual(code,200);self.assertEqual(done['status'],'completed',done)
        self.assertEqual(len(reads),12);self.assertEqual(len(set(reads)),12)
        self.assertEqual([stage['reads'] for stage in done['investigation_stages']],[4,4,4])
        self.assertEqual([item['id'] for item in done['result']['evidence']],['OBS-'+str(i) for i in range(1,13)])
        self.assertEqual(set(reads[-4:]),{(device,check) for device in ('local-primary','remote-primary') for check in ('routes','logs')})
        self.assertEqual(len(done['result']['diagnostic_case']['pairs']),2)
        self.assertFalse(done['result']['diagnostic_report']['root_cause_confirmed'])

    def test_overview_cannot_partly_cross_a_stage_boundary(self):
        reads=[]
        def probe(device,check):reads.append((device,check));return fixture(device,check)
        def planner(context):
            count=len(context['observations'])
            if count<3:return {'action':'read','device':'local-primary','check':('services','interfaces','routes')[count]},0
            return {'action':'read_overview','device':'local-backup'},0
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            task=request(client);approve(client,task);done=start(client,task)[1]
        self.assertEqual(done['attempts'],3);self.assertEqual(len(reads),3)
        self.assertEqual(done['result']['events'][-1]['reason'],'stage_read_budget_reached')
        self.assertEqual(len(done['investigation_stages']),1)

    def test_parent_time_budget_does_not_restart_in_second_stage(self):
        now=[0.0];reads=[]
        def probe(device,check):reads.append((device,check));return fixture(device,check)
        def planner(context):
            if len(context['observations'])>=4:now[0]=181.0
            return case_planner(context)
        with tempfile.TemporaryDirectory() as folder:
            result=run('局端主备当前Croc业务不通',[],planner,lambda q:[],probe,
                AuditStore(Path(folder)/'audit.db'),clock=lambda:now[0],permitted_checks=PAIR,
                question_policy={'mode':'diagnostic','scene':'ha_topology','require_current_evidence':True},
                execution_limits=requested_limits(PAIR,True))
        self.assertEqual(result['reason'],'time_budget_reached')
        self.assertEqual(len(reads),4)
        self.assertEqual(len(result['evidence']),4)

    def test_single_approval_carries_evidence_into_second_stage_without_repeated_reads(self):
        reads=[];contexts=[]
        def probe(device,check):reads.append((device,check));return fixture(device,check)
        def planner(context):contexts.append(context);return case_planner(context)
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            task=request(client)
            self.assertEqual(task['execution_limits']['max_reads'],8)
            self.assertEqual(start(client,task)[0],409);self.assertEqual(reads,[])
            self.assertEqual(approve(client,task)[0],200)
            self.assertEqual(client.call('/api/tasks/'+task['id']+'/start',{'execution_limits':legacy_limits()})[0],400)
            code,done=start(client,task)
            self.assertEqual(start(client,task)[0],409)
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/tasks/'+task['id'],browser=other)[0],404)
        self.assertEqual(code,200);self.assertEqual(done['status'],'completed',done)
        self.assertEqual(done['attempts'],6)
        self.assertEqual(len(set(reads)),6)
        self.assertEqual(set(reads[-2:]),{('local-primary','routes'),('local-primary','logs')})
        self.assertEqual([phase['reads'] for phase in done['investigation_stages']],[4,2])
        second=next(context for context in contexts if context['investigation_stage']['number']==2)
        self.assertEqual([item['id'] for item in second['observations']],['OBS-1','OBS-2','OBS-3','OBS-4'])
        self.assertEqual(len(second['attempted_checks']),4)
        self.assertFalse(done['result']['diagnostic_report']['root_cause_confirmed'])

    def test_cancel_or_expire_during_stage_two_keeps_inflight_evidence_and_stops_next_read(self):
        for condition in ('cancel','expire'):
            entered=threading.Event();release=threading.Event();reads=[];results=[]
            def probe(device,check):
                reads.append((device,check))
                if len(reads)==5:entered.set();release.wait(8)
                return fixture(device,check)
            with self.subTest(condition=condition),RunningApp(case_planner,probe,lookup=lambda q:[]) as client:
                task=request(client);approve(client,task)
                worker=threading.Thread(target=lambda:results.append(start(client,task)));worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    if condition=='cancel':self.assertEqual(client.call('/api/tasks/'+task['id']+'/cancel',{})[0],200)
                    else:
                        later=client.store.clock()+301;client.store.clock=lambda:later
                finally:release.set();worker.join(10)
                self.assertFalse(worker.is_alive())
                done=results[0][1]
                self.assertEqual(done['status'],'cancelled' if condition=='cancel' else 'expired')
                self.assertEqual(done['attempts'],5);self.assertEqual(len(reads),5)
                self.assertEqual(len(done['result']['evidence']),5)
                self.assertEqual(done['investigation_stages'][-1]['status'],done['status'])
                self.assertEqual(start(client,task)[0],409)

    def test_model_failure_after_first_stage_does_not_create_another_stage_or_retry(self):
        calls=[]
        def planner(context):
            calls.append(context)
            if len(context['observations'])>=4:raise ModelFailure('model_rate_limited',usage=None)
            return case_planner(context)
        with RunningApp(planner,fixture,lookup=lambda q:[]) as client:
            task=request(client);approve(client,task);done=start(client,task)[1]
        self.assertEqual(done['status'],'incomplete')
        self.assertEqual(done['attempts'],4)
        self.assertEqual(len(done['investigation_stages']),1)
        self.assertEqual(done['result']['reason'],'model_rate_limited')
        self.assertFalse(done['result']['total_tokens_complete'])
        self.assertEqual(len([context for context in calls if len(context['observations'])>=4]),1)

    def test_one_correction_is_shared_across_stages(self):
        bad_once=[False]
        def planner(context):
            count=len(context['observations'])
            if count==2 and not bad_once[0]:
                bad_once[0]=True
                return {'action':'answer','answer':'尚未核实','refs':[],'basis':'limited'},0
            if count>=5:return {'action':'answer','invalid':'fixture'},0
            return case_planner(context)
        with RunningApp(planner,fixture,lookup=lambda q:[]) as client:
            task=request(client);approve(client,task);done=start(client,task)[1]
        self.assertEqual(done['status'],'incomplete');self.assertEqual(done['attempts'],5)
        self.assertEqual(len([event for event in done['result']['events'] if event['action']=='correction']),1)


if __name__=='__main__':unittest.main()
