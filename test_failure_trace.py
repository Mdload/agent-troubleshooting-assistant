"""失败动作留可定位类别，严格拒绝重复/越界，不增加纠正或调用次数。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from chat_model import ModelFailure
from diagnostic_core import AuditStore
from unified_chat import run


class FailureTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.audit=AuditStore(Path(self.temp.name)/'audit.db')
        self.tool=Mock(return_value={'ok':False,'error':'timeout','collected_at':'SIMULATION','output':''})

    def tearDown(self): self.temp.cleanup()

    def execute(self, actions):
        planner=Mock(side_effect=actions)
        result=run('远端主机状态',[],planner,Mock(return_value=[]),self.tool,self.audit)
        return result,planner

    def test_duplicate_failed_read_is_classified_without_retry(self):
        action=({'action':'read','device':'remote-primary','check':'services'},1)
        result,planner=self.execute([action,action])
        self.assertEqual(self.tool.call_count,1)
        self.assertEqual(planner.call_count,2)
        self.assertEqual(result['reason'],'invalid_action_or_citation')
        self.assertEqual(result['events'][-1],{'action':'rejected','reason':'duplicate_read'})
        self.assertIn('合成状态回放',result['answer'])
        self.assertIn('不能据此判断',result['answer'])
        self.assertFalse(any(event['action']=='correction' for event in result['events']))
        self.assertTrue(any(row[1]=='rejected' for row in self.audit.read(result['task'])))

    def test_unregistered_value_is_not_written_to_audit(self):
        result,_=self.execute([({'action':'read','device':'PRIVATE_SENTINEL','check':'services'},1)])
        self.tool.assert_not_called()
        self.assertEqual(result['events'][-1]['reason'],'unregistered_device')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.audit.read(result['task'])))

    def test_unsupported_check_is_classified(self):
        result,_=self.execute([({'action':'read','device':'remote-primary','check':'reboot'},1)])
        self.tool.assert_not_called()
        self.assertEqual(result['events'][-1]['reason'],'unsupported_check')

    def test_malformed_read_is_classified(self):
        result,_=self.execute([({'action':'read','device':'remote-primary','check':'services','command':'PRIVATE_SENTINEL'},1)])
        self.tool.assert_not_called()
        self.assertEqual(result['events'][-1]['reason'],'invalid_read_request')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.audit.read(result['task'])))

    def test_timeout_after_success_preserves_synthetic_scope_in_copied_answer(self):
        self.tool.return_value={'ok':True,'collected_at':'SIMULATION','output':'active'}
        result,_=self.execute([({'action':'read','device':'remote-primary','check':'services'},1),ModelFailure('model_timeout')])
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('合成状态回放',result['answer'])
        self.assertIn('模型响应超时',result['answer'])
        self.assertEqual(result['evidence_scope']['successful_reads'],1)


if __name__=='__main__': unittest.main()
