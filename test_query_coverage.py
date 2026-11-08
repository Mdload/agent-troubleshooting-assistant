"""未用于上轮调参的新问法及显式设备/模式边界。"""
import unittest
import qa
from unified_chat import needs_topology,search

class QueryCoverageTests(unittest.TestCase):
    def test_new_paraphrases_reach_relevant_rules(self):
        cases=[('watchdog是不是会把服务器重启？','KB-015'),
               ('接收队列积压会直接切换节点吗？','KB-015'),
               ('VRRP的等待时间就是恢复业务时间吗？','KB-018'),
               ('备用节点隧道接口没载波就坏了吗？','KB-014')]
        for question,source in cases:
            with self.subTest(question=question):
                self.assertTrue(needs_topology(question))
                self.assertIn(source,[s['id'] for s in search(question)])
        self.assertTrue({'KB-013','KB-015'} <= {s['id'] for s in search('watchdog与VRRP选举之间差在哪里？')})

    def test_explicit_scope_survives_normalization(self):
        for mode,group in [('unknown','remote'),('fixed_primary_test','unknown')]:
            ids=[h['case']['id'] for h in qa.retrieve('watchdog是不是会把服务器重启？',mode,group)]
            self.assertNotIn('KB-015',ids)

    def test_general_question_does_not_pin_lab_watchdog(self):
        self.assertEqual(qa.context_ids('硬件watchdog如何喂狗','unknown','unknown'),[])
        for question in ('硬件watchdog如何喂狗','watchdog一般会重启服务器吗？'):
            self.assertEqual(qa.retrieve(question),[])
        self.assertFalse(needs_topology('Python元组怎么用？'))
        self.assertEqual(qa.retrieve('Python元组怎么用？'),[])
