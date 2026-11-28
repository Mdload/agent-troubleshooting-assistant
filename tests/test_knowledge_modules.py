"""模块主入口、条件隔离、按需来源和已审经验更新的离线回归。"""
if not __package__:
    import _bootstrap

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import qa
from knowledge_modules import compile_archive, load_corpus, clean_body, model_modules, module_document
from response_contract import response_policy
from unified_chat import search, prefetch_query
from tests.test_model_protocol import adapter
from tests.workflow_test_support import RunningApp


class ModuleKnowledgeTests(unittest.TestCase):
    def test_retrieved_topology_rules_include_real_routes_and_historical_failure_limits(self):
        records={r['id']:r for r in qa.RECORDS}
        expected={'KB-002':('203.0.113.134','固定 IP'),
                  'KB-003':('TOR4','物理 MAC','重启'),
                  'KB-004':('基站','有线','候选'),
                  'KB-007':('三层独立热备','普通 ping'),
                  'KB-017':('下挂','回程','指定源')}
        for ident,terms in expected.items():
            content=records[ident]['historical_finding']
            for term in terms:self.assertIn(term,content,(ident,term))
        sources=search('普通 ping 走管理 PPP 不通，但指定源地址走有线可以到局端，怎么判断')
        self.assertTrue({'KB-002','KB-007','KB-017'} & {s['id'] for s in sources})
        planner,opener=adapter()
        planner({'question':'远端备机接管后隧道地址会变吗','sources':[records['KB-002']],
                 'workflow_policy':{'mode':'knowledge','domain':'topology'}})
        payload=json.loads(opener.open.call_args[0][0].data)
        self.assertIn('203.0.113.134',payload['messages'][1]['content'])

    def test_topology_rules_reach_intake_and_diagnostic_model_prompts(self):
        from chat_model import select_prompt
        for view in (
            {'workflow_policy':{'mode':'knowledge','domain':'topology','chat_space':'ha'},'diagnostic_options':{}},
            {'workflow_policy':{'mode':'diagnostic','domain':'topology'}},
            {'workflow_policy':{'mode':'knowledge','domain':'topology'}}):
            prompt,_=select_prompt(view)
            for term in ('三层独立热备','普通 ping','TOR4','真实地址','203.0.113.134'):
                self.assertIn(term,prompt)

    def test_handbooks_match_archive_without_reading_archive_during_normal_load(self):
        compiled = compile_archive(qa.ROOT)
        for name, content in compiled.items():
            self.assertEqual((qa.ROOT/name).read_text(encoding='utf-8'), content, name)
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if '/docs/topology/' in path.as_posix() or '/docs/lab-basics/' in path.as_posix() or path.name == 'cases.json':
                self.fail('Normal load accessed source archive')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guarded):
            records, sources = load_corpus(qa.ROOT)
        self.assertEqual(len(records), 61)
        self.assertEqual(len({r['id'] for r in records}), 61)
        self.assertEqual(len(sources), 48)
        self.assertTrue(all('module_id' in r for r in records))

    def test_croc_book_contains_health_switch_watchdog_and_timing_without_source_footer(self):
        book = module_document('croc_local', qa.RECORDS)
        self.assertTrue({'KB-013', 'KB-014', 'KB-015', 'KB-018'} <= set(book['source_ids']))
        for term in ('fall 3', 'MAX_OWNER_FAILURES', '300秒', '不能相乘'):
            self.assertIn(term, book['content'])
        self.assertNotRegex(book['content'], r'(?m)^来源[：:]')
        self.assertFalse(book['current_state_supported'])

    def test_source_cleanup_preserves_code_and_different_conditions(self):
        text = '前提A成立。\n\n前提A成立。\n\n```text\n来源：代码里的原样内容\n\nx\n\nx\n```\n\n前提B不成立。\n来源：档案'
        cleaned = clean_body(text)
        self.assertEqual(cleaned.count('前提A成立。'), 1)
        self.assertIn('来源：代码里的原样内容\n\nx\n\nx', cleaned)
        self.assertIn('前提B不成立', cleaned)
        self.assertNotIn('来源：档案', cleaned)

    def test_real_transport_uses_one_module_per_name_and_keeps_reference_ids(self):
        sources = search('局端健康失败之后如何切换')
        context = {'workflow_policy': {'mode': 'knowledge', 'domain': 'topology'},
                   'question': '局端健康失败之后如何切换', 'sources': sources,
                   'response_contract': response_policy('局端健康失败之后如何切换')}
        before = copy.deepcopy(context)
        planner, opener = adapter()
        planner(context)
        payload = json.loads(opener.open.call_args[0][0].data)
        view = json.loads(payload['messages'][1]['content'])
        modules = view['knowledge_modules']
        self.assertEqual(len({m['id'] for m in modules}), len(modules))
        self.assertTrue(all('historical_finding' not in s for s in view['sources']))
        self.assertTrue(all('provenance_notes' not in s for m in modules for s in m['sections']))
        self.assertEqual({s['id'] for s in view['sources']}, {s['id'] for s in sources})
        self.assertEqual(context, before)
        self.assertFalse(search('云服务器局端的VIP配置'))

    def test_learned_material_is_in_same_module_and_disappears_when_withdrawn(self):
        source = copy.deepcopy(next(r for r in qa.KNOWLEDGE if r['id'] == 'KB-014'))
        source.update(id='EXP-'+'a'*32, kind='reviewed_experience', title='已核实补充',
                      historical_finding='核实后的新增经验。', historical_outcome='')
        enriched = module_document('croc_local', qa.RECORDS+[source])
        self.assertEqual(enriched['experience_count'], 1)
        self.assertIn('核实后的新增经验', enriched['content'])
        withdrawn = module_document('croc_local', qa.RECORDS)
        self.assertNotIn('核实后的新增经验', withdrawn['content'])

    def test_explicit_source_request_exposes_only_requested_provenance(self):
        source = search('局端切换规则')[0]
        view = model_modules({'sources': [source], 'response_contract': response_policy('依据什么来源？')})
        self.assertEqual(response_policy('解释局端切换')['source_display'], 'on_request')
        self.assertEqual(response_policy('依据什么来源？')['source_display'], 'expanded')
        self.assertIn('provenance_notes', view['knowledge_modules'][0]['sections'][0])
        history=[{'role':'user','content':'局端健康检查如何影响切换？'}]
        query=prefetch_query('依据是什么？',history)
        self.assertIn('局端健康检查',query)
        self.assertIn('KB-014',[r['id'] for r in search(query)])

    def test_http_module_entry_has_complete_book_and_empty_library_is_honest(self):
        with RunningApp() as client:
            code, book = client.call('/api/knowledge-libraries/croc_local')
            self.assertEqual(code, 200)
            self.assertIn('局端健康判据和故障计数', book['content'])
            self.assertIn('局端watchdog恢复', book['content'])
            code, empty = client.call('/api/knowledge-libraries/six_card')
            self.assertEqual(code, 200)
            self.assertEqual(empty['section_count'], 0)
            self.assertIn('尚无可用正文', empty['content'])
            self.assertEqual(client.call('/api/knowledge-libraries/unknown')[0], 404)

    def test_http_module_automatically_reflects_publication_and_withdrawal(self):
        from tests.test_experience_web import submission, approve
        with RunningApp() as client:
            store=client.server.get_experiences()
            item=approve(store,store.submit('test-owner',submission()))
            store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
            book=client.call('/api/knowledge-libraries/croc_local')[1]
            self.assertEqual(book['experience_count'],1)
            self.assertIn(submission()['corrected_answer'],book['content'])
            store.withdraw(item['id'],item['candidate_hash'],'authenticated-admin')
            book=client.call('/api/knowledge-libraries/croc_local')[1]
            self.assertEqual(book['experience_count'],0)


    def test_model_inputs_exclude_old_runtime_table_but_archive_and_date_stay_unchanged(self):
        source=next(row for row in qa.RECORDS if row['id']=='KB-008')
        original=copy.deepcopy(source)
        for mode in ('knowledge','diagnostic'):
            context={'question':'局端主机主要负责什么','sources':[source],
                     'workflow_policy':{'mode':mode}}
            projected=model_modules(context)
            section=projected['knowledge_modules'][0]['sections'][0]
            self.assertIn('Croc_sar',section['content'])
            self.assertIn('登记',section['content'])
            for old in ('本次资源状态','6条业务TCP ESTAB','七VIP在本机','Croc安装未运行','Croc服务安装但未运行'):
                self.assertNotIn(old,section['content'])
            self.assertTrue(section['runtime_snapshot_excluded'])
            self.assertEqual(section['evidence_date'],'2026-09-28')
            planner,opener=adapter()
            planner(context)
            payload=json.loads(opener.open.call_args[0][0].data)
            wire=json.loads(payload['messages'][1]['content'])
            self.assertNotIn('6条业务TCP ESTAB',json.dumps(wire,ensure_ascii=False))
            self.assertNotIn('七VIP在本机',json.dumps(wire,ensure_ascii=False))
        self.assertEqual(source,original)
        history=model_modules({'question':'2026-09-28局端备机当时的情况','sources':[source]})
        self.assertIn('本次资源状态',history['knowledge_modules'][0]['sections'][0]['content'])

    def test_old_connection_and_process_observations_are_not_current_model_input(self):
        from knowledge_freshness import source_for_model,historical_request
        for ident,old in [('KB-002','现场在6个源地址看到TCP9965 ESTAB'),
                          ('KB-005','脚本哈希在本轮一致'),('KB-009','MySQL/nginx/node进程已看到')]:
            excerpts = {
                'KB-002': old+'，局端也观察到对应连接。',
                'KB-005': '- 两局端的health/role/watchdog脚本哈希在本轮一致；两远端health/check_croc_links哈希一致。未比较所有文件。',
                'KB-009': old,
            }
            source={'id':ident,'historical_finding':'合成示例。\n'+excerpts[ident]}
            self.assertIn(old,source['historical_finding'])
            self.assertNotIn(old,source_for_model(source,'解释两端程序怎样配合')['historical_finding'])
            self.assertEqual(source_for_model(source,'回顾2026-09-28当时的状态'),source)
        self.assertFalse(historical_request('不要再用2026-09-28的历史情况判断现在'))
        self.assertFalse(historical_request('别参考旧状态，局端主机现在正常吗'))
        self.assertTrue(historical_request('和9月28日当时的情况对比'))
        import datetime
        day=datetime.date(2026,10,23)
        for question in ('2026-10-23局端主机状态','2026年10月23日局端主机状态',
                         '2026-10-24局端主机状态','2026-13-40局端主机状态'):
            self.assertFalse(historical_request(question,day),question)
        self.assertTrue(historical_request('2026年9月28日局端主机状态',day))



if __name__ == '__main__':
    unittest.main()
