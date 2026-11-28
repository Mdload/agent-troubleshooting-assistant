"""实验室基础知识的检索与正式接口验证；模型和设备均为替身。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import retrieval.qa as qa
from devices.diagnostic_core import AuditStore
from retrieval.knowledge_coverage import assess
from retrieval.lab_basics import BY_ID, SECTIONS, is_basics_query
from legacy.question_routing import classify
from legacy.unified_chat import run, search
from tests.workflow_test_support import RunningApp


# 人工编写的开发问法，不是真人验收集或回答准确率样本。
QUESTIONS = [
    ('绝对路径和相对路径有什么区别', 'KB-101'),
    ('Linux文件权限怎么看', 'KB-102'),
    ('Linux进程和内存怎么看', 'KB-103'),
    ('磁盘满了如何检查', 'KB-104'),
    ('默认路由是什么', 'KB-105'),
    ('DNS域名解析是什么', 'KB-106'),
    ('TCP和UDP有什么区别', 'KB-107'),
    ('NAT和回程是什么', 'KB-108'),
    ('SSH是什么', 'KB-109'),
    ('SCP和SFTP怎么传文件', 'KB-110'),
    ('SSH隧道怎么建立', 'KB-111'),
    ('HTTPS证书失败怎么排查', 'KB-112'),
    ('systemd active/exited是什么意思', 'KB-113'),
    ('enabled和disabled是什么意思', 'KB-114'),
    ('journalctl日志怎么查看', 'KB-115'),
    ('nginx和Node是什么', 'KB-116'),
    ('MySQL和达梦数据库是什么', 'KB-117'),
    ('SELECT和WHERE怎么查询', 'KB-118'),
    ('数据库连接失败如何排查', 'KB-119'),
    ('数据库备份导出是什么', 'KB-120'),
    ('ping时延丢包怎么看', 'KB-121'),
    ('iperf吞吐Mbps怎么理解', 'KB-122'),
    ('tcpdump怎么限量抓包', 'KB-123'),
    ('CSV实验数据怎么整理', 'KB-124'),
    ('Python脚本Traceback报错怎么看', 'KB-125'),
    ('Python虚拟环境怎么用', 'KB-126'),
    ('JSON怎么解析', 'KB-127'),
    ('Git暂存和commit有什么区别', 'KB-128'),
]


def answer(text, refs, basis='evidence'):
    return {'action':'answer', 'answer':text, 'refs':refs, 'basis':basis}, 0


class BasicKnowledgeTests(unittest.TestCase):
    def test_eight_categories_and_thirty_public_records(self):
        records = [r for r in qa.KNOWLEDGE if r['kind']=='lab_basics']
        self.assertEqual(len({s[1] for s in SECTIONS}), 8)
        self.assertEqual({r['id'] for r in records}, {'KB-%s' % i for i in range(101,131)})
        self.assertEqual(len(records), 30)
        for r in records:
            self.assertIsNone(r['evidence_date'])
            self.assertEqual(r['documentation_date'], '2026-10-21' if r['id'] in ('KB-129','KB-130') else '2026-10-12')
            self.assertEqual(r['knowledge_coverage']['scope'], 'general_principle')
            self.assertFalse(r['knowledge_coverage']['current_state_supported'])
            self.assertFalse(r['repair_enabled'])
            self.assertTrue(r['reference_urls'])
            self.assertTrue(all(url.startswith('https://') for url in r['reference_urls']))
            self.assertTrue(set(r['sources']) <= set(qa.SOURCE_MAP))
            self.assertIn('用途', r['historical_finding'])
            self.assertIn('基础概念', r['historical_finding'])
            self.assertIn('常见问题', r['historical_finding'])

    def test_all_development_questions_retrieve_the_intended_topic(self):
        for q, wanted in QUESTIONS:
            with self.subTest(question=q):
                route = classify(q)
                self.assertEqual(route['domain'], 'basics')
                sources = search(q)
                self.assertIn(wanted, [s['id'] for s in sources])
                self.assertTrue(all(s['kind']=='lab_basics' for s in sources))
                self.assertEqual(assess(q, sources, basics=True)['status'], 'supported')

    def test_topology_queries_keep_original_sources_and_dates(self):
        for q, wanted in [('局端备机为什么不运行Croc','KB-005'),
                          ('远端Croc谁负责启动','KB-004'),
                          ('SSH能连140是不是业务就通了','KB-001')]:
            with self.subTest(question=q):
                sources = search(q)
                self.assertIn(wanted, [s['id'] for s in sources])
                self.assertTrue(all(s['kind']!='lab_basics' for s in sources))
                self.assertTrue(all(s['evidence_date']=='2026-09-28'
                                    for s in sources if s['kind']=='topology'))

    def test_physical_wiring_does_not_claim_tcp_port_coverage(self):
        for q in ('能从现有文档精确列出每根网线插在哪个交换机端口吗？',
                  '这根光纤接在哪个交换机端口？','能给出现有接线表吗？'):
            with self.subTest(question=q):
                self.assertFalse(is_basics_query(q))
                self.assertEqual(classify(q)['domain'],'topology')
                sources=search(q)
                self.assertNotIn('KB-107',[s['id'] for s in sources])
                coverage=assess(q,sources)
                self.assertNotEqual(coverage['status'],'supported')
                self.assertEqual(coverage['requirements'][0]['topic'],'wiring')

    def test_switch_port_concepts_still_get_general_learning(self):
        for q in ('交换机物理端口是什么','交换机端口和TCP端口有什么区别','网线如何连接交换机'):
            with self.subTest(question=q):
                self.assertEqual(classify(q)['route'],'general')
                self.assertFalse(is_basics_query(q))
        self.assertEqual(classify('TCP和UDP端口有什么区别')['domain'],'basics')

    def test_public_reference_does_not_answer_private_configuration(self):
        for q in ('我们的新服务监听什么端口','我们的nginx监听哪个端口',
                  '实验室Node和MySQL业务关系是什么','我们的数据库名是什么',
                  '这台服务器Python安装在哪','这台服务器磁盘容量是多少'):
            with self.subTest(question=q):
                self.assertFalse(is_basics_query(q))
                self.assertNotEqual(assess(q, search(q))['status'], 'supported')
                self.assertFalse(any(s['kind']=='lab_basics' for s in search(q)))

    def test_uncovered_advanced_topic_stays_out_of_basic_scope(self):
        for q in ('MySQL事务是什么','数据库连接池耗尽怎么处理','SSH加密算法实现是什么'):
            self.assertFalse(is_basics_query(q))
        self.assertEqual(classify('MySQL事务是什么')['route'], 'general')
        self.assertEqual(qa.retrieve('数据库连接池耗尽怎么处理'), [])

    def test_basic_examples_are_not_a_complete_operation_manual(self):
        q = 'systemd重启完整操作步骤是什么'
        result = assess(q, search(q), basics=True)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('procedure', [n['aspect'] for n in result['missing']])

    def test_current_status_and_direct_read_still_require_a_target(self):
        self.assertEqual(classify('实验室SSH现在正常吗')['route'], 'clarify')
        self.assertEqual(classify('请检查远端主机进程')['route'], 'diagnostic')
        self.assertEqual(classify('Python列表是什么')['route'], 'general')

    def test_new_basic_topic_does_not_inherit_old_topology_context(self):
        history = [{'role':'user','content':'局端备机为什么不运行Croc'}]
        route = classify('那SSH是什么', history)
        self.assertEqual(route['domain'], 'basics')

    def test_short_log_followup_keeps_existing_topology_context(self):
        history = [{'role':'user','content':'局端备机为什么不运行Croc'}]
        self.assertEqual(classify('那日志怎么查', history)['domain'], 'topology')

    def test_short_command_words_do_not_match_arbitrary_words(self):
        self.assertFalse(is_basics_query('discuss ideas'))


class BasicWorkflowTests(unittest.TestCase):
    def test_formal_http_answers_from_public_reference_without_device_reads(self):
        planner = Mock(return_value=answer('这是本地转发的学习示例，实际目标需要确认。', ['KB-111']))
        probe = Mock()
        with RunningApp(planner, probe) as client:
            code, result = client.chat('SSH隧道怎么建立')
            self.assertEqual(code, 200)
            self.assertEqual(result['status'], 'answered', result)
            self.assertEqual(result['knowledge_status'], 'supported')
            self.assertEqual(result['refs'], ['KB-111'])
            self.assertEqual(client.store.list(), [])
            context = planner.call_args[0][0]
            self.assertEqual(context['workflow_policy']['domain'], 'basics')
            self.assertEqual(context['tool_catalog'], {})
            self.assertTrue(context['sources'][0]['reference_urls'])
            self.assertIsNone(context['sources'][0]['evidence_date'])
        probe.assert_not_called()

    def test_teaching_commands_never_grants_a_device_tool(self):
        planner = Mock(return_value=({'action':'read','device':'local-primary','check':'services'},0))
        probe = Mock()
        with RunningApp(planner, probe) as client:
            _, result = client.chat('systemctl服务状态怎么看')
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(client.store.list(), [])
        probe.assert_not_called()

    def test_model_search_cannot_turn_basics_into_private_facts(self):
        planner = Mock(side_effect=[({'action':'search','query':'SSH默认端口是什么'},0),
                                    answer('实际端口仍需配置依据。', [], 'limited')])
        probe = Mock()
        with tempfile.TemporaryDirectory() as directory:
            result = run('我们的新服务监听什么端口', [], planner, search, probe,
                         AuditStore(Path(directory)/'audit.db'), permitted_checks={},
                         question_policy={'mode':'knowledge','domain':'lab'})
        self.assertEqual(result['knowledge_status'], 'missing')
        self.assertEqual(result['knowledge_sufficiency']['supporting_refs'], [])
        probe.assert_not_called()

    def test_public_database_reference_cannot_be_cited_as_lab_configuration(self):
        sources = [r for r in qa.KNOWLEDGE if r['id']=='KB-117']
        planner = Mock(return_value=answer('实验室数据库名是sample。', ['KB-117']))
        probe = Mock()
        with tempfile.TemporaryDirectory() as directory:
            result = run('我们的数据库名是什么', [], planner, Mock(return_value=sources), probe,
                         AuditStore(Path(directory)/'audit.db'), permitted_checks={},
                         question_policy={'mode':'knowledge','domain':'lab'})
        self.assertEqual(result['status'], 'incomplete')
        self.assertNotIn('数据库名是sample', result['answer'])
        self.assertEqual(planner.call_count, 2)
        probe.assert_not_called()


if __name__=='__main__':
    unittest.main()
