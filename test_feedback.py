"""用户原话与同义问法回归，避免只测展示按钮。"""
import json
import unittest
from unittest.mock import patch
import qa

class FeedbackTests(unittest.TestCase):
    def test_switch_rule_paraphrases_have_conditions(self):
        for question in ['局端触发切换的规则是什么', '局端什么时候切到备机',
                         '局端接管条件有哪些', '局端主备切换要失败几次',
                         '局端故障优先级怎么变化']:
            with self.subTest(question=question):
                ids = [h['case']['id'] for h in qa.retrieve(question)]
                self.assertEqual(ids[0], 'KB-013')
                self.assertIn('KB-014', ids)

    def test_explicit_scope_is_not_overridden(self):
        ids = [h['case']['id'] for h in qa.retrieve('局端触发切换的规则是什么', group='remote')]
        self.assertNotIn('KB-013', ids)
        ids = [h['case']['id'] for h in qa.retrieve('局端触发切换的规则是什么', mode='fixed_primary_test')]
        self.assertNotIn('KB-013', ids)

    def test_short_answer_and_labeled_inference(self):
        hits = qa.retrieve('局端触发切换的规则是什么')
        data = {'summary': [
            {'text': '检查连续失败3次，优先级降20。', 'refs': ['KB-013'], 'type': 'fact'},
            {'text': '主机降至90后，健康备机100具有接管条件。', 'refs': ['KB-013'], 'type': 'inference'}]}
        text, sections = qa.parse_synthesis(json.dumps(data), hits)
        self.assertIn('推断：', text)
        self.assertNotIn('综合回答', text)
        self.assertNotIn('日期与未知项', text)
        self.assertLess(len(text), 150)

    def test_failed_model_does_not_dump_documents(self):
        with patch('qa.ask_model', side_effect=RuntimeError('private error')):
            result = qa.answer('局端触发切换的规则是什么', allow_new_topology=True)
        self.assertLess(len(result['answer']), 250)
        self.assertTrue(result['hits'])
        self.assertNotIn('private error', str(result))

if __name__ == '__main__':
    unittest.main()
