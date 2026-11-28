if not __package__:
    import _bootstrap

from chat_model import ANSWER_REASONING_POLICY
"""回答可读性、工具规划视图及运输指标的离线行为验证。"""
import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from chat_model import make_model, select_prompt, PROMPT, KNOWLEDGE_PROMPT, LEARNING_PROMPT, ModelFailure
from device_tools import tool_catalog
from diagnostic_core import AuditStore
from evaluation.ha_model import FixtureResponse, isolated_io
from evaluation.representative import run_case, fixture_probe
from mechanism_guard import contradicted_constraints, observation_constraints
from unified_chat import available_catalog, run
from tests.workflow_test_support import RunningApp
from knowledge_coverage import assess
import qa


def answer(text='一般原理说明。'):
    return {'action':'answer','answer':text,'refs':[],'basis':'general'},1


class PlanningViewTests(unittest.TestCase):
    def test_stale_initial_state_stops_before_model_without_retrying_the_probe(self):
        planner=Mock();probe=Mock(side_effect=fixture_probe('stale'))
        with RunningApp(planner,probe,lookup=lambda q:[],prefetch_overview=True) as client:
            task=client.diagnostic_chat('远端主机当前Croc服务与VIP')[1]['task']
            client.login();client.approve(task['id'])
            result=client.resume(task['id'])[1]['result']
        self.assertEqual(result['reason'],'required_state_unavailable')
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(len(result['evidence']),2)
        self.assertEqual(result['refs'],[])
        self.assertEqual(result['model_trace'],[])
        self.assertEqual(probe.call_count,2)
        planner.assert_not_called()

    def test_failed_or_stale_reads_are_not_offered_again_and_original_is_unchanged(self):
        original=tool_catalog(['remote-primary'])
        snapshot=copy.deepcopy(original)
        attempted=[{'device':'remote-primary','check':'services','ok':False},
                   {'device':'remote-primary','check':'interfaces','ok':True,'freshness':'stale'}]
        view=available_catalog(original,attempted,2)
        self.assertEqual(set(view['remote-primary']['checks']),set(snapshot['remote-primary']['checks'])-{'services','interfaces'})
        self.assertNotIn('overview',view['remote-primary'])
        self.assertEqual(original,snapshot)
        self.assertEqual(view['remote-primary']['registered_management_interface'],'tun7')

    def test_planning_view_respects_scope_and_remaining_read_cost(self):
        scope={'remote-primary':copy.deepcopy(tool_catalog()['remote-primary'])}
        scope['remote-primary']['checks'].pop('logs')
        self.assertNotIn('logs',available_catalog(scope,[],3)['remote-primary']['checks'])
        self.assertNotIn('overview',available_catalog(scope,[],1)['remote-primary'])
        self.assertEqual(available_catalog(scope,[],0),{})

    def test_host_context_removes_failed_read_but_still_rejects_requested_duplicate(self):
        action=({'action':'read','device':'remote-primary','check':'services'},1)
        planner=Mock(side_effect=[action,action])
        probe=Mock(return_value={'ok':False,'collected_at':'SIMULATION','error':'timeout','output':''})
        with tempfile.TemporaryDirectory() as directory:
            result=run('远端主机服务状态',[],planner,lambda q:[],probe,AuditStore(Path(directory)/'a.db'),
                permitted_checks={'remote-primary':['services','interfaces']})
        second=planner.call_args_list[1][0][0]
        self.assertEqual(set(second['tool_catalog']['remote-primary']['checks']),{'interfaces'})
        self.assertEqual(second['attempted_checks'][0]['ok'],False)
        self.assertEqual(result['events'][-1]['reason'],'duplicate_read')
        self.assertEqual(probe.call_count,1)
        self.assertEqual(planner.call_count,2)


class LearningResponseTests(unittest.TestCase):
    def test_learning_transport_uses_smaller_prompt_and_lab_rules_remain_separate(self):
        content=json.dumps(answer()[0],ensure_ascii=False)
        response=FixtureResponse({'usage':{'total_tokens':3},'choices':[
            {'message':{'content':content},'finish_reason':'stop'}]})
        opener=Mock();opener.open.return_value=response
        planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        planner({'workflow_policy':{'mode':'knowledge','domain':'basics'}})
        payload=json.loads(opener.open.call_args[0][0].data)
        self.assertEqual(payload['messages'][0]['content'],LEARNING_PROMPT+'\n'+ANSWER_REASONING_POLICY)
        self.assertLess(len(LEARNING_PROMPT.encode()),len(PROMPT.encode())/2)
        self.assertEqual(planner.last_metrics['prompt_kind'],'general_learning')
        self.assertEqual(select_prompt({'workflow_policy':{'mode':'knowledge','domain':'topology'}})[0],KNOWLEDGE_PROMPT+'\n'+ANSWER_REASONING_POLICY)
        self.assertEqual(select_prompt({'workflow_policy':{'mode':'diagnostic','domain':'basics'}})[1],'diagnostic')

    def test_knowledge_transport_uses_compact_rules_with_same_output_and_time_limits(self):
        content=json.dumps(answer()[0],ensure_ascii=False)
        response=FixtureResponse({'usage':{'total_tokens':3},'choices':[
            {'message':{'content':content},'finish_reason':'stop'}]})
        opener=Mock();opener.open.return_value=response
        planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        planner({'workflow_policy':{'mode':'knowledge','domain':'topology'}})
        payload=json.loads(opener.open.call_args[0][0].data)
        self.assertEqual(payload['messages'][0]['content'],KNOWLEDGE_PROMPT+'\n'+ANSWER_REASONING_POLICY)
        self.assertEqual(planner.last_metrics['prompt_kind'],'lab_knowledge')
        self.assertLess(len(KNOWLEDGE_PROMPT.encode()),len(PROMPT.encode())/2)
        self.assertEqual(payload['max_tokens'],4096)
        self.assertEqual(opener.open.call_args[1]['timeout'],65)
        self.assertEqual(opener.open.call_count,1)

    def test_general_learning_keeps_coverage_in_details_without_unrelated_lab_prefix(self):
        planner=Mock(return_value=answer('进程存在不能单独证明服务健康。'))
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            result=client.chat('Linux进程是否能证明服务健康')[1]
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['answer'],'进程存在不能单独证明服务健康。')
        self.assertEqual(result['knowledge_status'],'missing')
        self.assertEqual(result['basis'],'limited')
        self.assertEqual(planner.call_count,1)

    def test_private_question_still_shows_missing_lab_basis_and_has_no_device_tools(self):
        planner=Mock(return_value=answer('具体服务端口未知，可从配置核对。'))
        probe=Mock()
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            result=client.chat('我们的新服务监听什么端口')[1]
        self.assertEqual(result['status'],'answered')
        self.assertIn('监听端口还不确定',result['answer'])
        self.assertNotIn('资料库',result['answer'])
        self.assertEqual(planner.call_args[0][0]['tool_catalog'],{})
        probe.assert_not_called()

    def test_demoted_named_side_uses_ha_mechanism_not_unassessed(self):
        question='局端节点降权之后，是否就恢复业务？'
        coverage=assess(question,qa.KNOWLEDGE)
        self.assertEqual(coverage['status'],'supported')
        self.assertIn('KB-013',coverage['supporting_refs'])
        self.assertFalse(coverage['current_state_supported'])
        self.assertEqual(assess('降权以后会怎样',qa.KNOWLEDGE)['status'],'unassessed')

    def test_negating_full_tunnel_health_does_not_trigger_a_correction(self):
        rules=observation_constraints([{'id':'OBS-1','device':'remote-primary','check':'services','ok':True}])
        for text in ('服务active不证明Croc进程或隧道整体正常。',
                     '接口标志不足以证明Croc隧道整体正常。'):
            self.assertEqual(contradicted_constraints(text,rules),[])
        self.assertTrue(contradicted_constraints('Croc隧道整体正常。',rules))
        self.assertTrue(contradicted_constraints('接口标志不证明业务健康，但Croc隧道整体正常。',rules))


class EvaluationTransportTests(unittest.TestCase):
    def test_dns_failure_is_explained_without_lab_notice_private_error_or_retries(self):
        import urllib.error
        opener=Mock();opener.open.side_effect=urllib.error.URLError(socket.gaierror('PRIVATE_DNS_DETAIL'))
        adapter=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        probe=Mock()
        with tempfile.TemporaryDirectory() as directory:
            result=run('工具调用超时怎么办',[],adapter,lambda q:[],probe,
                AuditStore(Path(directory)/'a.db'),permitted_checks={},
                question_policy={'mode':'knowledge','domain':'general'})
        self.assertEqual(result['reason'],'model_connection_failed')
        self.assertIn('域名解析失败',result['answer'])
        self.assertNotIn('实验室资料',result['answer'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertFalse(result['total_tokens_complete'])
        self.assertEqual(opener.open.call_count,1)
        probe.assert_not_called()

    def test_truncation_preserves_optional_usage_breakdown_without_response_text(self):
        response=FixtureResponse({'usage':{'total_tokens':100,'prompt_tokens':40,'completion_tokens':60,
            'completion_tokens_details':{'reasoning_tokens':55}},
            'choices':[{'finish_reason':'length','message':{'content':'PRIVATE_RESPONSE_TEXT'}}]})
        opener=Mock();opener.open.return_value=response
        adapter=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        with self.assertRaises(ModelFailure) as failure:adapter({})
        self.assertEqual(failure.exception.code,'model_output_truncated')
        self.assertEqual(failure.exception.usage,100)
        self.assertEqual(adapter.last_metrics['finish_reason'],'length')
        self.assertEqual(adapter.last_metrics['reasoning_tokens'],55)
        self.assertEqual(adapter.last_metrics['completion_tokens'],60)
        from model_trace import safe_metrics
        recorded=safe_metrics(adapter.last_metrics)
        self.assertEqual(recorded['prompt_tokens'],40)
        self.assertEqual(recorded['response_content_bytes'],21)
        self.assertNotIn('PRIVATE',json.dumps(recorded))
        self.assertEqual(opener.open.call_count,1)

    def test_missing_or_invalid_usage_details_remain_unknown(self):
        response=FixtureResponse({'usage':{'completion_tokens':True,
            'prompt_tokens':-1,'completion_tokens_details':{'reasoning_tokens':'PRIVATE'}},
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer()[0])}}]})
        opener=Mock();opener.open.return_value=response
        adapter=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        action,usage=adapter({})
        self.assertIsNone(usage)
        for field in ('prompt_tokens','completion_tokens','reasoning_tokens'):
            self.assertNotIn(field,adapter.last_metrics)

    def test_evaluation_wrapper_retains_adapter_stage_on_timeout_without_private_text(self):
        opener=Mock();opener.open.side_effect=socket.timeout('PRIVATE_TRANSPORT_DETAIL')
        adapter=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
            'LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'},opener)
        case={'id':'DEV-TIMEOUT','question':'Python字典是什么','history':[],
              'mode':'knowledge','required_points':[]}
        with isolated_io() as state:row=run_case(case,[],adapter,state)
        call=row['adapter_calls'][0]
        self.assertEqual(call['transport']['stage'],'awaiting_headers')
        self.assertEqual(call['transport']['failure_kind'],'socket_timeout')
        self.assertEqual(row['result']['model_trace'][0]['transport'],call['transport'])
        self.assertIsNone(call['usage'])
        self.assertFalse(row['result']['total_tokens_complete'])
        self.assertNotIn('PRIVATE',json.dumps(call))
        self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
