"""问法、连续追问、历史冲突与真实响应格式的离线回归。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import legacy.unified_chat as chat
from agent.chat_model import make_model, ModelFailure
from devices.diagnostic_core import AuditStore


class AnswerQualityTests(unittest.TestCase):
    def test_missing_topic_prefetch(self):
        for question in ('主机优先级110，失败扣20，备机100，谁接管？',
                         '备机tun16 DOWN就说明备机坏了吗？', 'ppp5是不是还坏着？'):
            with self.subTest(question=question):
                self.assertTrue(chat.prefetch_query(question, []))
                self.assertTrue(chat.search(question))
        for question in ('Python元组能当字典键吗？', '秋天为什么叫fall？'):
            self.assertIsNone(chat.prefetch_query(question, []))

    def test_followup_receives_local_rules_before_model(self):
        history=[{'role':'user','content':'局端主机切换规则是什么？'},
                 {'role':'assistant','content':'OLD_OBSERVATION'}]
        lookup=Mock(wraps=chat.search)
        planner=Mock(return_value=({'action':'answer','answer':'依据资料解释',
                                   'refs':['KB-013'],'basis':'evidence'},1))
        with tempfile.TemporaryDirectory() as tmp:
            result=chat.run('那备机呢？',history,planner,lookup,Mock(),AuditStore(Path(tmp)/'audit.db'))
        self.assertEqual(result['status'],'answered')
        self.assertIn('局端主机',lookup.call_args[0][0])
        self.assertNotIn('OLD_OBSERVATION',lookup.call_args[0][0])
        self.assertIn('KB-013',[s['id'] for s in planner.call_args[0][0]['sources']])

    def test_topic_change_does_not_reuse_old_topology(self):
        history=[{'role':'user','content':'局端主机呢'},
                 {'role':'assistant','content':'旧解释'},
                 {'role':'user','content':'Python列表怎么排序'}]
        self.assertIsNone(chat.prefetch_query('那元组呢？',history))
        self.assertIsNone(chat.prefetch_query('解释Python装饰器',history[:2]))

    def test_conflict_note_survives_top_four(self):
        ids=['HA-007','KB-002','KB-004','KB-005','KB-011']
        with patch('legacy.unified_chat.qa.retrieve',return_value=[{'case':{'id':i}} for i in ids]):
            result=chat.search('ppp5')
        self.assertIn('KB-011',[s['id'] for s in result])
        self.assertLessEqual(len(result),5)

    def test_explicit_new_topic_does_not_prefetch_old_device(self):
        history=[{'role':'user','content':'局端主机切换规则是什么？'}]
        for question in ('那Python元组呢？','为什么Python列表可变？','那FastAPI怎么用？'):
            self.assertIsNone(chat.prefetch_query(question,history))
        self.assertIn('局端',chat.prefetch_query('那备机呢？',history))

    def test_complete_fences_only(self):
        action={'action':'answer','answer':'示例包含字面量 \\n，应原样保留', 'refs':[], 'basis':'general'}
        body=json.dumps(action,ensure_ascii=False)
        for content in (body,'```\n'+body+'\n```','```JSON\r\n'+body+'\r\n```'):
            self.assertEqual(self.call_model(content)[0],action)
        for content in ('解释文字\n'+body,'```json\n'+body[:-4],body+' trailing'):
            with self.assertRaises(ModelFailure): self.call_model(content)

    @staticmethod
    def call_model(content):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'choices':[{'message':{'content':content}}]}).encode()
        with patch('agent.chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=response
            return make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
                               'LLM_API_KEY':'test','LLM_MODEL_ID':'test'})({})
