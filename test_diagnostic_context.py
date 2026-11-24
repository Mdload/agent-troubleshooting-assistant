from chat_model import ANSWER_REASONING_POLICY
"""诊断投影和因果边界回归；禁止真实模型或设备访问。"""
import copy
import json
import unittest
from unittest.mock import Mock
from chat_model import make_model, PROMPT, KNOWLEDGE_PROMPT
from diagnostic_context import model_view, compact_logs, causal_issues, DIAGNOSTIC_PROMPT
from workflow_test_support import RunningApp
from ha_case_test_support import case_probe


class DiagnosticContextTests(unittest.TestCase):
    def test_service_evidence_does_not_prove_unattempted_ping(self):
        services = [{'id': 'OBS-1', 'ok': True, 'check': 'services'}]
        self.assertEqual(causal_issues('本次 ping 通了。[OBS-1]', services)[0]['id'], 'icmp_measurement_required')
        self.assertFalse(causal_issues('目前无法判断是否 ping 通。', services))

    def test_approved_ha_basics_are_read_before_first_model_call(self):
        calls=[]
        def planner(context):
            calls.append(context)
            self.assertEqual([o['check'] for o in context['observations']],['services','interfaces'])
            self.assertEqual(context['diagnostic_case']['nodes'][0]['finding'],'non_owner_stop_expected')
            return {'action':'answer','answer':'按正常HA假设，无VIP且Croc服务停止符合局部预期；模式、对端和业务仍未核实。',
                    'refs':['OBS-1','OBS-2'],'basis':'evidence'},1
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[],prefetch_overview=True) as client:
            task=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task']
            self.assertEqual(client.resume(task['id'])[0],409);self.assertEqual(calls,[])
            client.login();client.approve(task['id']);result=client.resume(task['id'])[1]
            self.assertEqual(result['status'],'completed');self.assertEqual(len(calls),1)
            self.assertEqual(result['attempts'],2);self.assertEqual(len(result['result']['model_trace']),1)
            self.assertEqual(result['result']['events'][1]['action'],'host_plan')

    def test_failed_required_state_returns_partial_evidence_without_model_or_extra_reads(self):
        planner=Mock(side_effect=AssertionError('No model needed for an unavailable required state'))
        probe=Mock(side_effect=case_probe('read_failure'))
        with RunningApp(planner,probe,lookup=lambda q:[],prefetch_overview=True) as client:
            task=client.diagnostic_chat('局端备机当前Croc业务不通')[1]['task']
            client.login();client.approve(task['id']);result=client.resume(task['id'])[1]['result']
            self.assertEqual(result['status'],'incomplete');self.assertEqual(result['reason'],'required_state_unavailable')
            self.assertEqual(len(result['evidence']),2);self.assertEqual(result['refs'],['OBS-2'])
            self.assertEqual(result['total_tokens'],0);self.assertTrue(result['total_tokens_complete'])
            self.assertEqual(probe.call_count,2);planner.assert_not_called()
            self.assertIn('不能判断',result['answer'])

    def test_projection_retains_scope_dates_conditions_refs_and_original_evidence(self):
        source={'id':'KB-016','title':'HA规则','kind':'topology','evidence_date':'2026-09-28',
                'operating_modes':['normal_ha'],'historical_finding':'只有正常HA且未持VIP时停止才符合局部预期。',
                'verification_limits':'非现场快照','knowledge_coverage':{'redundant':'metadata'}}
        context={'workflow_policy':{'mode':'diagnostic'},'sources':[source],
                 'tool_catalog':{'local-backup':{'checks':{'services':'状态'}}},
                 'observations':[],'correction_feedback':{'code':'current_evidence_required'},
                 'remaining_reads':1,'remaining_corrections':0}
        original=copy.deepcopy(context);view=model_view(context)
        self.assertEqual(context,original)
        self.assertLess(len(json.dumps(view)),len(json.dumps(context)))
        for field in ('id','historical_finding','operating_modes','evidence_date','verification_limits'):
            self.assertEqual(view['sources'][0][field],source[field])
        for field in ('tool_catalog','correction_feedback','remaining_reads','remaining_corrections'):
            self.assertEqual(view[field],context[field])
        self.assertIs(model_view({'workflow_policy':{'mode':'knowledge'}}).get('sources'),None)

    def test_repeated_logs_retain_event_positions_times_units_and_original(self):
        message='W1013 22:48:24.538250 350452 SarPktProcess.cpp:430] '+('invalid IP length; '*10)
        lines=['LOG_RECORDS_RETURNED=7']
        for index in range(7):
            lines.append('[unit=croc-sar.service device_time_us={}] {}'.format(100+index,message))
            if index==2: lines.append('[unit=keepalived.service device_time_us=102] Entering BACKUP STATE')
        raw='\n'.join(lines);compact=compact_logs(raw)
        self.assertLess(len(compact),len(raw))
        for field in ('count=7','first_device_time_us=100','last_device_time_us=106',
                      'positions=0,1,2,4,5,6,7','Entering BACKUP STATE','不表示连续'):
            self.assertIn(field,compact)
        context={'workflow_policy':{'mode':'diagnostic'},'sources':[],
                 'observations':[{'id':'OBS-3','check':'logs','output':raw,'evidence_role':'event_history'}]}
        view=model_view(context)
        self.assertEqual(context['observations'][0]['output'],raw)
        self.assertEqual(view['observations'][0]['id'],'OBS-3')
        self.assertIn('model_projection',view['observations'][0])
        self.assertEqual(compact_logs('unknown\nformat'),'unknown\nformat')

    def test_diagnostic_transport_uses_short_prompt_and_projected_context_only(self):
        opener=Mock();response=Mock()
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'action':'answer','answer':'未知','refs':[],'basis':'limited'})}}]}).encode()
        opener.open.return_value=response
        planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test-placeholder','LLM_MODEL_ID':'fixture'},opener)
        planner({'workflow_policy':{'mode':'diagnostic'},'sources':[],'observations':[]})
        payload=json.loads(opener.open.call_args[0][0].data.decode())
        self.assertEqual(payload['messages'][0]['content'],DIAGNOSTIC_PROMPT+'\n'+ANSWER_REASONING_POLICY)
        self.assertLess(len(DIAGNOSTIC_PROMPT.encode()),len(PROMPT.encode())*.6)
        planner({'workflow_policy':{'mode':'knowledge'}})
        self.assertEqual(json.loads(opener.open.call_args[0][0].data.decode())['messages'][0]['content'],KNOWLEDGE_PROMPT+'\n'+ANSWER_REASONING_POLICY)
        planner({})
        self.assertEqual(json.loads(opener.open.call_args[0][0].data.decode())['messages'][0]['content'],PROMPT+'\n'+ANSWER_REASONING_POLICY)

    def test_call_stack_is_not_a_proven_cause_but_qualified_explanations_are_allowed(self):
        observations=[{'id':'OBS-3','ok':True,'check':'logs'}]
        for text in ['信号处理内thread join触发abort。','根因就是thread join。',
                     'Result=success说明是干净停止而非崩溃。']:
            self.assertTrue(causal_issues(text,observations),text)
        for text in ['thread join可能触发abort，仍需源码与退出时序核验。',
                     '不能确定thread join导致SIGABRT。','thread join触发abort吗？',
                     '日志有SIGABRT及join栈帧，只是线索，根因未知。',
                     '错误示例：thread join触发abort。','示例 `thread join触发abort`。']:
            self.assertEqual(causal_issues(text,observations),[],text)

    def test_plain_status_gets_ha_guidance_without_expanding_approved_scope(self):
        def planner(context):
            if not context['observations']:
                return {'action':'read_overview','device':'local-backup'},1
            self.assertEqual(context['diagnostic_case']['nodes'][0]['finding'],'non_owner_stop_expected')
            self.assertEqual(context['diagnostic_case']['next_checks'],[])
            return {'action':'answer','answer':'按正常HA假设，无VIP且Croc服务停止符合非持有节点的局部预期；模式与业务仍未核实。',
                    'refs':['OBS-1','OBS-2'],'basis':'evidence'},1
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task']
            self.assertEqual(task['scope'],{'local-backup':['services','interfaces']})
            client.login();client.approve(task['id']);result=client.resume(task['id'])[1]
            self.assertEqual(result['status'],'completed');self.assertEqual(result['attempts'],2)

    def test_causal_guard_also_covers_non_ha_scene_with_shared_one_correction(self):
        bad=({'action':'answer','answer':'thread join触发abort。','refs':['OBS-1','OBS-2'],'basis':'evidence'},1)
        good=(dict(bad[0],answer='日志线索不能确认代码根因；当前Croc服务停止，模式、触发者和业务仍未核实。'),1)
        planner=Mock(side_effect=[({'action':'read_overview','device':'local-backup'},1),bad,good])
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[]) as client:
            client.enter_diagnostic(scene='croc_local')
            task=client.chat('局端备机当前Croc服务和VIP')[1]['task']
            client.login();client.approve(task['id']);result=client.resume(task['id'])[1]['result']
            self.assertEqual(result['status'],'answered');self.assertEqual(len(result['evidence']),2)
            self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)
            self.assertEqual(planner.call_args[0][0]['correction_feedback']['constraints'][0]['id'],'diagnostic_stop_cause')


if __name__=='__main__': unittest.main()
