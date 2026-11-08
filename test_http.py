"""本机临时端口验证API；无服务器部署和设备连接。"""
import json
import threading
import unittest
import urllib.request
from unittest.mock import patch
import app

class HTTPTests(unittest.TestCase):
    def test_health_page_and_fallback(self):
        server = app.Server(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urllib.request.urlopen(base + '/api/health') as response:
                health = json.load(response)
            self.assertEqual(health['version'], '0.7.0')
            self.assertEqual(health['knowledge_count'], 18)
            with urllib.request.urlopen(base) as response:
                self.assertIn('直接提问，无需选择模式', response.read().decode('utf-8'))
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
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    unittest.main()
