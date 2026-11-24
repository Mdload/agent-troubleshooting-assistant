import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from approval_store import ApprovalStore,TaskError
from diagnostic_core import AuditStore
from lab_workflow import WorkflowService
from workflow_test_support import fake_planner,fake_probe,current_probe_fixture
from question_routing import classify
from unified_chat import run,ToolRejection
from workflow_test_support import RunningApp
import app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'workflow.db'
        self.now=[1000.0];self.store=ApprovalStore(self.path,clock=lambda:self.now[0])
        self.probe=Mock(side_effect=fake_probe)
        self.service=WorkflowService(self.store,fake_planner,self.probe)

    def tearDown(self):self.temp.cleanup()

    def request(self):
        state=self.store.set_interaction('student','default','diagnostic','ha_topology')
        return self.service.ask('student','局端备机现在Croc没运行，帮我检查一下',[],True,
                                interaction_mode='diagnostic',interaction_revision=state['revision'])['task']

    def approve(self,task):return self.store.decide(task['id'],True,'local-admin')

    def test_development_routing_examples(self):
        groups={
            'general':['推荐一部电影','这道菜怎么做','Python列表是什么','MySQL事务是什么','函数怎么传参数','字典和列表的区别'],
            'lab_knowledge':['局端备机为什么不运行Croc','主备切换规则是什么','我们的新服务监听什么端口',
                             '局端备机怎样检查服务','如何排查当前Croc异常','我们的服务怎样重启',
                             '实验室业务路径是什么','远端主机只解释原理，不要检查',
                             'Linux默认路由是什么','HTTP和TCP有什么区别','SSH是什么','JSON怎么解析'],
            'diagnostic':['局端备机现在Croc没运行','远端主机今天业务不通','请检查局端主机',
                          '帮我排查远端备机','局端主备当前状态怎样','远端备机PPP掉线了',
                          '查看局端备机服务','局端备机突然没有业务'],
            'clarify':['现在远端业务不通','实验室现在服务异常','连不上怎么办','那现在呢'],
            'unsupported_device':['Router现在有没有故障','请检查路由器'],
            }
        for route,questions in groups.items():
            for question in questions:
                with self.subTest(question=question):self.assertEqual(classify(question)['route'],route)
        self.assertEqual(sum(map(len,groups.values())),32)

    def test_followup_changes_role_without_topic_pollution(self):
        hist=[{'role':'user','content':'局端备机为什么不运行Croc'}]
        r=classify('那主机现在呢',hist)
        self.assertEqual(list(r['scope']),['local-primary'])
        self.assertEqual(classify('Python列表是什么',hist)['route'],'general')

    def test_conversational_check_requests_and_faults_prepare_diagnostics(self):
        for question in ('帮我看看局端备机','麻烦看一下远端主机','看下局端主机服务',
                         '请检测远端备机','核对局端主机服务','帮我排障局端备机','远端主机业务断断续续',
                         '局端备机Croc起不来','远端主机延迟很高','局端主机访问不了'):
            with self.subTest(question=question):
                result=classify(question)
                self.assertEqual(result['route'],'diagnostic')
                self.assertTrue(result['scope'])
        self.assertEqual(classify('帮我看看设备')['route'],'clarify')
        self.assertEqual(classify('帮我排障')['route'],'clarify')

    def test_learning_and_explicit_no_read_requests_do_not_prepare_tasks(self):
        for question in ('怎么排查局端备机Croc起不来','怎么排障局端备机','丢包是什么','假如远端主机丢包应如何排查'):
            with self.subTest(question=question):
                self.assertNotEqual(classify(question)['route'],'diagnostic')
        for question in ('局端备机现在Croc起不来，只讲原理','局端主机当前状态，不用读取',
                         '远端主机业务断断续续，不要连接设备'):
            with self.subTest(question=question):
                self.assertEqual(classify(question)['route'],'current_unverified')

    def test_no_consent_no_task_or_probe(self):
        r=self.service.ask('student','局端备机当前状态',[],False)
        self.assertEqual(r['status'],'consent_required');self.assertEqual(self.store.list(),[])
        self.probe.assert_not_called()

    def test_request_does_not_execute_model_or_probe(self):
        self.service.planner=Mock(side_effect=AssertionError('planner must wait'))
        task=self.request();self.assertEqual(task['status'],'pending')
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        self.service.planner.assert_not_called();self.probe.assert_not_called()

    def test_approved_round_trip_records_synthetic_observations(self):
        task=self.request();self.approve(task)
        r=self.service.resume('student',task['id'])
        self.assertEqual(r['status'],'incomplete');self.assertEqual(r['attempts'],2)
        self.assertIn('状态未核实',r['result']['answer'])
        self.assertEqual(self.probe.call_count,2)
        self.assertTrue(all(o['origin']=='synthetic' for o in r['result']['evidence']))
        self.assertEqual(len([x for x in self.service.audit.read(task['id']) if x[1]=='observation']),2)

    def test_general_and_mechanism_never_read(self):
        self.assertEqual(self.service.ask('student','Linux默认路由是什么',[],True)['route'],'lab_knowledge')
        r=self.service.ask('student','局端备机为什么不运行Croc',[],True)
        self.assertTrue(r['sources']);self.probe.assert_not_called()

    def test_missing_lab_facts_do_not_create_diagnostic(self):
        self.service.lookup=Mock(return_value=[])
        r=self.service.ask('student','我们的新服务监听什么端口',[],True)
        self.assertEqual(r['status'],'answered');self.assertEqual(r['knowledge_status'],'missing');self.assertIn('监听端口还不确定',r['answer'])
        self.assertEqual(self.store.list(),[]);self.probe.assert_not_called()

    def test_rejected_and_foreign_tasks_never_read(self):
        task=self.request();self.store.decide(task['id'],False,'local-admin')
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        with self.assertRaises(TaskError):self.store.get(task['id'],'other-student')
        with self.assertRaises(TaskError):self.store.cancel(task['id'],'other-student')
        self.probe.assert_not_called()

    def test_admin_decision_requires_trusted_local_actor(self):
        task=self.request()
        with self.assertRaises(TaskError):self.store.decide(task['id'],True,'student')
        self.assertEqual(self.store.get(task['id'])['status'],'pending')

    def test_expired_approval_and_cancel_before_resume(self):
        task=self.request();self.approve(task);self.now[0]+=301
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        self.assertEqual(self.store.get(task['id'])['status'],'expired')
        second=self.request();self.approve(second);self.store.cancel(second['id'],'student')
        with self.assertRaises(TaskError):self.service.resume('student',second['id'])
        self.probe.assert_not_called()

    def test_cancellation_between_reads_preserves_first_observation(self):
        task=self.request();self.approve(task)
        def read(device,check):
            self.store.cancel(task['id'],'student');return fake_probe(device,check)
        self.service.probe=Mock(side_effect=read)
        r=self.service.resume('student',task['id'])
        self.assertEqual(r['status'],'cancelled');self.assertEqual(r['attempts'],1)
        self.assertEqual(len(r['result']['evidence']),1);self.service.probe.assert_called_once()

    def test_expiry_between_reads_stops_next_check(self):
        task=self.request();self.approve(task)
        def read(device,check):self.now[0]+=301;return fake_probe(device,check)
        self.service.probe=Mock(side_effect=read)
        r=self.service.resume('student',task['id'])
        self.assertEqual(r['status'],'expired');self.service.probe.assert_called_once()

    def test_repeated_resume_cannot_reuse_approval(self):
        task=self.request();self.approve(task);self.service.resume('student',task['id'])
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        self.assertEqual(self.probe.call_count,2)

    def test_scope_change_after_approval_is_rejected(self):
        task=self.request();self.approve(task)
        with sqlite3.connect(str(self.path)) as db:
            db.execute('UPDATE workflow_tasks SET scope=? WHERE id=?',
                       (json.dumps({'remote-primary':['routes']}),task['id']))
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        self.probe.assert_not_called()

    def test_knowledge_model_cannot_force_device_read(self):
        self.service.planner=Mock(return_value=({'action':'read','device':'local-backup','check':'services'},0))
        r=self.service.ask('student','Linux默认路由是什么',[],True)
        self.assertEqual(r['status'],'incomplete');self.assertEqual(r['events'][-1]['reason'],'check_not_approved')
        self.probe.assert_not_called()

    def test_overview_is_all_or_nothing_for_partial_grant(self):
        planner=Mock(return_value=({'action':'read_overview','device':'local-backup'},0))
        r=run('检查',[],planner,Mock(return_value=[]),self.probe,self.service.audit,
              permitted_checks={'local-backup':['services']})
        self.assertEqual(r['status'],'incomplete');self.probe.assert_not_called()
        catalog=planner.call_args[0][0]['tool_catalog']
        self.assertNotIn('overview',catalog['local-backup'])

    def test_cross_device_model_request_is_rejected(self):
        task=self.request();self.approve(task)
        self.service.planner=Mock(return_value=({'action':'read','device':'remote-primary','check':'services'},0))
        r=self.service.resume('student',task['id'])
        self.assertEqual(r['status'],'incomplete');self.assertEqual(r['attempts'],0);self.probe.assert_not_called()

    def test_failed_probe_remains_failure_not_device_damage(self):
        task=self.request();self.approve(task);self.probe.side_effect=None
        self.probe.return_value={'ok':False,'error':'timeout','origin':'synthetic'}
        r=self.service.resume('student',task['id'])
        self.assertEqual(r['status'],'incomplete');self.assertIn('状态未核实',r['result']['answer'])

    def test_task_and_browser_session_persist_across_service_restart(self):
        task=self.request();self.approve(task);self.store.add_session('owner-hash')
        reopened=ApprovalStore(self.path,clock=lambda:self.now[0])
        self.assertEqual(reopened.get(task['id'],'student')['status'],'approved')
        self.assertTrue(reopened.has_session('owner-hash'))

    def test_interrupted_running_task_is_not_replayed(self):
        task=self.request();self.approve(task);self.store.claim(task['id'],'student')
        self.store.authorize_read(task['id'],'student','local-backup','services')
        self.store.recover_interrupted()
        r=self.store.get(task['id']);self.assertEqual(r['status'],'incomplete');self.assertEqual(r['attempts'],1)
        with self.assertRaises(TaskError):self.service.resume('student',task['id'])
        self.probe.assert_not_called()

    def test_persistent_read_budget_and_duplicate_guard(self):
        scope={'local-primary':['services','interfaces','routes'],'local-backup':['services','interfaces']}
        state=self.store.set_interaction('student','default','diagnostic','ha_topology')
        task=self.store.create('student','当前状态',[],scope,revision=state['revision']);self.approve(task);self.store.claim(task['id'],'student')
        for device,check in [('local-primary','services'),('local-primary','interfaces'),('local-primary','routes'),('local-backup','services')]:
            self.store.authorize_read(task['id'],'student',device,check)
        with self.assertRaises(ToolRejection):self.store.authorize_read(task['id'],'student','local-backup','interfaces')
        self.assertEqual(self.store.get(task['id'])['attempts'],4)

    def test_concurrent_claim_is_single_use(self):
        task=self.request();self.approve(task);outcomes=[]
        def claim():
            try:self.store.claim(task['id'],'student');outcomes.append('claimed')
            except TaskError:outcomes.append('rejected')
        threads=[threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(outcomes),['claimed','rejected'])


class WorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=ApprovalStore(Path(self.temp.name)/'http.db')
        self.probe=Mock(side_effect=fake_probe)
        self.client=RunningApp(fake_planner,self.probe).__enter__()
        self.store=self.client.store;self.server=self.client.server;self.base=self.client.base
        self.cookie=self.session()

    def tearDown(self):self.client.__exit__();self.temp.cleanup()

    def session(self):
        with urlopen(self.base+'/api/session') as response:return response.headers['Set-Cookie'].split(';')[0]

    def call(self,path,data=None,cookie=None,origin=True):
        headers={'Cookie':cookie or self.cookie}
        if data is not None:
            headers.update({'Content-Type':'application/json','X-ATA-Token':app.CSRF_TOKEN})
            if origin:headers['Origin']=self.base
        request=Request(self.base+path,headers=headers,data=json.dumps(data).encode('utf-8') if data is not None else None)
        try:
            with urlopen(request) as response:return response.status,json.loads(response.read().decode('utf-8'))
        except HTTPError as error:return error.code,json.loads(error.read().decode('utf-8'))

    def create(self):
        _,state=self.call('/api/conversations/default/mode',{'mode':'diagnostic','scene':'ha_topology'})
        code,r=self.call('/api/chat',{'question':'局端备机当前状态','history':[],'allow_context':True,
                                    'interaction_mode':'diagnostic','interaction_revision':state['revision']})
        self.assertEqual(code,200);return r['task']

    def test_http_student_admin_continue_round_trip(self):
        self.probe.side_effect=current_probe_fixture
        consent={'allow_context':True,'allow_live_data':True}
        task=self.create();self.assertEqual(self.call('/api/tasks/'+task['id']+'/resume',consent)[0],409)
        self.probe.assert_not_called()
        from approval_store import DIAGNOSTIC_DATA_USE
        self.assertEqual(self.call('/api/tasks/'+task['id']+'/decision',
            {'approved':True,'data_use':DIAGNOSTIC_DATA_USE})[0],200)
        code,r=self.call('/api/tasks/'+task['id']+'/resume',consent)
        self.assertEqual(code,200);self.assertEqual(r['status'],'completed');self.assertEqual(r['attempts'],2)

    def test_http_cannot_self_approve_or_change_scope(self):
        task=self.create()
        self.assertEqual(self.call('/api/tasks/'+task['id']+'/approve',{})[0],404)
        self.assertEqual(self.call('/api/chat',{'question':'当前状态','allow_context':True,'role':'admin'})[0],400)
        self.assertEqual(self.call('/api/tasks/'+task['id']+'/resume',{'scope':{'remote-primary':['routes']}})[0],400)
        self.assertEqual(self.store.get(task['id'])['status'],'pending');self.probe.assert_not_called()

    def test_http_foreign_browser_isolation(self):
        task=self.create();other=self.session()
        for path,data in [('/api/tasks/'+task['id'],None),('/api/tasks/'+task['id']+'/cancel',{}),('/api/tasks/'+task['id']+'/resume',{'allow_context':True,'allow_live_data':True})]:
            self.assertEqual(self.call(path,data,other)[0],404)
        self.assertEqual(self.call('/api/tasks',cookie=other)[1]['tasks'],[])

    def test_http_cross_origin_and_missing_origin_rejected(self):
        data={'question':'局端备机当前状态','allow_context':True}
        self.assertEqual(self.call('/api/chat',data,origin=False)[0],403)
        self.probe.assert_not_called()

    def test_http_page_refresh_reuses_browser_session(self):
        task=self.create()
        with urlopen(Request(self.base+'/api/session',headers={'Cookie':self.cookie})) as response:
            self.assertIsNone(response.headers['Set-Cookie'])
        self.assertEqual(self.call('/api/tasks')[1]['tasks'][0]['id'],task['id'])


if __name__=='__main__':unittest.main()
