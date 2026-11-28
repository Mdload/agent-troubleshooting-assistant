"""失败请求不能冒充零消耗，格式错误和超时不泄露响应内容。"""
if not __package__:
    import _bootstrap

import json,tempfile,unittest
import threading,urllib.request
from pathlib import Path
from unittest.mock import Mock,patch
from diagnostic_core import AuditStore
from unified_chat import run
from chat_model import ModelFailure,make_model
from tests.workflow_test_support import RunningApp

class ResponseAccountingTests(unittest.TestCase):
    def execute(self,actions):
        with tempfile.TemporaryDirectory() as tmp:
            return run('普通问题',[],Mock(side_effect=actions),Mock(return_value=[]),Mock(),AuditStore(Path(tmp)/'a.db'))

    def test_failed_step_counts_returned_usage(self):
        result=self.execute([({'action':'search','query':'信息'},10),ModelFailure('model_output_truncated',17)])
        self.assertEqual(result['total_tokens'],27)
        self.assertTrue(result['total_tokens_complete'])
        self.assertEqual(result['reason'],'model_output_truncated')

    def test_unknown_usage_is_not_zero_consumption(self):
        for action in (ModelFailure('model_timeout'),
                       ({'action':'answer','answer':'答案','refs':[],'basis':'general'},None)):
            result=self.execute([action])
            self.assertFalse(result['total_tokens_complete'])
            self.assertEqual(result['total_tokens'],0)

    def test_usage_survives_invalid_response(self):
        for content,finish,code in [('PRIVATE INVALID JSON','stop','model_invalid_json'),
                                    ('partial','length','model_output_truncated'),
                                    (None,'stop','model_invalid_response')]:
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.return_value=json.dumps({'usage':{'total_tokens':73},
                'choices':[{'message':{'content':content},'finish_reason':finish}]}).encode()
            with patch('chat_model.urllib.request.build_opener') as factory:
                factory.return_value.open.return_value=response
                planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test','LLM_MODEL_ID':'test'})
                with self.assertRaises(ModelFailure) as error: planner({})
            self.assertEqual(error.exception.usage,73)
            self.assertEqual(error.exception.code,code)
            self.assertNotIn('PRIVATE',str(error.exception))

    def test_invalid_usage_not_treated_as_count(self):
        for usage in (True,-1,'73'):
            self.assertIsNone(ModelFailure('test',usage).usage)

    def test_http_returns_failed_call_usage(self):
        with RunningApp(lookup=Mock(return_value=[])) as client:
            for code,usage in (('model_invalid_json',81),('model_timeout',None)):
                with patch('unified_chat.qa.model_config',return_value={}), \
                     patch('unified_chat.make_model',return_value=Mock(side_effect=ModelFailure(code,usage))):
                    result=client.chat('Python列表怎么用')[1]
                self.assertEqual(result['status'],'incomplete')
                self.assertEqual(result['reason'],code)
                self.assertEqual(result['total_tokens'],(usage or 0)*(2 if code=='model_invalid_json' else 1))
                self.assertEqual(result['total_tokens_complete'],usage is not None)


if __name__ == "__main__":
    import unittest

    unittest.main()
