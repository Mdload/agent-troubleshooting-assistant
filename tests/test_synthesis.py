"""离线验收真实检索和模型协议；模拟响应不等于真实模型质量验收。"""
if not __package__:
    import _bootstrap

import json
import unittest
from unittest.mock import patch
import qa

# 15题覆盖机制、启动链、模式、历史冲突和未确认项。
QUESTIONS = [
    ('远端到局端业务怎么走，Router、Croc和tun16分别负责什么？', ['KB-001', 'KB-002', 'KB-003']),
    ('为什么备机拨号还在，Croc却停止？谁负责启动它们？', ['KB-004', 'KB-008']),
    ('192.0.2.15和192.0.2.25有什么区别？', ['KB-002']),
    ('Router负责运行Croc吗？', ['KB-003']),
    ('局端用哪个接口做Keepalived心跳？', ['KB-010']),
    ('固定主机测试和正常HA有什么区别？', ['KB-006']),
    ('现在ppp5是不是还坏着？', ['HA-007', 'KB-011']),
    ('Croc的算法91具体怎么调度？', ['KB-012']),
    ('能直接运行健康检查脚本作为只读检查吗？', ['KB-007']),
    ('SSH能连140是不是业务就通了？', ['KB-001']),
    ('当前主机是哪台？', ['KB-008']),
    ('远端Croc从rc.local还是systemd启动？', ['KB-009']),
    ('为什么不能直接对齐各台设备日志时间？', ['KB-011']),
    ('交换机完整接线是什么？', ['KB-012']),
    ('数据库连接池耗尽怎么处理？', []),
]

class SynthesisTests(unittest.TestCase):
    def test_question_set(self):
        for question, expected in QUESTIONS:
            with self.subTest(question=question):
                ids = {h['case']['id'] for h in qa.retrieve(question)}
                if expected:
                    self.assertTrue(set(expected) <= ids, (expected, ids))
                else:
                    self.assertEqual(ids, set())

    def test_mode_scope(self):
        hits = qa.retrieve('固定主机测试和正常HA有什么区别？', 'normal_ha')
        self.assertNotIn('KB-006', [h['case']['id'] for h in hits])
        hits = qa.retrieve('Router负责运行Croc吗？', group='router')
        self.assertNotIn('KB-004', [h['case']['id'] for h in hits])

    def test_model_request_and_multi_source_answer(self):
        question = QUESTIONS[0][0]
        response = {
            'summary': [{'text': '业务从远端SMR经中间网络到局端SAR，再进入tun16。', 'refs': ['KB-001', 'KB-002']}],
            'reasoning': [{'text': 'Router负责转发及七VIP同步，不终止Croc。', 'refs': ['KB-003']}],
            'checks': [],
            'limits': [{'text': '这描述功能关系，尚未验收完整物理接线和业务传输。', 'refs': ['KB-001']}],
        }
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'choices': [{'message': {'content': json.dumps(response)}}],
                                   'usage': {'total_tokens': 100}}).encode()
        cfg = dict(LLM_API_KEY='TEST-ONLY', LLM_MODEL_ID='fake', LLM_BASE_URL='https://api.inferera.com/v1')
        with patch('qa.model_config', return_value=cfg), patch('qa.urllib.request.urlopen', return_value=FakeResponse()) as call:
            result = qa.answer(question, allow_new_topology=True)
        self.assertEqual(result['kind'], 'model')
        self.assertIn('[KB-003]', result['answer'])
        self.assertEqual(len(result['sections']), 4)
        self.assertFalse(result['device_executed'])
        payload = json.loads(call.call_args[0][0].data.decode())
        evidence = json.loads(payload['messages'][1]['content'])['evidence']
        self.assertTrue({'KB-001', 'KB-002', 'KB-003'} <= {c['id'] for c in evidence})

    def test_reject_missing_and_invented_citations(self):
        hits = qa.retrieve(QUESTIONS[0][0])
        for refs in [[], ['KB-999'], ['HA-999']]:
            content = {k: [{'text': 'unsupported', 'refs': refs}] for k in ('summary', 'reasoning', 'limits')}
            content['checks'] = []
            with self.assertRaises(ValueError):
                qa.parse_synthesis(json.dumps(content), hits)

    def test_failure_keeps_multiple_sources(self):
        with patch('qa.ask_model', side_effect=ValueError('SECRET-SENTINEL')):
            result = qa.answer(QUESTIONS[0][0], allow_new_topology=True)
        self.assertEqual(result['kind'], 'extract')
        self.assertIn('[KB-001]', result['answer'])
        self.assertIn('[KB-003]', result['answer'])
        self.assertIn('未完成回答', result['answer'])
        self.assertNotIn('SECRET-SENTINEL', str(result))

    def test_sources_and_snapshot_dates(self):
        self.assertEqual(len({c['id'] for c in qa.RECORDS}), len(qa.RECORDS))
        for c in qa.KNOWLEDGE:
            if c['kind'] == 'lab_basics':
                self.assertIsNone(c['evidence_date'])
                self.assertTrue(c['reference_urls'])
            else:
                self.assertIn(c['evidence_date'], ('2026-09-28','2026-09-30'))
            self.assertTrue(set(c['sources']) <= set(qa.SOURCE_MAP))

    def test_fenced_json_and_truncated_json(self):
        hits = qa.retrieve(QUESTIONS[0][0])
        content = {k: [{'text': 'supported', 'refs': ['KB-001']}] for k in ('summary', 'reasoning', 'limits')}
        content['checks'] = []
        encoded = json.dumps(content)
        self.assertEqual(qa.parse_synthesis(encoded, hits),
                         qa.parse_synthesis('```json\n' + encoded + '\n```', hits))
        with self.assertRaises(ValueError):
            qa.parse_synthesis(encoded[:-10], hits)

    def test_truncation_reason_and_usage_are_preserved(self):
        class TruncatedResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'choices': [{'message': {'content': 'SECRET-SENTINEL'},
                    'finish_reason': 'length'}], 'usage': {'total_tokens': 4500}}).encode()
        cfg = dict(LLM_API_KEY='TEST-ONLY', LLM_MODEL_ID='fake', LLM_BASE_URL='https://api.inferera.com/v1')
        with patch('qa.model_config', return_value=cfg), patch('qa.urllib.request.urlopen', return_value=TruncatedResponse()):
            result = qa.answer(QUESTIONS[0][0], allow_new_topology=True)
        self.assertEqual(result['kind'], 'extract')
        self.assertEqual(result['error_code'], 'output_truncated')
        self.assertEqual(result['total_tokens'], 4500)
        self.assertNotIn('SECRET-SENTINEL', str(result))

if __name__ == '__main__':
    unittest.main()
