import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from devices.diagnostic_core import AuditStore
from legacy.unified_chat import run
from agent.chat_model import ModelFailure,make_model

class PrefetchTests(unittest.TestCase):
    def test_special_question_gets_sources_before_first_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            lookup=Mock(return_value=[{'id':'KB-015','title':'规则'}])
            planner=Mock(return_value=({'action':'answer','answer':'依据规则回答','refs':['KB-015'],'basis':'evidence'},1))
            r=run('Recv-Q非零就重启吗',[],planner,lookup,Mock(),AuditStore(Path(tmp)/'a.db'))
            self.assertEqual(r['status'],'answered');self.assertEqual(planner.call_args[0][0]['sources'][0]['id'],'KB-015')
    def test_failure_code_preserved_without_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=run('普通问题',[],Mock(side_effect=ModelFailure('model_timeout')),Mock(),Mock(),AuditStore(Path(tmp)/'a.db'))
            self.assertEqual(r['reason'],'model_timeout')
    def test_prefetch_same_search_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lookup=Mock(return_value=[{'id':'KB-015'}]);planner=Mock(side_effect=[({'action':'search','query':'Recv-Q'},0),({'action':'answer','answer':'结论','refs':['KB-015'],'basis':'evidence'},1)])
            r=run('Recv-Q',[],planner,lookup,Mock(),AuditStore(Path(tmp)/'a.db'))
            self.assertEqual(r['status'],'answered');self.assertEqual(lookup.call_count,1)
    def test_invalid_json_sanitized(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=b'PRIVATE BAD RESPONSE'
        with patch('agent.chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=response
            planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test','LLM_MODEL_ID':'test'})
            with self.assertRaises(ModelFailure) as error:planner({})
            self.assertEqual(error.exception.code,'model_invalid_json');self.assertNotIn('PRIVATE',str(error.exception))
