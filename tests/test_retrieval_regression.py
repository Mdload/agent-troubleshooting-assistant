import unittest
import retrieval.qa as qa

class RetrievalRegression(unittest.TestCase):
    def test_additional_wordings(self):
        pairs=[('脚本里 fall 和 rise 各管什么？','KB-014'),
               ('MAX_OWNER_FAILURES怎样影响降权？','KB-014'),
               ('Recv-Q 堆积时有哪些自动恢复条件？','KB-015'),
               ('9965接收队列积压多久会重启？','KB-015'),
               ('局端备用机上的Croc不启动，是正常状态吗？','KB-014'),
               ('局端主机应该保留哪些业务路由？','KB-014'),
               ('解释远端业务经过Router送到局端的路径。','KB-003'),
               ('可以把croc-ha-health.sh当成状态查询直接执行吗？','KB-007')]
        for question,source in pairs:
            with self.subTest(question=question):
                self.assertIn(source,[h['case']['id'] for h in qa.retrieve(question)[:4]])
    def test_unrelated_queries_remain_empty(self):
        for question in ('Python列表和元组有什么区别？','数据库连接池耗尽怎么处理？','fall season是什么意思？','队列数据结构怎么实现？'):
            with self.subTest(question=question):
                self.assertEqual(qa.retrieve(question),[])
    def test_compound_identifier_requires_an_exact_match_for_unknown_domain(self):
        terms = set(qa.tokens('查看 Zeta_probe-agent.service 与 203.0.113.134'))
        self.assertTrue({'zeta_probe-agent.service', 'zeta', 'probe', 'agent', 'service',
                         '203.0.113.134', '查看'}.issubset(terms))
        self.assertNotIn('134', terms)
        base = {'operating_modes': [], 'device_groups': [], 'kind': 'reviewed_experience'}
        title = dict(base, id='EXP-title', title='Zeta_probe-agent.service')
        body = dict(base, id='EXP-body', historical_finding='Zeta_probe-agent.service')
        scoped = dict(title, id='EXP-scoped', operating_modes=['normal_ha'], device_groups=['local'])
        records = [body, title, scoped]
        ids = [hit['case']['id'] for hit in qa.retrieve(
            'Zeta_probe-agent.service', mode='normal_ha', group='local', extra_records=records)]
        self.assertEqual(ids[:3], ['EXP-scoped', 'EXP-title', 'EXP-body'])
        self.assertEqual(qa.retrieve('zeta_probe-notes.txt', extra_records=records), [])

    def test_explicit_scope_applies_to_context(self):
        for group in ('remote','router'):
            for hit in qa.retrieve('局端健康路由和Recv-Q问题',group=group):
                groups=hit['case']['device_groups']
                self.assertTrue(not groups or group in groups)
