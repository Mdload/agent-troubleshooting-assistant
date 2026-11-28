"""来源限定、已复现矛盾、正常表达、共享纠正预算与失败留痕的离线回归。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent.chat_model import ModelFailure
from devices.diagnostic_core import AuditStore
from legacy.mechanism_guard import mechanism_constraints, contradicted_constraints
from legacy.unified_chat import run, search


def final(text, usage=7):
    return {'action': 'answer', 'answer': text, 'refs': ['KB-013', 'KB-014'], 'basis': 'evidence'}, usage


class MechanismGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = AuditStore(Path(self.temp.name) / 'audit.db')
        sources = search('局端健康检查失败后切换规则')
        self.sources = [source for source in sources if source['id'] in ('KB-013', 'KB-014')]
        self.assertEqual({source['id'] for source in self.sources}, {'KB-013', 'KB-014'})
        self.constraints = mechanism_constraints(self.sources)
        self.lookup = Mock(return_value=self.sources)
        self.probe = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, actions, question='局端切换规则', clock=None):
        self.planner = Mock(side_effect=actions)
        kwargs = {'clock': clock} if clock is not None else {}
        return run(question, [], self.planner, self.lookup, self.probe, self.audit, **kwargs)

    def test_constraints_require_source_content_and_scope(self):
        self.assertEqual(len(self.constraints), 4)
        self.assertEqual(mechanism_constraints([{'id': 'KB-013'}]), [])
        for group in ('remote', 'router'):
            self.assertEqual(mechanism_constraints(self.sources, group), [])
        for field, value in [('kind', 'historical_case'), ('operating_modes', ['fixed_primary_test']), ('device_groups', ['remote'])]:
            changed = [dict(source, **{field: value}) for source in self.sources]
            self.assertEqual(mechanism_constraints(changed), [])

    def test_recorded_false_claims_are_detected(self):
        examples = [
            ('两台都健康失败时，即使VIP漂到了某一台，这些门禁不通过，业务照样不通。', 'local_business'),
            ('notify脚本负责对端Croc启停。', 'local_notify'),
            ('备机只有自身健康才具备足够优先级接管。', 'local_election'),
        ]
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertIn(expected, [item['id'] for item in contradicted_constraints(text, self.constraints)])

    def test_other_affirmative_wordings_are_detected(self):
        examples = [
            ('备用节点必须保持健康方能获得接管资格。', 'local_election'),
            ('健康是备机接管的必要条件。', 'local_election'),
            ('notify负责启动另一台节点的Croc。', 'local_notify'),
            ('双机都不健康，因此肯定业务不通。', 'local_business'),
            ('track_interface故障一律只是降低优先级。', 'local_interface'),
        ]
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertIn(expected, [item['id'] for item in contradicted_constraints(text, self.constraints)])

    def test_supported_conditions_negations_and_questions_pass(self):
        examples = [
            '主机降为90，健康备机100高于90，因此可能接管。',
            '健康检查失败不能保证业务可用，需要具体失败项和路径证据。',
            '不能说notify负责对端Croc启停；它控制本机。',
            'notify不负责对端Croc启停。',
            '备机不是只有自身健康才有接管资格。',
            '两台不健康不代表业务一定不通。',
            'notify负责对端Croc启停吗？',
            '接口跟踪故障依配置可能进入FAULT或降权。',
            '错误说法是“健康是备机接管的必要条件”。',
            '仅降权的候选在对端退出时仍可能持有VIP，业务需另验。',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(contradicted_constraints(text, self.constraints), [])

    def test_unsupported_sources_do_not_claim_validation(self):
        self.assertEqual(contradicted_constraints('notify负责对端Croc启停。', []), [])

    def test_aggregate_health_is_not_a_universal_business_gate(self):
        bad = '成为owner后要求服务、进程、tun16、监听与路由同时满足，任一项失败，即使持有VIP业务也不通。'
        self.assertIn('local_business', [item['id'] for item in contradicted_constraints(bad, self.constraints)])
        for text in ('任一项失败只能报告这一项未通过，不能推出业务不通。',
                     '若用于本次业务的Croc服务停止，相关隧道业务会受影响。',
                     '这些环节是关键路径，任一环节失败，对应业务不通。',
                     '任一项失败时，仅说明相关业务受到影响，不能推成所有业务中断。',
                     '某项路由检查未通过，其他目标业务是否通需要另测。'):
            self.assertEqual(contradicted_constraints(text, self.constraints), [])

    def test_correction_contains_source_constraints_not_bad_answer(self):
        invalid = 'PRIVATE notify脚本负责对端Croc启停。'
        result = self.execute([final(invalid), final('各节点notify只启停本机Croc，业务仍需验证。')])
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(self.planner.call_count, 2)
        self.assertEqual(result['total_tokens'], 14)
        self.probe.assert_not_called()
        self.lookup.assert_called_once()
        context = self.planner.call_args_list[1][0][0]
        self.assertEqual(context['remaining_corrections'], 0)
        self.assertEqual(context['correction_feedback']['code'], 'unsupported_mechanism_claim')
        self.assertEqual(context['correction_feedback']['constraints'][0]['refs'], ['KB-013'])
        self.assertNotIn('PRIVATE', json.dumps(context))
        self.assertNotIn('PRIVATE', json.dumps(self.audit.read(result['task'])))
        self.assertEqual(result['events'][-1]['constraints'], ['local_notify'])

    def test_repeated_conflict_stops_without_returning_bad_answer(self):
        text = 'notify负责对端Croc启停。'
        result = self.execute([final(text), final(text), final('unused')])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'unsupported_mechanism_claim')
        self.assertEqual(self.planner.call_count, 2)
        self.assertNotIn(text, result['answer'])
        self.assertEqual(result['total_tokens'], 14)

    def test_protocol_and_mechanism_share_one_correction(self):
        result = self.execute([ModelFailure('model_invalid_json', 3), final('健康是备机接管的必要条件。'), final('unused')])
        self.assertEqual(result['reason'], 'unsupported_mechanism_claim')
        self.assertEqual(self.planner.call_count, 2)
        self.assertEqual(result['total_tokens'], 10)
        self.assertEqual(sum(event['action'] == 'correction' for event in result['events']), 1)

    def test_deadline_does_not_expand_for_semantic_correction(self):
        result = self.execute([final('notify负责对端Croc启停。'), final('unused')],
                              clock=Mock(side_effect=[0, 0, 1, 181]))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(self.planner.call_count, 1)

    def test_remote_question_does_not_inherit_local_constraints(self):
        # 故意返回局端资料，scope仍以用户设备范围为准。
        result = self.execute([final('notify负责对端Croc启停。')], question='远端notify规则是什么？')
        self.assertEqual(self.planner.call_args[0][0]['mechanism_constraints'], [])
        self.assertEqual(result['status'], 'answered')

    def test_general_answer_uses_no_topology_constraint(self):
        self.lookup.return_value = []
        planner = Mock(return_value=({'action': 'answer', 'answer': '元组成员都可哈希时才可作为键。', 'refs': [], 'basis': 'general'}, 2))
        result = run('Python元组作为字典键有什么要求？', [], planner, self.lookup, self.probe, self.audit)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(planner.call_args[0][0]['mechanism_constraints'], [])
        self.lookup.assert_not_called()

    def test_fake_device_still_rejected_without_correction(self):
        result = self.execute([({'action': 'read', 'device': 'unregistered-device', 'check': 'services'}, 3), final('unused')])
        self.assertEqual(self.planner.call_count, 1)
        self.assertEqual(result['status'], 'incomplete')
        self.probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
