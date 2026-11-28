"""资料范围与正式入口的离线开发检查；不等于真人问答正确率。"""
if not __package__:
    import _bootstrap

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import qa
from knowledge_coverage import SUPPORT, HISTORY_TOPICS, assess, requirements, answer_issue
from diagnostic_core import AuditStore
from unified_chat import run, search
from tests.workflow_test_support import RunningApp
from question_routing import classify
from lab_basics import BY_ID
from chat_model import ModelFailure


def action(text='一般原理：需核对配置和启动入口，实验室具体值未知。', refs=None, basis='limited'):
    return {'action': 'answer', 'answer': text, 'refs': refs or [], 'basis': basis}, 2


class CoverageTests(unittest.TestCase):
    def test_unspecified_backup_croc_stop_does_not_discard_available_ha_rules(self):
        question='不要读取，只解释备机为什么可能停止Croc。'
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-013','KB-014','KB-006','KB-009')]
        coverage=assess(question,sources)
        self.assertEqual({n['topic'] for n in coverage['requirements']},{'local_ha','remote_ha'})
        self.assertEqual(coverage['status'],'partial')
        self.assertIn('KB-013',coverage['supporting_refs'])
        self.assertEqual({n['topic'] for n in coverage['missing']},{'remote_ha'})
        self.assertIsNone(answer_issue(coverage,'limited',['KB-013'],sources))

    def test_croc_service_vip_relationship_uses_ha_rules_for_the_named_side(self):
        for side,topic,ref in [('局端','local_ha','KB-013'),('远端','remote_ha','KB-004')]:
            with self.subTest(side=side):
                question=side+'Croc服务与VIP在正常HA里是什么关系？'
                coverage=assess(question,search(question))
                self.assertEqual({n['topic'] for n in coverage['requirements']},{topic})
                self.assertEqual(coverage['status'],'supported',coverage)
                self.assertIn(ref,coverage['supporting_refs'])
        # Croc内外层地址仍需隧道机制，不能把VIP一词泛化为HA规则。
        self.assertEqual({n['topic'] for n in requirements('Croc内层地址和VIP有什么区别')},{'croc'})

    def test_comparison_covers_both_named_mechanisms(self):
        for question, topics, refs in (
            ('局端watchdog和Keepalived的职责有什么区别', {'local_watchdog','local_ha'}, {'KB-015','KB-013'}),
            ('局端watchdog重启Croc和Keepalived移动VIP是同一个动作吗', {'local_watchdog','local_ha'}, {'KB-015','KB-013'}),
            ('Router与Croc分别承担什么职责', {'router_ha','croc'}, {'KB-003','KB-002'}),
            ('局端watchdog与健康脚本的分工是什么', {'local_watchdog','local_health'}, {'KB-015','KB-014'}),
        ):
            with self.subTest(question=question):
                sources = search(question)
                coverage = assess(question, sources)
                self.assertEqual({n['topic'] for n in coverage['requirements']}, topics)
                self.assertEqual(coverage['status'], 'supported', coverage)
                self.assertTrue(refs <= set(coverage['supporting_refs']))
                self.assertIsNone(answer_issue(coverage, 'evidence', list(refs), sources))
                self.assertEqual(answer_issue(coverage, 'evidence', [next(iter(refs))], sources),
                                 'knowledge_citation_gap')

    def test_comparison_missing_one_source_does_not_borrow_other_side(self):
        question = '局端watchdog和Keepalived的区别是什么'
        sources = [s for s in qa.RECORDS if s['id']=='KB-015']
        coverage = assess(question, sources)
        self.assertEqual(coverage['status'], 'partial')
        self.assertEqual([n['topic'] for n in coverage['missing']], ['local_ha'])
        self.assertEqual(answer_issue(coverage, 'evidence', ['KB-015'], sources), 'knowledge_insufficient')

    def test_comparison_retains_mode_and_device_boundaries(self):
        question = '远端watchdog和Keepalived的职责区别是什么'
        sources = search(question)
        self.assertNotIn('KB-015', [s['id'] for s in sources])
        self.assertNotIn('local_ha', [n['topic'] for n in requirements(question)])
        coverage = assess('局端watchdog和Keepalived职责区别是什么', qa.RECORDS, mode='maintenance')
        self.assertNotEqual(coverage['status'], 'supported')
        self.assertFalse(coverage['supporting_refs'])

    def test_every_record_has_curated_scope_without_new_snapshot(self):
        self.assertEqual(set(SUPPORT) | set(HISTORY_TOPICS) | set(BY_ID), {r['id'] for r in qa.RECORDS})
        self.assertEqual((len(qa.KNOWLEDGE), len(qa.CASES)), (48, 13))
        for source in qa.RECORDS:
            self.assertFalse(source['knowledge_coverage']['current_state_supported'])
            if source['id'].startswith('HA-'):
                self.assertTrue(all(a == ['history'] for a in source['knowledge_coverage']['supports'].values()))
            elif source.get('kind') == 'lab_basics':
                self.assertIsNone(source['evidence_date'])
                self.assertEqual(source['knowledge_coverage']['scope'], 'general_principle')
            else:
                self.assertEqual(source['evidence_date'], '2026-09-28')

    def test_existing_direct_mechanism_and_configuration(self):
        for question in ('局端备机为什么不运行Croc', '远端主备切换规则是什么',
                         'Router负责运行Croc吗', 'Croc监听什么端口',
                         '局端用哪个接口做Keepalived心跳', '远端Croc从rc.local还是systemd启动'):
            with self.subTest(question=question):
                result = assess(question, qa.RECORDS)
                self.assertEqual(result['status'], 'supported', result)
                self.assertTrue(result['supporting_refs'])
                self.assertFalse(result['semantic_verified'])

    def test_new_service_does_not_borrow_croc_port(self):
        r = assess('我们的新服务监听什么端口', qa.RECORDS)
        self.assertEqual(r['status'], 'missing')
        self.assertEqual(r['supporting_refs'], [])
        self.assertTrue(any('服务名称' in gap for gap in r['gaps']))

    def test_config_and_paths_are_not_complete_operation_steps(self):
        for question in ('局端Croc如何重启', '远端Croc如何重启', '固定主机测试如何退出，具体命令是什么'):
            with self.subTest(question=question):
                r = assess(question, qa.RECORDS)
                self.assertEqual(r['status'], 'partial')
                self.assertFalse(r['supporting_refs'])
                self.assertIn('procedure', [n['aspect'] for n in r['missing']])

    def test_multi_aspect_retains_direct_port_but_reports_missing_steps(self):
        r = assess('局端Croc监听什么端口，如何重启', qa.RECORDS)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual([n['aspect'] for n in r['supported']], ['ports'])
        self.assertEqual([n['aspect'] for n in r['missing']], ['procedure'])
        self.assertIn('KB-002', r['supporting_refs'])

    def test_startup_owner_does_not_hide_missing_operation_steps(self):
        for question in ('远端Croc谁负责启动，手动重启步骤是什么',
                         '远端Croc谁负责启动，以及如何重启',
                         '远端Croc谁负责启动和如何停止'):
            with self.subTest(question=question):
                r = assess(question, qa.RECORDS)
                self.assertEqual(r['status'], 'partial')
                self.assertEqual([n['aspect'] for n in r['supported']], ['mechanism'])
                self.assertEqual([n['aspect'] for n in r['missing']], ['procedure'])
                self.assertIn('KB-009', r['supporting_refs'])

    def test_checking_startup_owner_is_not_a_request_to_start_service(self):
        needs = requirements('如何判断远端Croc谁负责启动')
        self.assertEqual([n['aspect'] for n in needs], ['checklist'])

    def test_explicit_gaps_are_never_positive_facts(self):
        for question in ('交换机完整接线是什么', 'ppp5对应哪家运营商',
                         'Croc算法91具体怎么调度', '实验室Node和MySQL的业务关系是什么',
                         '局端主备切换实测多少秒'):
            with self.subTest(question=question):
                r = assess(question, qa.RECORDS)
                self.assertIn(r['status'], ('partial', 'missing'))
                self.assertFalse(r['supporting_refs'])
                self.assertTrue(r['gaps'])

    def test_normal_ha_rules_do_not_answer_test_or_maintenance_mode(self):
        for mode in ('fixed_primary_test', 'maintenance'):
            with self.subTest(mode=mode):
                r = assess('局端备机为什么不运行Croc', qa.RECORDS, mode=mode)
                self.assertNotEqual(r['status'], 'supported')
                self.assertFalse(r['supporting_refs'])

    def test_remote_question_cannot_use_local_rule(self):
        local = [r for r in qa.RECORDS if r['id'] in ('KB-005', 'KB-013', 'KB-014')]
        r = assess('远端主备切换规则是什么', local)
        self.assertNotEqual(r['status'], 'supported')
        self.assertFalse(r['supporting_refs'])

    def test_mixed_chapter_does_not_extend_normal_startup_to_test_mode(self):
        source = [r for r in qa.RECORDS if r['id'] == 'KB-009']
        self.assertEqual(assess('远端Croc从rc.local还是systemd启动', source)['status'], 'supported')
        r = assess('远端Croc从rc.local还是systemd启动', source, mode='fixed_primary_test')
        self.assertNotEqual(r['status'], 'supported')
        self.assertFalse(r['supporting_refs'])

    def test_old_case_only_supports_historical_example(self):
        source = [r for r in qa.CASES if r['id'] == 'HA-004']
        self.assertNotEqual(assess('远端Croc怎样启动', source)['status'], 'supported')
        r = assess('远端Croc历史故障案例是什么', source)
        self.assertEqual(r['status'], 'supported')
        self.assertEqual(r['supporting_refs'], ['HA-004'])
        self.assertFalse(r['current_state_supported'])

    def test_unrecognized_topic_is_not_declared_sufficient(self):
        self.assertEqual(assess('这套系统的许可证是什么', qa.RECORDS)['status'], 'unassessed')
        self.assertEqual(assess('局端设备许可证是什么', qa.RECORDS)['status'], 'unassessed')
        self.assertEqual(assess('这套系统的许可证是什么', [])['status'], 'missing')

    def test_legacy_ask_cannot_guess_missing_port_or_restart_steps(self):
        with patch('qa.ask_model') as model:
            result = qa.answer('局端Croc监听什么端口，如何重启', allow_new_topology=True)
        self.assertEqual(result['kind'], 'insufficient')
        self.assertIn('操作步骤', result['answer'])
        model.assert_not_called()

    def test_selected_search_retains_dates_and_scope(self):
        sources = search('局端主备切换规则是什么')
        self.assertTrue(sources)
        for source in sources:
            self.assertIn('knowledge_coverage', source)
            if source['id'].startswith('KB-'):
                self.assertIn('documentation_date', source)
                self.assertEqual(source['evidence_date'], '2026-09-28')
        rule = next(s for s in sources if s['id'] == 'KB-013')
        self.assertEqual(rule['documentation_date'], '2026-10-01')

    def test_candidate_provenance_and_actual_retrieval_or_route(self):
        data = json.loads((qa.ROOT/'planning/knowledge-coverage-candidates.json').read_text(encoding='utf-8'))
        self.assertFalse(data['real_user_feedback'])
        self.assertFalse(data['independent_acceptance'])
        self.assertEqual(len({item['id'] for item in data['candidates']}), 16)
        for item in data['candidates']:
            with self.subTest(candidate=item['id']):
                path, marker = item['source'].split('#', 1)
                self.assertIn(marker, (qa.ROOT/path).read_text(encoding='utf-8-sig'))
                expected = item['expected']
                if expected in ('clarify', 'unsupported_device'):
                    self.assertEqual(classify(item['question'])['route'], expected)
                else:
                    result = assess(item['question'], search(item['question']))
                    if expected == 'gap':
                        self.assertNotEqual(result['status'], 'supported')
                        self.assertTrue(result['gaps'])
                    else:
                        self.assertEqual(result['status'], expected, result)


class CoverageWorkflowTests(unittest.TestCase):
    def test_json_correction_does_not_whitelist_related_recovery_time_source(self):
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-013','KB-014','KB-018')]
        planner=Mock(side_effect=[ModelFailure('model_invalid_json',7),
            action('按正常HA文档解释，当前设备状态未核实。',['KB-013','KB-014'],'evidence')])
        result=self.execute('局端健康脚本与Keepalived的职责有什么区别？',planner,lambda q:sources)
        self.assertEqual(result['status'],'answered',result)
        feedback=planner.call_args_list[1][0][0]['correction_feedback']
        self.assertEqual(set(feedback['valid_refs']),{'KB-013','KB-014'})
        self.assertNotIn('KB-018',feedback['valid_refs'])
        self.assertEqual(feedback['code'],'model_invalid_json')
        self.assertEqual(result['total_tokens'],9)
        self.assertEqual(planner.call_count,2)

    def test_json_correction_still_rejects_unsupported_reference_without_second_retry(self):
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-013','KB-014','KB-018')]
        planner=Mock(side_effect=[ModelFailure('model_invalid_json',7),
            action('按文档解释。',['KB-013','KB-014','KB-018'],'evidence'),action()])
        result=self.execute('局端健康脚本与Keepalived的职责有什么区别？',planner,lambda q:sources)
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['reason'],'unsupported_knowledge_citation')
        self.assertEqual(planner.call_count,2)
        self.assertEqual(len([e for e in result['events'] if e['action']=='correction']),1)

    def test_parameter_and_conditional_vip_questions_stay_in_knowledge_without_reads(self):
        for question in ('有人把fall=3与另一个失败计数3相乘，这个解释可靠吗？',
                         '如果接口有VIP、服务是active，还需要业务流量验证吗？'):
            with self.subTest(question=question):
                probe=Mock(side_effect=AssertionError('No device read allowed'))
                planner=Mock(return_value=action())
                with RunningApp(planner,probe) as client:
                    code,result=client.chat(question)
                    self.assertEqual(code,200)
                    self.assertEqual(result['status'],'answered',result)
                    self.assertEqual(client.store.list(),[])
                planner.assert_called_once()
                probe.assert_not_called()
        live=classify('远端主机当前fall=3，健康检查失败，请检查')
        self.assertEqual(live['route'],'diagnostic')

    def test_comparison_valid_citations_do_not_consume_correction(self):
        planner = Mock(return_value=action('既有文档说明watchdog做局部恢复；Keepalived负责角色与VIP。',
                                          ['KB-015','KB-013'], 'evidence'))
        result = self.execute('局端watchdog和Keepalived的职责有什么区别', planner)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['knowledge_status'], 'supported')
        planner.assert_called_once()

    def execute(self, question, planner, lookup=search):
        probe = Mock(side_effect=AssertionError('knowledge must never read'))
        with tempfile.TemporaryDirectory() as directory:
            result = run(question, [], planner, lookup, probe,
                         AuditStore(Path(directory)/'audit.db'), permitted_checks={},
                         question_policy={'mode': 'knowledge', 'domain': 'topology'})
        probe.assert_not_called()
        return result

    def test_irrelevant_hit_is_missing_even_with_valid_id(self):
        sources = [r for r in qa.RECORDS if r['id'] == 'KB-008']
        planner = Mock(return_value=action())
        r = self.execute('我们的新服务监听什么端口', planner, Mock(return_value=sources))
        self.assertEqual(r['knowledge_status'], 'missing')
        self.assertEqual(r['basis'], 'limited')
        self.assertIn('监听端口', r['answer'])
        self.assertIn('knowledge_sufficiency', planner.call_args[0][0])

    def test_wrong_evidence_is_corrected_once_into_limited_general_principle(self):
        sources = [r for r in qa.RECORDS if r['id'] == 'KB-002']
        planner = Mock(side_effect=[action('新服务监听9965。', ['KB-002'], 'evidence'), action()])
        r = self.execute('我们的新服务监听什么端口', planner, Mock(return_value=sources))
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['basis'], 'limited')
        self.assertNotIn('新服务监听9965', r['answer'])
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(planner.call_args[0][0]['correction_feedback']['valid_refs'], [])

    def test_second_invalid_response_is_stopped_and_no_wrong_conclusion_returned(self):
        planner = Mock(return_value=action('新服务监听9965。', ['KB-002'], 'evidence'))
        r = self.execute('我们的新服务监听什么端口', planner,
                         Mock(return_value=[s for s in qa.RECORDS if s['id'] == 'KB-002']))
        self.assertEqual(r['status'], 'incomplete')
        self.assertEqual(r['reason'], 'unsupported_knowledge_citation')
        self.assertEqual(planner.call_count, 2)
        self.assertNotIn('新服务监听9965', r['answer'])

    def test_covered_port_is_answered_with_direct_source(self):
        planner = Mock(return_value=action('既有配置记录TCP9965、UDP9966，不是当前监听观测。', ['KB-002'], 'evidence'))
        r = self.execute('Croc监听什么端口', planner)
        self.assertEqual(r['status'], 'answered', r)
        self.assertEqual(r['knowledge_status'], 'supported')
        self.assertEqual(r['basis'], 'evidence')
        planner.assert_called_once()

    def test_partial_answer_keeps_supported_part_and_specific_gap(self):
        planner = Mock(return_value=action('既有资料记录Croc端口；一般原理：重启前需确认角色和资源归属，具体命令未知。', ['KB-002']))
        r = self.execute('局端Croc监听什么端口，如何重启', planner)
        self.assertEqual(r['status'], 'answered', r)
        self.assertEqual(r['knowledge_status'], 'partial')
        self.assertEqual(r['basis'], 'limited')
        self.assertIn('操作步骤', r['answer'])
        self.assertIn('KB-002', r['refs'])

    def test_startup_source_cannot_authorize_uncovered_restart_instructions(self):
        planner = Mock(side_effect=[
            action('已有资料确认可直接重启Croc。', ['KB-009'], 'evidence'),
            action('资料说明正常HA下的启动关系；完整重启命令仍缺少依据。', ['KB-009'])])
        r = self.execute('远端Croc谁负责启动，手动重启步骤是什么', planner)
        self.assertEqual(r['status'], 'answered', r)
        self.assertEqual(r['knowledge_status'], 'partial')
        self.assertEqual(r['basis'], 'limited')
        self.assertEqual(planner.call_count, 2)
        self.assertNotIn('可直接重启Croc', r['answer'])
        self.assertIn('操作步骤', r['answer'])
        self.assertIn('KB-009', r['refs'])

    def test_search_can_fill_a_gap_without_changing_user_requirements(self):
        planner = Mock(side_effect=[({'action': 'search', 'query': 'Croc外层与内层端口'}, 0),
                                    action('Croc配置端口来自原文。', ['KB-002'], 'evidence')])
        source = [r for r in qa.RECORDS if r['id'] == 'KB-002']
        r = self.execute('Croc监听什么端口', planner, Mock(side_effect=[[], source]))
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['knowledge_status'], 'supported')

    def test_model_search_cannot_redirect_new_service_to_croc(self):
        planner = Mock(side_effect=[({'action': 'search', 'query': 'Croc端口'}, 0), action()])
        r = self.execute('我们的新服务监听什么端口', planner,
                         Mock(side_effect=[[], [s for s in qa.RECORDS if s['id'] == 'KB-002']]))
        self.assertEqual(r['knowledge_status'], 'missing')
        self.assertEqual(r['knowledge_sufficiency']['requirements'][0]['topic'], 'unknown_service')

    def test_knowledge_correction_does_not_grant_read_permission(self):
        planner = Mock(side_effect=[action('重启命令已经确认。', ['KB-009'], 'evidence'),
                                    ({'action': 'read', 'device': 'local-primary', 'check': 'services'}, 0)])
        r = self.execute('局端Croc如何重启', planner)
        self.assertEqual(r['status'], 'incomplete')
        self.assertEqual(r['events'][-1]['reason'], 'check_not_approved')

    def test_formal_http_returns_structured_gap_without_task_or_read(self):
        planner = Mock(return_value=action())
        probe = Mock()
        with RunningApp(planner, probe) as client:
            code, r = client.chat('我们的新服务监听什么端口')
            self.assertEqual(code, 200)
            self.assertEqual(r['status'], 'answered')
            self.assertEqual(r['knowledge_status'], 'missing')
            self.assertTrue(r['knowledge_sufficiency']['gaps'])
            self.assertEqual(client.store.list(), [])
        probe.assert_not_called()

    def test_followup_preserves_object_but_requires_new_kind_of_document(self):
        planner = Mock(side_effect=[action('既有配置记录Croc的端口。', ['KB-002'], 'evidence'), action()])
        probe = Mock()
        with RunningApp(planner, probe) as client:
            first = client.chat('局端Croc监听什么端口')[1]
            second = client.chat('那如何重启呢')[1]
            self.assertEqual(first['knowledge_status'], 'supported', first)
            self.assertEqual(second['status'], 'answered', second)
            self.assertEqual(second['knowledge_status'], 'partial')
            self.assertIn('procedure', [n['aspect'] for n in second['knowledge_sufficiency']['missing']])
            self.assertEqual(client.store.list(), [])
        probe.assert_not_called()

    def test_now_and_new_general_topic_keep_existing_boundaries(self):
        probe = Mock()
        with RunningApp(Mock(return_value=action()), probe) as client:
            client.chat('局端备机为什么不运行Croc')
            now = client.chat('那现在呢')[1]
            self.assertEqual(now['status'], 'current_unverified')
            general = client.chat('Python列表是什么')[1]
            self.assertEqual(general['route'], 'general')
            self.assertNotIn('knowledge_sufficiency', general)
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
