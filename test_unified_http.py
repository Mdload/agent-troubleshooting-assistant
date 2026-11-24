import json
import threading
import unittest
import urllib.request
from unittest.mock import patch,Mock
import app
from workflow_test_support import RunningApp,current_probe_fixture

class UnifiedHTTPTests(unittest.TestCase):
    def test_chat_route_consent_and_approved_read(self):
        with RunningApp(lookup=Mock(return_value=[])) as client:
            planner=Mock(side_effect=[({'action':'read','device':'local-primary','check':'services'},1),
                ({'action':'answer','answer':'服务运行，业务状态尚未验证','refs':['OBS-1'],'basis':'evidence'},2)])
            with patch('unified_chat.make_model',return_value=planner) as model, patch('unified_chat.qa.model_config',return_value={}), patch('unified_chat.remote_probe',side_effect=current_probe_fixture) as tool:
                self.assertEqual(client.chat('检查局端主机',False)[1]['status'],'consent_required')
                task=client.diagnostic_chat('检查局端主机')[1]['task']
                model.assert_not_called();tool.assert_not_called()
                self.assertEqual(client.resume(task['id'])[0],409)
                self.assertEqual(client.login()[0],200);self.assertEqual(client.approve(task['id'])[0],200)
                result=client.resume(task['id'])[1]['result']
                self.assertEqual(result['status'],'answered');self.assertEqual(result['total_tokens'],3)
                self.assertEqual(result['evidence'][0]['freshness'],'current')
                tool.assert_called_once_with('local-primary','services')
