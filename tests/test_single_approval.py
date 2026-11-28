"""一次范围批准的实际HTTP边界；全程使用固定模型/设备替身。"""
if not __package__:
    import _bootstrap

import unittest
import time
from contextlib import closing
from unittest.mock import Mock
from approval_store import SINGLE_APPROVAL,DIAGNOSTIC_DATA_USE
from chat_model import ModelFailure
from question_routing import classify
from tests.workflow_test_support import RunningApp,current_probe_fixture


def planner(context):
    if not context['observations']:
        return {'action':'read','device':next(iter(context['tool_catalog'])),'check':'services'},1
    return {'action':'answer','answer':'本轮仅取得服务状态，角色与业务仍未核实。',
            'refs':[context['observations'][0]['id']],'basis':'limited'},1


class SingleApprovalTests(unittest.TestCase):
    def setUp(self):
        self.planner=Mock(side_effect=planner)
        self.probe=Mock(side_effect=current_probe_fixture)
        self.client=RunningApp(self.planner,self.probe,lookup=lambda q:[]).__enter__()
        self.client.call('/api/conversations/once/mode',{'mode':'assistant','scene':None})

    def tearDown(self):self.client.__exit__()

    def request(self,question='局端备机当前服务状态',flow=SINGLE_APPROVAL,consent=False):
        state=self.client.call('/api/conversations/once/mode')[1]
        return self.client.call('/api/chat',{'question':question,'history':[],
            'conversation_id':'once','interaction_mode':'assistant','interaction_revision':state['revision'],
            'allow_context':consent,'approval_flow':flow})

    def approve(self,task):
        return self.client.call('/api/tasks/'+task['id']+'/decision',
                                {'approved':True,'data_use':DIAGNOSTIC_DATA_USE})

    def start(self,task,browser=None):
        return self.client.call('/api/tasks/'+task['id']+'/start',{},browser=browser)

    def test_request_needs_no_duplicate_checkbox_but_approval_binds_data_and_scope(self):
        code,result=self.request()
        self.assertEqual(code,200);self.assertEqual(result['status'],'approval_required')
        task=result['task']
        self.assertFalse(task['data_use_approved'])
        self.assertEqual(self.start(task)[0],409)
        self.planner.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.client.call('/api/tasks/'+task['id']+'/decision',{'approved':True})[0],409)
        self.assertEqual(self.client.call('/api/tasks/'+task['id']+'/decision',
            {'approved':True,'data_use':'unregistered_destination'})[0],409)
        code,approved=self.approve(task)
        self.assertEqual(code,200);self.assertTrue(approved['data_use_approved'])
        self.assertEqual(approved['scope'],task['scope'])
        self.probe.assert_not_called()
        code,done=self.start(task)
        self.assertEqual(code,200);self.assertEqual(done['status'],'completed')
        self.assertEqual(done['attempts'],1)
        self.assertEqual(self.probe.call_args[0],('local-backup','services'))
        self.assertEqual(self.start(task)[0],409)
        self.assertEqual(self.probe.call_count,1)
        with closing(self.client.store.connect()) as db:
            event=db.execute("SELECT body FROM workflow_events WHERE task=? AND kind='approved'",(task['id'],)).fetchone()
            self.assertIn(DIAGNOSTIC_DATA_USE,event['body'])

    def test_server_task_waits_for_its_own_input_then_continues_under_the_same_grant(self):
        scope={'local-primary':['services','interfaces','routes']}
        question='接收那头的业务程序在跑吗？'
        supplement='按你说的先看第一个。'
        clarification='下一步先确认业务地址，还是查看路由？'

        def plan(context):
            if 'diagnostic_options' in context:
                return {'action':'prepare_diagnostic','scope':scope},0
            count=len(context['observations'])
            if count==0:return {'action':'read','device':'local-primary','check':'services'},0
            if not context['task_thread']:return {'action':'clarify','question':clarification},0
            if count==1 and not context['correction_feedback']:
                return {'action':'read','device':'remote-primary','check':'routes'},0
            if count==1:return {'action':'read','device':'local-primary','check':'interfaces'},0
            return {'action':'answer','answer':'已查看服务和接口，端到端业务仍待核实。',
                    'refs':['OBS-1','OBS-2'],'basis':'limited'},0

        self.planner.side_effect=plan
        self.client.service.prefetch_overview=True
        task=self.request(question,consent=True)[1]['task']
        path='/api/tasks/'+task['id']
        payload={'message_id':'note-1','text':supplement}
        self.assertEqual(self.client.call(path+'/input',payload)[0],409)
        self.probe.assert_not_called()
        code,grant=self.client.call(path+'/decision',{'approved':True,'data_use':DIAGNOSTIC_DATA_USE,'background':True})
        self.assertEqual(code,200)
        self.assertTrue(grant['server_owned'])

        def wait_until(predicate):
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                view=self.client.store.get(task['id'])
                if predicate(view):return view
                time.sleep(.02)
            self.fail('Task did not advance: %r'%view)

        try:
            waiting=wait_until(lambda view:(view.get('progress') or {}).get('stage')=='awaiting_input')
            self.assertEqual(waiting['progress']['question'],clarification)
            self.probe.assert_called_once_with('local-primary','services')
            self.assertTrue(self.client.service.slot.acquire(timeout=1))
            self.client.service.slot.release()
            other=self.client.new_browser();self.client.call('/api/session',browser=other)
            self.assertEqual(self.client.call(path+'/input',payload,browser=other)[0],404)
            self.assertEqual(self.client.call(path+'/input',dict(payload,scope={'remote-primary':['routes']}))[0],400)
            self.assertEqual(self.client.call(path+'/input',payload)[0],200)

            done=wait_until(lambda view:view['status']=='completed')
            self.assertEqual(done['scope'],scope)
            self.assertEqual(done['expires'],grant['expires'])
            self.assertEqual(done['task_inputs'],[supplement])
            self.assertEqual([item['id'] for item in done['result']['evidence']],['OBS-1','OBS-2'])
            self.assertIn({'action':'rejected','reason':'check_not_approved'},done['result']['events'])
            self.assertEqual([call[0] for call in self.probe.call_args_list],
                             [('local-primary','services'),('local-primary','interfaces')])
            last=self.planner.call_args[0][0]
            self.assertEqual(last['question'],question)
            self.assertEqual(last['task_thread'],[{'role':'assistant','content':clarification},
                                                  {'role':'user','content':supplement}])
            self.assertEqual(last['remaining_reads'],2)
            self.assertEqual(self.client.call(path+'/input',dict(payload,message_id='note-2'))[0],409)
            self.assertEqual(self.start(task)[0],409)
        finally:
            if self.client.store.get(task['id'])['status'] in ('approved','running'):
                self.client.call(path+'/cancel',{})

    def test_single_flow_does_not_default_general_model_consent(self):
        code,result=self.request('Python列表是什么')
        self.assertEqual(code,200);self.assertEqual(result['status'],'consent_required')
        self.planner.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.client.store.list(),[])
        with self.client.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workflow_topics').fetchone()[0],0)
        self.assertEqual(self.request(flow='approve_everything')[0],400)

    def test_natural_chat_asks_before_execution_and_requester_confirms_without_admin(self):
        self.planner.return_value=({'action':'answer','answer':'列表按顺序保存元素。','refs':[],'basis':'general'},1)
        self.planner.side_effect=None
        code,answer=self.request('Python列表是什么',consent=True)
        self.assertEqual(code,200);self.assertEqual(answer['status'],'answered')
        self.assertEqual(self.client.store.list(),[]);self.probe.assert_not_called()
        self.planner.reset_mock();self.planner.side_effect=planner
        code,request=self.request('帮我看看局端备机')
        self.assertEqual(code,200);self.assertEqual(request['status'],'approval_required')
        self.assertIn('是否同意',request['answer']);task=request['task']
        self.assertEqual(task['status'],'pending');self.planner.assert_not_called();self.probe.assert_not_called()
        self.client.auth.path.unlink() # 仅删除临时夹具配置，证明没有管理员也能确认。
        self.assertEqual(self.approve(task)[0],200)
        self.assertFalse(self.client.call('/api/admin/status')[1]['authenticated'])
        self.assertEqual(self.start(task)[1]['status'],'completed');self.assertEqual(self.probe.call_count,1)

    def test_same_intake_accepts_natural_questions_about_different_checks(self):
        examples=(('收视频那边的两台里面，此刻哪台拿着业务地址？',
                   {'local-primary':['interfaces'],'local-backup':['interfaces']}),
                  ('发送端上负责把数据送出去的程序突然停了，能帮我看下吗？',
                   {'remote-primary':['services','logs'],'remote-backup':['services','logs']}),
                  ('远端能 ping 通，但业务不通。',
                   {'remote-primary':['interfaces','routes'],'local-primary':['interfaces','routes']}))
        self.planner.side_effect=None
        for question,scope in examples:
            with self.subTest(question=question):
                self.assertNotEqual(classify(question)['route'],'diagnostic')
                self.planner.return_value=({'action':'prepare_diagnostic','scope':scope},1)
                code,result=self.request(question,consent=True)
                self.assertEqual(code,200)
                self.assertEqual(result['status'],'approval_required',result)
                task=result['task']
                self.assertEqual(task['scope'],scope)
                self.assertFalse(task['data_use_approved'])
                self.assertEqual(self.start(task)[0],409)
                context=self.planner.call_args[0][0]
                self.assertEqual(context['question'],question)
                self.assertEqual(context['observations'],[])
                self.assertEqual(context['tool_catalog'],{})
                self.assertTrue(set(scope)<=set(context['diagnostic_options']))
        self.probe.assert_not_called()
        self.assertEqual(self.planner.call_count,len(examples))
        self.assertEqual(self.approve(task)[0],200)
        self.planner.side_effect=[({'action':'read','device':'remote-primary','check':'routes'},1),
            ({'action':'answer','answer':'只查到了远端路由，其他路径仍待检查。','refs':['OBS-1'],'basis':'limited'},1)]
        done=self.start(task)[1]
        self.assertEqual(done['status'],'completed',done)
        self.probe.assert_called_once_with('remote-primary','routes')

    def test_clarification_keeps_the_original_issue_for_the_users_next_turn(self):
        symptom='画面忽明忽暗。'
        self.assertEqual(classify(symptom)['route'],'general')
        self.planner.side_effect=None
        self.planner.return_value=({'action':'clarify','question':'你想排查采集端还是接收端？'},1)
        code,result=self.request(symptom,consent=True)
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'clarify')
        self.assertIn('接收端',result['answer'])
        self.assertEqual(self.client.store.list(),[])

        self.planner.return_value=({'action':'prepare_diagnostic','scope':{'local-primary':['services']}},1)
        code,result=self.request('先确认接收端有没有启动业务',consent=True)
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'approval_required',result)
        task=result['task']
        self.assertEqual(set(task['scope']),{'local-primary'})
        self.assertEqual(self.planner.call_args[0][0]['workflow_policy']['clarification_context'],symptom)
        self.assertEqual(self.planner.call_count,2)
        self.probe.assert_not_called()
        self.assertEqual(self.approve(task)[0],200)
        self.planner.side_effect=planner
        self.assertEqual(self.start(task)[1]['status'],'completed')
        self.assertIn({'role':'user','content':symptom},self.planner.call_args_list[2][0][0]['history'])

    def test_intake_cannot_act_directly_or_propose_an_unavailable_scope(self):
        self.planner.side_effect=None
        cases=(('接收那头的程序在跑吗？',{'action':'read','device':'local-primary','check':'services'}),
               ('接收那头的程序在跑吗？',{'action':'prepare_diagnostic','scope':{'router-1':['services']}}),
               ('接收那头的程序在跑吗？',{'action':'prepare_diagnostic','scope':{'local-primary':['restart']}}),
               ('只查局端的转发方向',{'action':'prepare_diagnostic','scope':{'remote-backup':['routes']}}),
               ('Python的列表如何排序？',{'action':'prepare_diagnostic','scope':{'local-primary':['services']}}),
               ('只想听一下原理，不要查设备',{'action':'prepare_diagnostic','scope':{'local-primary':['interfaces']}}))
        for question,action in cases:
            with self.subTest(question=question,action=action):
                self.planner.return_value=(action,1)
                code,result=self.request(question,consent=True)
                self.assertEqual(code,200)
                self.assertNotEqual(result['status'],'approval_required')
        self.assertEqual(self.client.store.list(),[])
        self.probe.assert_not_called()
        self.planner.reset_mock()
        self.assertEqual(self.request('远端业务不通',consent=False)[1]['status'],'clarify')
        self.planner.assert_not_called()

    def test_unclear_live_request_can_still_ask_for_details_when_model_is_unavailable(self):
        self.planner.side_effect=ModelFailure('model_timeout')
        code,result=self.request('远端业务不通',consent=True)
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'clarify')
        self.assertIn('未核实',result['answer'])
        self.assertEqual(self.client.store.list(),[])
        self.probe.assert_not_called()

    def test_only_own_task_can_be_confirmed_with_same_origin_and_exact_fields(self):
        task=self.request()[1]['task'];path='/api/tasks/'+task['id']+'/decision'
        payload={'approved':True,'data_use':DIAGNOSTIC_DATA_USE}
        other=self.client.new_browser();self.client.call('/api/session',browser=other)
        self.assertEqual(self.client.call(path,payload,browser=other)[0],404)
        self.assertEqual(self.client.call(path,payload,token=False)[0],403)
        self.assertEqual(self.client.call(path,payload,origin='http://example.invalid')[0],403)
        for extra in ({'scope':{'remote-primary':['routes']}},{'owner':'forged'},{'role':'admin'},{'approved':'true'}):
            self.assertEqual(self.client.call(path,dict(payload,**extra))[0],400)
        self.assertEqual(self.client.store.get(task['id'])['status'],'pending')
        self.planner.assert_not_called();self.probe.assert_not_called()

    def test_account_cannot_confirm_another_accounts_task(self):
        from unittest.mock import patch
        with patch('user_accounts.ITERATIONS',1000):
            payload={'username':'first_user','password':'testonly','allow_storage':True}
            self.assertEqual(self.client.call('/api/account/register',payload)[0],200)
            self.client.call('/api/conversations/once/mode',{'mode':'assistant','scene':None})
            task=self.request()[1]['task']
            other=self.client.new_browser()
            self.assertEqual(self.client.call('/api/account/register',dict(payload,username='other_user'),browser=other)[0],200)
            path='/api/tasks/'+task['id']+'/decision'
            self.assertEqual(self.client.call(path,{'approved':True,'data_use':DIAGNOSTIC_DATA_USE},browser=other)[0],404)
            self.assertEqual(self.approve(task)[0],200)
            self.probe.assert_not_called()

    def test_repeated_or_expired_confirmation_cannot_renew_permission(self):
        task=self.request()[1]['task'];self.assertEqual(self.approve(task)[0],200)
        expiry=self.client.store.get(task['id'])['expires']
        self.assertEqual(self.approve(task)[0],409)
        self.assertEqual(self.client.store.get(task['id'])['expires'],expiry)
        now=self.client.store.clock();self.client.store.clock=lambda:now+301
        self.assertEqual(self.approve(task)[0],409);self.assertEqual(self.start(task)[0],409)
        self.probe.assert_not_called()

    def test_declining_in_chat_cancels_without_model_or_device_calls(self):
        task=self.request('远端主机业务断断续续')[1]['task']
        code,cancelled=self.client.call('/api/tasks/'+task['id']+'/cancel',{})
        self.assertEqual(code,200);self.assertEqual(cancelled['status'],'cancelled')
        self.assertEqual(self.start(task)[0],409)
        self.planner.assert_not_called();self.probe.assert_not_called()

    def test_legacy_approval_and_request_flags_cannot_be_upgraded_to_data_permission(self):
        task=self.request(consent=True,flow=None)[1]['task']
        self.assertEqual(task['approval_flow'],SINGLE_APPROVAL)
        self.client.login()
        self.assertEqual(self.client.call('/api/admin/tasks/'+task['id']+'/decision',
            {'approved':True,'data_use':DIAGNOSTIC_DATA_USE})[0],410)
        self.assertEqual(self.start(task)[0],409)
        code,_=self.client.call('/api/tasks/'+task['id']+'/start',{'allow_live_data':True})
        self.assertEqual(code,400)
        self.planner.assert_not_called();self.probe.assert_not_called()

    def test_another_browser_cannot_start_the_task(self):
        task=self.request()[1]['task'];self.approve(task)
        other=self.client.new_browser();self.client.call('/api/session',browser=other)
        self.assertEqual(self.start(task,other)[0],404)
        self.probe.assert_not_called()
        self.assertEqual(self.start(task)[1]['status'],'completed')

    def test_new_target_requires_a_separate_range_and_data_approval(self):
        first=self.request()[1]['task'];self.approve(first);self.start(first)
        second=self.request('远端主机当前服务状态')[1]['task']
        self.assertNotEqual(first['id'],second['id'])
        self.assertFalse(second['data_use_approved'])
        self.assertEqual(set(second['scope']),{'remote-primary'})
        self.assertEqual(self.start(second)[0],409)
        self.assertEqual(self.probe.call_count,1)

    def test_cancellation_expiry_and_mode_change_stop_approved_automatic_start(self):
        for condition in ('cancel','expire','mode'):
            self.client.call('/api/conversations/once/mode',{'mode':'assistant','scene':None})
            task=self.request()[1]['task'];self.approve(task)
            if condition=='cancel':self.client.call('/api/tasks/'+task['id']+'/cancel',{})
            elif condition=='expire':
                now=self.client.store.clock();self.client.store.clock=lambda:now+301
            else:self.client.call('/api/conversations/once/mode',{'mode':'qa','scene':None})
            self.assertEqual(self.start(task)[0],409)
            self.probe.assert_not_called()
            if condition=='expire':self.client.store.clock=__import__('time').time

    def test_no_automatic_start_can_bypass_model_slots(self):
        task=self.request()[1]['task'];self.approve(task)
        self.client.server.model_slots.acquire();self.client.server.model_slots.acquire()
        try:self.assertEqual(self.start(task)[0],429)
        finally:self.client.server.model_slots.release();self.client.server.model_slots.release()
        self.probe.assert_not_called()
        self.assertEqual(self.start(task)[1]['status'],'completed')


    def test_admin_session_and_trusted_admin_actor_cannot_approve_requester_task(self):
        from approval_store import TaskError
        task=self.request()[1]['task'];self.client.login()
        for actor in ('authenticated-admin','local-admin'):
            with self.assertRaisesRegex(TaskError,'requester_required'):
                self.client.store.decide(task['id'],True,actor,DIAGNOSTIC_DATA_USE)
        self.assertEqual(self.client.call('/api/admin/tasks')[0],410)
        self.assertEqual(self.client.call('/api/admin/tasks/'+task['id'])[0],410)
        self.assertEqual(self.client.call('/api/admin/tasks/'+task['id']+'/decision',
            {'approved':True,'data_use':DIAGNOSTIC_DATA_USE})[0],410)
        self.assertEqual(self.client.store.get(task['id'])['status'],'pending')
        self.assertEqual(self.approve(task)[0],200)
        self.probe.assert_not_called()

    def test_missing_flow_uses_requester_approval_without_admin(self):
        state=self.client.call('/api/conversations/once/mode')[1]
        result=self.client.call('/api/chat',{'question':'检查局端备机当前服务状态','history':[],
            'conversation_id':'once','interaction_mode':'assistant',
            'interaction_revision':state['revision'],'allow_context':False})[1]
        task=result['task']
        self.assertEqual(task['approval_flow'],SINGLE_APPROVAL)
        self.assertEqual(self.start(task)[0],409)
        self.assertEqual(self.approve(task)[0],200)
        self.assertEqual(self.start(task)[1]['status'],'completed')
        self.assertEqual(self.probe.call_count,1)


if __name__=='__main__':unittest.main()
