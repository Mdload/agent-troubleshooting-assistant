"""逻辑分区、空库和宿主检索范围回归；不调用真实模型或设备。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import qa
from diagnostic_core import AuditStore
from knowledge_catalog import LIBRARIES, catalog, query_scope, allows, filter_sources
from knowledge_coverage import assess
from question_routing import classify
from unified_chat import run, search, prefetch_query
from workflow_test_support import RunningApp


class CatalogTests(unittest.TestCase):
    def test_seven_libraries_with_honest_material_counts(self):
        result = catalog(qa.RECORDS)
        by_id = {item['id']: item for item in result['libraries']}
        self.assertEqual(len(by_id), 7)
        self.assertEqual(result['unique_record_count'], 61)
        self.assertEqual(by_id['general_basics']['document_count'], 30)
        for name in ('six_card', 'three_card'):
            self.assertEqual(by_id[name]['state'], 'awaiting_material')
            self.assertEqual(by_id[name]['record_count'], 0)
        self.assertEqual(by_id['croc_local']['environment_counts'], {'physical': 0, 'cloud': 0})
        self.assertEqual(len({r['id'] for r in qa.RECORDS}), len(qa.RECORDS))
        for item in LIBRARIES:
            self.assertTrue((qa.ROOT/item['directory']/'README.md').is_file())

    def test_explicit_card_type_does_not_reuse_unclassified_old_configuration(self):
        for q in ('六卡设备的Croc服务路径在哪', '三卡设备PPP接口与卡槽怎么对应'):
            with self.subTest(question=q):
                sources = search(q)
                self.assertFalse(any(s['source_scope']=='historical_snapshot' for s in sources))
                coverage = assess(q, sources)
                self.assertNotEqual(coverage['status'], 'supported')
                self.assertTrue(any('分区未找到适用资料' in g for g in coverage['gaps']))

    def test_explicit_cloud_and_physical_do_not_reuse_unmarked_lan_ha(self):
        for env in ('云服务器', '物理机'):
            sources = search(env+'局端的VIP和Croc服务配置是什么')
            self.assertEqual(sources, [])
            self.assertEqual(assess(env+'局端的VIP和Croc服务配置是什么', sources)['status'], 'missing')

    def test_unrecorded_software_version_has_no_configuration_evidence(self):
        q = 'Croc v2.4的监听端口是什么'
        self.assertEqual(search(q), [])
        self.assertIn('所问软件版本尚无直接资料：v2.4', assess(q, [])['gaps'])
        for q in ('Croc2.4的监听端口是什么', '六卡设备版本2.4的配置'):
            self.assertEqual(query_scope(q)['software_versions'], ['v2.4'])

    def test_public_basic_can_supplement_but_not_fill_device_library(self):
        q = '六卡设备SSH怎么使用'
        sources = search(q)
        self.assertIn('KB-109', [s['id'] for s in sources])
        self.assertTrue(all(s['source_scope']=='public_principle' for s in sources))
        coverage = assess(q, sources)
        self.assertEqual(coverage['status'], 'partial')
        self.assertIn('KB-109', coverage['supporting_refs'])

    def test_future_tagged_records_are_isolated_and_comparison_can_use_both(self):
        six = {'id':'example-six', 'knowledge_libraries':['six_card'], 'device_types':['six_card'],
               'source_scope':'documented', 'deployment_environments':[], 'software_versions':['v1.0']}
        three = dict(six, id='example-three', knowledge_libraries=['three_card'], device_types=['three_card'])
        self.assertEqual(filter_sources([six, three], query_scope('六卡设备的配置')), [six])
        self.assertEqual(filter_sources([six, three], query_scope('三卡设备的配置')), [three])
        self.assertEqual(filter_sources([six, three], query_scope('六卡和三卡设备比较')), [six, three])
        self.assertFalse(allows(six, query_scope('六卡设备v2.0的配置')))
        self.assertTrue(allows(six, query_scope('六卡设备v1.0的配置')))

    def test_even_principle_record_with_specific_device_label_cannot_cross_type(self):
        source = {'knowledge_libraries':['device_common'], 'source_scope':'public_principle',
                  'device_types':['six_card'], 'deployment_environments':[], 'software_versions':[]}
        self.assertFalse(allows(source, query_scope('三卡设备的基本原理')))

    def test_recent_followup_retargets_type_without_losing_scope(self):
        history = [{'role':'user', 'content':'六卡设备的Croc启动关系是什么'}]
        query = prefetch_query('那三卡呢', history)
        self.assertEqual(query_scope(query)['device_types'], ['three_card'])
        self.assertEqual(classify('那三卡呢', history)['domain'], 'libraries')
        self.assertIsNone(prefetch_query('那SSH是什么', history))
        self.assertEqual(query_scope('16卡设备')['device_types'], [])
        self.assertEqual(query_scope('十三卡设备')['device_types'], [])
        self.assertEqual(query_scope('6 张卡设备')['device_types'], ['six_card'])

    def test_explicit_library_filter_rejects_unknown_or_conflicting_scope(self):
        hits = qa.retrieve('Linux文件权限怎么看', library='general_basics')
        self.assertTrue(hits)
        self.assertTrue(all('general_basics' in h['case']['knowledge_libraries'] for h in hits))
        with self.assertRaises(ValueError):
            qa.retrieve('Linux文件权限怎么看', library='missing_library')
        with self.assertRaises(ValueError):
            qa.retrieve('六卡设备的配置', library='three_card')


class CatalogWorkflowTests(unittest.TestCase):
    def test_http_catalog_and_empty_library_answer_without_device_reads(self):
        planner = Mock(return_value=({'action':'answer', 'answer':'三卡设备的实际配置还没有确认，暂时不能确定 Croc 是怎么启动的。', 'refs':[], 'basis':'limited'}, 0))
        probe = Mock()
        with RunningApp(planner, probe) as client:
            code, directory = client.call('/api/knowledge-libraries')
            self.assertEqual(code, 200)
            self.assertEqual(len(directory['libraries']), 7)
            planner.assert_not_called()
            code, result = client.chat('三卡设备的Croc启动关系是什么')
            self.assertEqual(code, 200)
            self.assertEqual(result['status'], 'answered', result)
            self.assertEqual(result['knowledge_status'], 'missing')
            self.assertEqual(result['knowledge_scope']['selected_libraries'], ['three_card'])
            self.assertIn('配置还没有确认', result['answer'])
            self.assertEqual(planner.call_args[0][0]['tool_catalog'], {})
            self.assertEqual(client.store.list(), [])
        probe.assert_not_called()

    def test_model_query_rewrite_does_not_import_another_device_configuration(self):
        foreign = search('局端备机为什么不运行Croc')
        self.assertTrue(foreign)
        lookup = Mock(side_effect=[[], foreign])
        planner = Mock(side_effect=[({'action':'search','query':'局端备机为什么不运行Croc'}, 0),
                                    ({'action':'answer','answer':'三卡资料仍不足。','refs':[],'basis':'limited'}, 0)])
        probe = Mock()
        with tempfile.TemporaryDirectory() as directory:
            result = run('三卡设备的启动配置是什么', [], planner, lookup, probe,
                         AuditStore(Path(directory)/'audit.db'), permitted_checks={},
                         question_policy={'mode':'knowledge','domain':'libraries'})
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['sources'], [])
        self.assertEqual(planner.call_args[0][0]['sources'], [])
        probe.assert_not_called()

    def test_persistent_followup_and_live_question_do_not_guess_a_device(self):
        planner = Mock(return_value=({'action':'answer','answer':'专属资料暂空。','refs':[],'basis':'limited'}, 0))
        probe = Mock()
        with RunningApp(planner, probe) as client:
            client.chat('六卡设备的Croc启动关系是什么', conversation_id='catalog-test')
            _, result = client.chat('那三卡呢', conversation_id='catalog-test')
            self.assertEqual(result['knowledge_scope']['device_types'], ['three_card'])
            _, live = client.chat('那现在正常吗', conversation_id='catalog-test')
            self.assertEqual(live['status'], 'current_unverified', live)
            self.assertEqual(client.store.list(), [])
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
