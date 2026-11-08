"""连续追问与转向不同设备的检索实测，不调用模型或设备。"""
import unittest
import unified_chat as chat

def history(*questions):
    result=[]
    for q in questions:
        result.extend([{'role':'user','content':q},{'role':'assistant','content':'OLD_ASSISTANT_FACT_NOT_EVIDENCE'}])
    return result

class ThemeContextTests(unittest.TestCase):
    def test_third_turn_keeps_original_topic(self):
        h=history('局端触发切换的规则是什么？','为什么不保证业务恢复？')
        query=chat.prefetch_query('那该怎么判断呢？',h)
        self.assertIn('局端触发切换',query)
        self.assertNotIn('OLD_ASSISTANT',query)
        self.assertIn('KB-013',[s['id'] for s in chat.search(query)])

    def test_role_only_followup_does_not_replace_topic_anchor(self):
        query=chat.prefetch_query('为什么呢？',history('局端切换规则是什么？','那备机呢？'))
        self.assertIn('局端切换规则',query)
        self.assertIn('KB-013',[s['id'] for s in chat.search(query)])

    def test_explicit_device_switch_uses_its_rules(self):
        for question,wanted,wrong in (('那远端呢？','KB-004','KB-013'),('那Router呢？','KB-003','KB-013')):
            query=chat.prefetch_query(question,history('局端触发切换的规则是什么？'))
            ids=[s['id'] for s in chat.search(query)]
            self.assertIn(wanted,ids);self.assertNotIn(wrong,ids)

    def test_switched_device_stays_in_next_elliptical_turn(self):
        query=chat.prefetch_query('那该怎么判断呢？',history('局端触发切换的规则是什么？','那远端呢？'))
        self.assertEqual(chat.retrieval_group(query),'remote')
        self.assertNotIn('局端',query)
        self.assertIn('KB-004',[s['id'] for s in chat.search(query)])

    def test_comparison_keeps_shared_context_and_can_focus_next(self):
        self.assertEqual(chat.retrieval_group('局端和远端的切换有什么不同？'),'unknown')
        query=chat.prefetch_query('那远端呢？',history('局端和远端的切换有什么不同？'))
        self.assertEqual(chat.retrieval_group(query),'remote')
        self.assertIn('KB-004',[s['id'] for s in chat.search(query)])

    def test_generic_topic_and_unrelated_question_stop_old_lab(self):
        for question in ('为什么Python列表可变？','为什么天空是蓝色的？'):
            self.assertIsNone(chat.prefetch_query(question,history('局端切换规则是什么？')))
        self.assertIsNone(chat.prefetch_query('那该怎么判断呢？',history('局端切换规则是什么？','Python列表怎么排序？')))
        self.assertIsNone(chat.prefetch_query('那该怎么判断呢？',history('局端切换规则是什么？','帮我写一封邮件。')))

    def test_health_counter_prefetch_matches_retrieval_gate(self):
        for question in ('fall 3和rise 2分别是什么意思？','fall3与rise2有什么区别？'):
            query=chat.prefetch_query(question,[])
            self.assertIsNotNone(query)
            self.assertIn('KB-014',[s['id'] for s in chat.search(query)])
        for question in ('秋天为什么叫fall？','fall season and rise of the sun'):
            self.assertIsNone(chat.prefetch_query(question,[]))

if __name__=='__main__':unittest.main()
