if not __package__:
    import _bootstrap

import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from html.parser import HTMLParser
from unittest.mock import patch
import app
import diagnostic_web
from tests.workflow_test_support import RunningApp

class DiagnosticWebTests(unittest.TestCase):
    def setUp(self):
        self.client=RunningApp().__enter__();self.base=self.client.base
        # 测试目标是当前进程创建的回环服务，不经过系统HTTP代理。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.client.__exit__()

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
            data['devices']=['198.51.100.46']
            self.assertEqual(self.request(data)[0],410)
            run.assert_not_called()

    def test_legacy_consent_cannot_bypass_approval(self):
        with patch('diagnostic_web.make_planner') as model, patch('diagnostic_web.remote_probe') as tool:
            code, result=self.request({'question':'检查', 'devices':['local-primary']})
            self.assertEqual(code,410); self.assertEqual(result['status'],'approval_required')
            model.assert_not_called(); tool.assert_not_called()

    def test_page_defaults_to_model_and_keeps_material_permission_separate(self):
        with self.opener.open(self.base) as response: page=response.read().decode()
        inputs=[]
        parser=HTMLParser()
        parser.handle_starttag=lambda tag,attrs:inputs.append(dict(attrs)) if tag=='input' else None
        parser.feed(page)
        self.assertFalse(any(item.get('id')=='allow-context' for item in inputs))
        material=next(item for item in inputs if item.get('id')=='allow-materials')
        self.assertEqual(material.get('type'),'checkbox');self.assertNotIn('checked',material)
        self.assertIn('allow_context:true',page)
        self.assertIn(app.CSRF_TOKEN,page)

    @unittest.skipIf(os.name=='nt','Unix worker only')
    def test_worker_rejects_arbitrary_command(self):
        import device_worker
        with patch('device_worker.probe') as tool:
            for data in ({'device':'local-primary','check':'reboot'},
                         {'device':'local-primary','check':'services','command':'id'},
                         {'device':'198.51.100.46','check':'services'}):
                with self.assertRaises(ValueError): device_worker.dispatch(data)
            tool.assert_not_called()

if __name__=='__main__': unittest.main()
