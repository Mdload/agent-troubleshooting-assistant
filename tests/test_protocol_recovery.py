"""格式错误可有限纠正，权限/预算边界保持关闭，离线不接模型或设备。"""
if not __package__:
    import _bootstrap

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from chat_model import ModelFailure, make_model
from diagnostic_core import AuditStore
from unified_chat import run, search

def answer(refs=None, text='规则解释', basis='general', usage=5):
    return {'action':'answer','answer':text,'refs':refs or [],'basis':basis},usage

class ProtocolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.audit=AuditStore(Path(self.temp.name)/'audit.db')
        self.lookup=Mock(return_value=[{'id':'KB-013','title':'局端规则'}])
        self.probe=Mock(return_value={'ok':True,'output':'active'})

    def tearDown(self): self.temp.cleanup()

    def execute(self, actions, question='局端规则', clock=None):
        self.planner=Mock(side_effect=actions)
        kwargs={'clock':clock} if clock is not None else {}
        return run(question,[],self.planner,self.lookup,self.probe,self.audit,**kwargs)

    def test_bad_reference_corrected_with_existing_sources(self):
        result=self.execute([answer(['KB-999'],text='PRIVATE INVALID ANSWER',basis='evidence'),
                             answer(['KB-013','KB-013'],basis='evidence')])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['refs'],['KB-013'])
        self.assertEqual(result['total_tokens'],10)
        self.lookup.assert_called_once_with('局端规则');self.probe.assert_not_called()
        context=self.planner.call_args_list[1][0][0]
        self.assertEqual(context['correction_feedback']['valid_refs'],['KB-013'])
        self.assertEqual(context['remaining_corrections'],0)
        self.assertNotIn('PRIVATE',json.dumps(context))
        self.assertNotIn('PRIVATE',json.dumps(self.audit.read(result['task'])))

    def test_bad_json_then_answer_preserves_failed_usage(self):
        result=self.execute([ModelFailure('model_invalid_json',7),answer()],question='普通问题')
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['total_tokens'],12)
        self.assertTrue(result['total_tokens_complete'])
        self.probe.assert_not_called()

    def test_correction_failure_stops_after_two_calls(self):
        result=self.execute([ModelFailure('model_invalid_json',7),
                             ModelFailure('model_invalid_json',11),answer()])
        self.assertEqual(result['reason'],'model_invalid_json')
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(result['total_tokens'],18)
        self.assertEqual([e['action'] for e in result['events']].count('correction'),1)

    def test_inline_reference_must_match_declared_refs(self):
        result=self.execute([answer(['KB-013'],text='规则 [KB-999]',basis='evidence'),
                             answer(['KB-013'],text='规则 [KB-013]',basis='evidence')])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['events'][-1]['reason'],'invalid_citation')

    def test_malformed_action_recovers(self):
        result=self.execute([([],2),answer()])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['events'][-1]['reason'],'invalid_action_format')

    def test_unknown_target_fails_without_retry(self):
        result=self.execute([({'action':'read','device':'unregistered-device','check':'services'},2),answer()])
        self.assertEqual(result['reason'],'invalid_action_or_citation')
        self.assertEqual(self.planner.call_count,1)
        self.probe.assert_not_called()
        self.assertFalse(any(e['action']=='correction' for e in result['events']))

    def test_failed_observation_cannot_become_valid_reference(self):
        self.probe.return_value={'ok':False,'error':'timeout'}
        result=self.execute([({'action':'read','device':'local-primary','check':'services'},2),
                             answer(['OBS-1'],basis='evidence'),answer(basis='limited')])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['basis'],'limited')
        self.assertNotIn('OBS-1',self.planner.call_args_list[2][0][0]['correction_feedback']['valid_refs'])
        self.assertEqual(self.probe.call_count,1)

    def test_transport_failures_do_not_retry(self):
        for code in ('model_timeout','model_rate_limited','model_connection_failed','model_http_error','model_output_truncated'):
            result=self.execute([ModelFailure(code,7),answer()])
            self.assertEqual(result['reason'],code)
            self.assertEqual(self.planner.call_count,1)

    def test_expired_deadline_does_not_start_correction(self):
        result=self.execute([ModelFailure('model_invalid_json',7),answer()],
                            clock=Mock(side_effect=[0,0,181]))
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(self.planner.call_count,1)

    def test_optional_usage_does_not_block_valid_content(self):
        for usage_data in (None,[], 'unknown', {'total_tokens':True}):
            response=Mock();response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            response.read.return_value=json.dumps({'usage':usage_data,
                'choices':[{'message':{'content':json.dumps(answer()[0])},'finish_reason':'stop'}]}).encode()
            with patch('chat_model.urllib.request.build_opener') as factory:
                factory.return_value.open.return_value=response
                planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
                                    'LLM_API_KEY':'test','LLM_MODEL_ID':'test'})
                result=run('普通问题',[],planner,self.lookup,self.probe,self.audit)
            self.assertEqual(result['status'],'answered')
            self.assertFalse(result['total_tokens_complete'])
            self.assertEqual(factory.return_value.open.call_count,1)

    def test_source_scope_and_history_flags_reach_planner(self):
        # 比较模式的问题需要两个来源，同时保留各自范围，不能只给正文。
        sources=search('正常HA和固定主机测试模式有什么不同？')
        fixed=next(s for s in sources if s['id']=='KB-006')
        self.assertEqual(fixed['operating_modes'],['fixed_primary_test'])
        self.assertEqual(fixed['kind'],'topology')
        old=search('远端ppp5以前拨号失败了吗？')
        case=next(s for s in old if s['id'].startswith('HA-'))
        self.assertEqual(case['kind'],'historical_case')
        self.assertFalse(case['live_verified'])
        self.lookup.return_value=sources
        result=self.execute([answer(['KB-006'],basis='evidence')])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(self.planner.call_args[0][0]['sources'],sources)

if __name__=='__main__': unittest.main()
