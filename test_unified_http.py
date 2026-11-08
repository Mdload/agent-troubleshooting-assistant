import json
import threading
import unittest
import urllib.request
from unittest.mock import patch,Mock
import app

class UnifiedHTTPTests(unittest.TestCase):
    def test_chat_route_consent_and_automatic_read(self):
        server=app.Server(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(consent):
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/chat',
                data=json.dumps({'question':'检查局端','allow_context':consent,'history':[]}).encode(),
                headers={'Content-Type':'application/json','X-ATA-Token':app.CSRF_TOKEN})
            with urllib.request.urlopen(req) as response:return json.load(response)
        try:
            planner=Mock(side_effect=[({'action':'read','device':'local-primary','check':'services'},1),
                ({'action':'answer','answer':'服务运行，业务状态尚未验证','refs':['OBS-1'],'basis':'evidence'},2)])
            with patch('unified_chat.make_model',return_value=planner) as model, patch('unified_chat.qa.model_config',return_value={}), patch('unified_chat.AuditStore'), patch('unified_chat.remote_probe',return_value={'ok':True,'output':'active'}) as tool:
                self.assertEqual(request(False)['status'],'consent_required');model.assert_not_called();tool.assert_not_called()
                result=request(True);self.assertEqual(result['status'],'answered');self.assertEqual(result['total_tokens'],3)
                tool.assert_called_once_with('local-primary','services')
        finally:server.shutdown();server.server_close();thread.join()
