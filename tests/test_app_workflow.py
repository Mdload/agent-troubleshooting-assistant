"""v0.8正式入口集成检查；上游模型与worker用边界替身，禁止实际外发。"""
import json
import unittest
from unittest.mock import Mock, patch
from server.admin_auth import AdminAuth, password_record
from server.approval_store import SINGLE_APPROVAL
from tests.workflow_test_support import RunningApp, TEST_PASSWORD, current_probe_fixture
from version import VERSION
from legacy.question_routing import classify


def answer(text='默认路由用于没有更具体匹配的目的地址。',basis='general',refs=None):
    return {'action':'answer','answer':text,'basis':basis,'refs':refs or []},3


class AppWorkflowTests(unittest.TestCase):
    def test_http_trace_links_redacted_question_and_answer_to_model_loop(self):
        marker = 'SYNTHETIC_CANARY_812'
        with RunningApp(lambda _: answer('复述 ' + marker), lookup=lambda _:[]) as client:
            code, result = client.call('/api/chat', {
                'question':'解释一下 Python 列表。 password=' + marker,
                'allow_context':True,'chat_space':'general','approval_flow':SINGLE_APPROVAL})
            self.assertEqual(code, 200, result)
            audit = client.service.audit
            saved = audit.read(result['trace_id'])
            self.assertEqual([row[1] for row in saved], ['chat_request','chat_result'])
            self.assertNotIn(marker, json.dumps(saved))
            self.assertEqual(audit.read(result['task'])[0][2]['request_trace'],result['trace_id'])

    def test_why_fault_is_not_automatically_mechanism_and_rag_is_general(self):
        self.assertEqual(classify('远端主机业务为什么不通')['route'],'diagnostic')
        self.assertEqual(classify('远端业务为什么不通')['route'],'clarify')
        self.assertEqual(classify('RAG是什么')['route'],'general')
        self.assertEqual(classify('局端备机为什么不运行Croc')['route'],'lab_knowledge')

    def test_formal_health_and_page(self):
        with RunningApp() as client:
            health=client.call('/api/health')[1]
            self.assertEqual(health['version'],VERSION)
            self.assertTrue(health['device_approval_required'])
            with client.opener.open(client.base) as response:
                page=response.read().decode('utf-8')
            self.assertIn('admin-login',page)
            self.assertIn('window.createSelectionConversation',page)
            self.assertNotIn('__SELECTION_FOLLOWUP_SCRIPT__',page)
            self.assertIn('window.readSelectionExplanation',page);self.assertNotIn('__SELECTION_STREAM_SCRIPT__',page)
            self.assertIn('v'+VERSION,page);self.assertNotIn('演练',page)

    def test_request_waits_without_initializing_model_or_worker(self):
        with RunningApp() as client, patch('legacy.unified_chat.qa.model_config') as config, patch('legacy.unified_chat.remote_probe') as probe:
            code,result=client.diagnostic_chat()
            self.assertEqual(code,200);self.assertEqual(result['status'],'approval_required')
            config.assert_not_called();probe.assert_not_called()
            self.assertEqual(client.resume(result['task']['id'])[0],409)

    def test_retired_admin_endpoints_cannot_approve_or_list_tasks(self):
        with RunningApp() as client:
            task=client.diagnostic_chat()[1]['task']
            self.assertEqual(client.call('/api/admin/tasks/'+task['id']+'/decision',{'approved':True})[0],410)
            self.assertEqual(client.call('/api/admin/tasks')[0],410)
            self.assertEqual(client.call('/api/chat',{'question':'局端备机当前状态','allow_context':True,'role':'admin'})[0],400)
            self.assertEqual(client.call('/api/admin/login',{'password':TEST_PASSWORD,'role':'admin'})[0],400)
            self.assertEqual(client.store.get(task['id'])['status'],'pending')

    def test_login_approve_resume_and_once_only_real_adapter_wiring(self):
        planner=Mock(side_effect=[({'action':'read_overview','device':'local-backup'},1),
            answer('服务与接口已取得局部观测，端到端业务未验证。','evidence',['OBS-1','OBS-2'])])
        with RunningApp(lookup=Mock(return_value=[])) as client, patch('legacy.unified_chat.make_model',return_value=planner) as factory, \
             patch('legacy.unified_chat.qa.model_config',return_value={}), patch('legacy.unified_chat.remote_probe',side_effect=current_probe_fixture) as probe:
            task=client.diagnostic_chat()[1]['task'];self.assertEqual(client.login()[0],200)
            self.assertEqual(client.approve(task['id'])[0],200)
            factory.assert_not_called();probe.assert_not_called()
            self.assertEqual(client.resume(task['id'],False)[0],409);probe.assert_not_called()
            code,result=client.resume(task['id'])
            self.assertEqual(code,200);self.assertEqual(result['status'],'completed')
            factory.assert_called_once_with({},availability=client.service.model_availability)
            self.assertEqual(probe.call_count,2)
            self.assertEqual(probe.call_args_list[0][0],('local-backup','services'))
            self.assertEqual(probe.call_args_list[1][0],('local-backup','interfaces'))
            self.assertEqual(client.resume(task['id'])[0],409);self.assertEqual(probe.call_count,2)

    def test_general_chat_uses_actual_model_transport_with_empty_catalog(self):
        config={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test-placeholder','LLM_MODEL_ID':'test-model'}
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        action=answer()[0]
        response.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(action)},'finish_reason':'stop'}],
            'usage':{'total_tokens':12}}).encode()
        opener=Mock();opener.open.return_value=response
        with RunningApp(lookup=Mock(return_value=[])) as client, patch('legacy.unified_chat.qa.model_config',return_value=config), \
             patch('agent.chat_model.urllib.request.build_opener',return_value=opener), patch('legacy.unified_chat.remote_probe') as probe:
            code,result=client.chat('Linux默认路由是什么')
            self.assertEqual(code,200);self.assertEqual(result['status'],'answered')
            self.assertEqual(result['total_tokens'],12);probe.assert_not_called()
            request=opener.open.call_args[0][0]
            self.assertEqual(request.full_url,'https://api.inferera.com/v1/chat/completions')
            payload=json.loads(request.data.decode())
            context=json.loads(payload['messages'][1]['content'])
            self.assertEqual(context['tool_catalog'],{});self.assertEqual(context['remaining_reads'],0)
            self.assertEqual(context['workflow_policy']['mode'],'knowledge')

    def test_missing_lab_knowledge_invokes_model_and_never_reads(self):
        planner=Mock(return_value=answer('一般原理：监听端口由服务配置决定；缺少该服务的配置，无法确认实验室实际端口。','limited'))
        probe=Mock()
        with RunningApp(planner,probe,lookup=Mock(return_value=[])) as client:
            result=client.chat('实验室的新服务监听什么端口')[1]
            self.assertEqual(result['status'],'answered');self.assertEqual(result['knowledge_status'],'missing')
            self.assertIn('监听端口',result['answer']);planner.assert_called_once();probe.assert_not_called()
            self.assertEqual(client.store.list(),[])

    def test_knowledge_model_cannot_force_worker(self):
        planner=Mock(return_value=({'action':'read','device':'local-primary','check':'services'},1));probe=Mock()
        with RunningApp(planner,probe) as client:
            result=client.chat('Python列表是什么')[1]
            self.assertEqual(result['status'],'incomplete');probe.assert_not_called()

    def test_foreign_browser_cannot_read_cancel_or_resume_task(self):
        with RunningApp() as client:
            task=client.diagnostic_chat()[1]['task'];other=client.new_browser();client.call('/api/session',browser=other)
            for path,data in [('/api/tasks/'+task['id'],None),('/api/tasks/'+task['id']+'/cancel',{}),
                              ('/api/tasks/'+task['id']+'/resume',{'allow_context':True,'allow_live_data':True})]:
                self.assertEqual(client.call(path,data,browser=other)[0],404)
            self.assertEqual(client.call('/api/tasks',browser=other)[1]['tasks'],[])

    def test_admin_approval_cannot_expand_scope_from_request_body(self):
        with RunningApp() as client:
            task=client.diagnostic_chat()[1]['task'];client.login()
            self.assertEqual(client.call('/api/admin/tasks/'+task['id']+'/decision',
                {'approved':True,'scope':{'remote-primary':['routes']}})[0],410)
            self.assertEqual(client.store.get(task['id'])['status'],'pending')

    def test_legacy_diagnose_endpoint_is_closed(self):
        with RunningApp() as client,patch('devices.diagnostic_web.diagnose') as legacy,patch('legacy.unified_chat.remote_probe') as probe:
            self.assertEqual(client.call('/api/diagnose',{'question':'检查','devices':['local-primary'],'allow_live_data':True})[0],410)
            legacy.assert_not_called();probe.assert_not_called()

    def test_admin_not_configured_fails_closed(self):
        with RunningApp(configured=False) as client:
            self.assertFalse(client.call('/api/admin/status')[1]['configured'])
            self.assertEqual(client.login()[1]['reason'],'admin_not_configured')
            task=client.diagnostic_chat()[1]['task'];self.assertEqual(client.call('/api/admin/tasks/'+task['id']+'/decision',{'approved':True})[0],410)

    def test_csrf_cross_origin_and_logout(self):
        with RunningApp() as client:
            self.assertEqual(client.call('/api/admin/login',{'password':TEST_PASSWORD},token=False)[0],403)
            self.assertEqual(client.call('/api/admin/login',{'password':TEST_PASSWORD},origin='http://evil.example')[0],403)
            client.login();self.assertTrue(client.call('/api/admin/status')[1]['authenticated'])
            client.call('/api/admin/logout',{});self.assertFalse(client.call('/api/admin/status')[1]['authenticated'])

    def test_password_and_admin_session_expiry(self):
        with RunningApp() as client:
            record=json.loads(client.auth.path.read_text(encoding='utf-8'))
            self.assertNotIn(TEST_PASSWORD,json.dumps(record))
            now=[1000];auth=AdminAuth(client.auth.path,clock=lambda:now[0])
            self.assertEqual(auth.login('wrong')[1],'invalid_credentials')
            token,error=auth.login(TEST_PASSWORD);self.assertIsNone(error);self.assertTrue(auth.valid(token))
            now[0]+=3601;self.assertFalse(auth.valid(token))
            for _ in range(5):auth.login('wrong')
            self.assertEqual(auth.login(TEST_PASSWORD)[1],'login_limited')

    def test_cookie_scope_and_refresh_preserve_task(self):
        with RunningApp() as client:
            task=client.diagnostic_chat()[1]['task']
            with client.opener.open(client.base) as response:
                response.read();self.assertIsNone(response.headers.get('Set-Cookie'))
            self.assertEqual(client.call('/api/tasks')[1]['tasks'][0]['id'],task['id'])


if __name__=='__main__':unittest.main()
