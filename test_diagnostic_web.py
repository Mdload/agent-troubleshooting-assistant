import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import app
import diagnostic_web

class DiagnosticWebTests(unittest.TestCase):
    def setUp(self):
        self.server = app.Server(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        # 测试目标是当前进程创建的回环服务，不经过系统HTTP代理。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, data, token=True, host=None):
        headers = {'Content-Type': 'application/json'}
        if token: headers['X-ATA-Token'] = app.CSRF_TOKEN
        if host: headers['Host'] = host
        request = urllib.request.Request(self.base+'/api/diagnose', data=json.dumps(data).encode(), headers=headers)
        try:
            with self.opener.open(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_csrf_and_scope_before_execution(self):
        data = {'question':'检查', 'devices':['local-primary'], 'allow_live_data':True}
        with patch('diagnostic_web.diagnose') as run:
            self.assertEqual(self.request(data, token=False)[0],403)
            self.assertEqual(self.request(data, host='evil.example')[0],403)
            data['devices']=['203.0.113.250']
            self.assertEqual(self.request(data)[0],400)
            run.assert_not_called()

    def test_no_consent_has_no_model_or_tool_call(self):
        with patch('diagnostic_web.make_planner') as model, patch('diagnostic_web.remote_probe') as tool:
            code, result=self.request({'question':'检查', 'devices':['local-primary']})
            self.assertEqual(code,200); self.assertEqual(result['status'],'consent_required')
            model.assert_not_called(); tool.assert_not_called()

    def test_page_has_unchecked_live_consent(self):
        with self.opener.open(self.base) as response: page=response.read().decode()
        self.assertIn('自动只读排查',page)
        self.assertIn(app.CSRF_TOKEN,page)
        self.assertNotIn('type="checkbox" id="allow-context" checked',page)

    @unittest.skipIf(os.name=='nt','Unix worker only')
    def test_worker_rejects_arbitrary_command(self):
        import device_worker
        with patch('device_worker.probe') as tool:
            for data in ({'device':'local-primary','check':'reboot'},
                         {'device':'local-primary','check':'services','command':'id'},
                         {'device':'203.0.113.250','check':'services'}):
                with self.assertRaises(ValueError): device_worker.dispatch(data)
            tool.assert_not_called()

if __name__=='__main__': unittest.main()
