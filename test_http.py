"""本机临时端口验证API；无服务器部署和设备连接。"""
import json
import threading
import unittest
import urllib.request
from html.parser import HTMLParser
from unittest.mock import patch
import app
from version import VERSION
from workflow_test_support import RunningApp

class HTTPTests(unittest.TestCase):
    def test_health_page_and_fallback(self):
        client=RunningApp(configured=False).__enter__()
        base=client.base
        try:
            with urllib.request.urlopen(base + '/api/health') as response:
                health = json.load(response)
            self.assertEqual(health['version'], VERSION)
            self.assertTrue(health['device_approval_required'])
            self.assertEqual(health['knowledge_count'], 48)
            with urllib.request.urlopen(base) as response:
                page=response.read().decode('utf-8')
            fields=[]
            parser=HTMLParser()
            parser.handle_starttag=lambda tag,attrs:fields.append((tag,dict(attrs).get('id'))) if tag in ('textarea','select') else None
            parser.feed(page)
            self.assertIn(('textarea','question'),fields)
            self.assertNotIn(('select','mode'),fields)
            request = urllib.request.Request(base + '/api/ask',
                data=json.dumps({'question': 'Router负责运行Croc吗？', 'allow_new_topology': True}).encode(),
                headers={'Content-Type': 'application/json'})
            with patch('qa.ask_model', side_effect=RuntimeError('private upstream')):
                with urllib.request.urlopen(request) as response:
                    data = json.load(response)
            self.assertEqual(data['kind'], 'extract')
            self.assertIn('[KB-003]', data['answer'])
            self.assertFalse(data['device_executed'])
            self.assertNotIn('private upstream', str(data))
        finally:
            client.__exit__()

if __name__ == '__main__':
    unittest.main()
