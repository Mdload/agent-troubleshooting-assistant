"""测时覆盖响应/超时/解析失败；轨迹不保存正文或凭据。"""
import json
import socket
import ssl
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from chat_model import make_model,ModelFailure
from diagnostic_core import AuditStore
from unified_chat import run
from model_trace import safe_metrics

CONFIG={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'PRIVATE_KEY','LLM_MODEL_ID':'test'}


class ModelTraceTests(unittest.TestCase):
    def response(self,content):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'usage':{'total_tokens':17},'choices':[
            {'message':{'content':content},'finish_reason':'stop'}]}).encode()
        return response

    def test_success_has_sizes_and_stages_without_content(self):
        response=self.response('{"action":"answer","answer":"PRIVATE_ANSWER","refs":[],"basis":"general"}')
        with patch('chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=response
            planner=make_model(CONFIG)
            action,usage=planner({'question':'PRIVATE_QUESTION'})
            self.assertEqual(usage,17)
            self.assertEqual(planner.last_metrics['stage'],'complete')
            self.assertEqual(planner.last_metrics['request_bytes'],len(factory.return_value.open.call_args[0][0].data))
            self.assertEqual(planner.last_metrics['response_bytes'],len(response.read.return_value))
            self.assertNotIn('PRIVATE',json.dumps(planner.last_metrics))

    def test_timeout_before_headers_is_not_retried(self):
        with patch('chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=socket.timeout('PRIVATE_NETWORK')
            planner=make_model(CONFIG)
            with self.assertRaises(ModelFailure): planner({})
            self.assertEqual(factory.return_value.open.call_count,1)
            self.assertEqual(planner.last_metrics['stage'],'awaiting_headers')
            self.assertEqual(planner.last_metrics['outcome'],'model_timeout')
            self.assertNotIn('response_bytes',planner.last_metrics)
            self.assertNotIn('PRIVATE',json.dumps(planner.last_metrics))

    def test_body_timeout_identified_separately(self):
        response=self.response('{}');response.read.side_effect=socket.timeout()
        with patch('chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=response
            planner=make_model(CONFIG)
            with self.assertRaises(ModelFailure): planner({})
            self.assertEqual(planner.last_metrics['stage'],'reading_body')
            self.assertIn('read_body_ms',planner.last_metrics)
            self.assertIn('wait_headers_ms',planner.last_metrics)

    def test_decode_failure_keeps_usage_and_metrics(self):
        with patch('chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=self.response('PRIVATE_NOT_JSON')
            planner=make_model(CONFIG)
            with self.assertRaises(ModelFailure) as error: planner({})
            self.assertEqual(error.exception.usage,17)
            self.assertEqual(planner.last_metrics['stage'],'decoding')
            self.assertEqual(planner.last_metrics['outcome'],'model_invalid_json')
            self.assertNotIn('PRIVATE',json.dumps(planner.last_metrics))

    def test_host_records_failure_after_observation_without_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit=AuditStore(Path(tmp)/'a.db')
            planner=Mock(side_effect=[({'action':'read','device':'remote-primary','check':'services'},9),ModelFailure('model_timeout')])
            result=run('PRIVATE_QUESTION',[],planner,Mock(return_value=[]),
                       Mock(return_value={'ok':True,'output':'PRIVATE_OBSERVATION','collected_at':'SIMULATION'}),audit)
            trace=result['model_trace']
            self.assertEqual(len(trace),2)
            self.assertEqual(trace[0]['action'],'read')
            self.assertEqual(trace[1]['phase'],'after_observations')
            self.assertEqual(trace[1]['outcome'],'model_timeout')
            self.assertNotIn('PRIVATE',json.dumps(trace))
            self.assertEqual(len([r for r in audit.read(result['task']) if r[1]=='model']),2)

    def test_budget_denial_does_not_reuse_previous_transport_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            planner=Mock(side_effect=ModelFailure('evaluation_budget_reached',0))
            planner.last_metrics={'stage':'complete','request_bytes':999,'PRIVATE':'secret'}
            result=run('hello',[],planner,Mock(),Mock(),AuditStore(Path(tmp)/'a.db'))
            self.assertEqual(result['model_trace'][0]['outcome'],'evaluation_budget_reached')
            self.assertNotIn('transport',result['model_trace'][0])

    def test_fixed_network_categories_keep_exception_details_private(self):
        errors=[(socket.gaierror(-2,'PRIVATE_DNS'),'dns'),
                (ssl.SSLError('PRIVATE_TLS'),'tls'),
                (ConnectionRefusedError('PRIVATE_PROXY'),'refused'),
                (ConnectionResetError('PRIVATE_PEER'),'reset')]
        for source,kind in errors:
            for error in (source,urllib.error.URLError(source)):
                with self.subTest(kind=kind,wrapped=isinstance(error,urllib.error.URLError)):
                    opener=Mock();opener.open.side_effect=error
                    planner=make_model(CONFIG,opener)
                    with self.assertRaises(ModelFailure) as raised:planner({})
                    self.assertEqual(raised.exception.code,'model_connection_failed')
                    self.assertEqual(planner.last_metrics['failure_kind'],kind)
                    self.assertEqual(opener.open.call_count,1)
                    self.assertNotIn('PRIVATE',json.dumps(planner.last_metrics))

    def test_metric_filter_rejects_nonfinite_and_untrusted_labels(self):
        metrics=safe_metrics({'elapsed_ms':float('nan'),'response_bytes':float('inf'),
            'request_bytes':True,'wait_headers_ms':-1,'failure_kind':'PRIVATE',
            'stage':'PRIVATE','prompt_kind':'general_learning','http_status':429})
        self.assertEqual(metrics,{'prompt_kind':'general_learning','http_status':429})


if __name__=='__main__': unittest.main()
