"""已复现越界结论、非断言表达、共享一次纠正和证据留存回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent.chat_model import ModelFailure
from devices.diagnostic_core import AuditStore
from legacy.mechanism_guard import observation_constraints, contradicted_constraints
from legacy.unified_chat import run


def final(text):
    return {'action': 'answer', 'answer': text, 'refs': ['OBS-1', 'OBS-2'], 'basis': 'limited'}, 3


class ObservationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = AuditStore(Path(self.temp.name) / 'audit.db')
        self.observations = [{'id': 'OBS-1', 'device': 'remote-primary', 'check': 'interfaces',
                              'ok': True, 'origin': 'synthetic'}]
        self.constraints = observation_constraints(self.observations)
        self.probe = Mock(return_value={'ok': True, 'output': 'PRIVATE fixture UP ESTAB',
                                       'collected_at': 'SIMULATION'})
        self.lookup = Mock(return_value=[])

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, following):
        self.planner = Mock(side_effect=[({'action': 'read_overview', 'device': 'remote-primary'}, 2)] + following)
        return run('远端能ping通但业务不通，请检查状态。', [], self.planner, self.lookup, self.probe, self.audit)

    def test_only_successful_registered_snapshot_generates_constraints(self):
        self.assertEqual(self.constraints[0]['refs'], ['OBS-1'])
        for field, value in [('ok', False), ('device', 'router-1'), ('check', 'business')]:
            self.assertEqual(observation_constraints([dict(self.observations[0], **{field: value})]), [])
        self.assertEqual(observation_constraints([]), [])

    def test_recorded_overclaim_and_exclusion_wordings_are_detected(self):
        for text in ['即外层Croc隧道两端建立正常。',
                     '合成观测下外层隧道正常，业务不通的可能位置收敛到内层路由与端到端路径。',
                     '隧道整体正常，但不能证明业务可用。',
                     '可以排除外层接入问题。',
                     '故障已锁定到内层路由。']:
            with self.subTest(text=text):
                self.assertEqual([r['id'] for r in contradicted_constraints(text, self.constraints)], ['snapshot_tunnel'])

    def test_states_negations_questions_and_hypotheses_are_not_assertions(self):
        for text in ['观测到tun16 UP与TCP ESTAB，未验证隧道数据路径或业务。',
                     '不能证明外层Croc隧道两端建立正常。',
                     '不应说隧道正常；局部状态不足以验证转发。',
                     '没有证据排除外层问题，内层路由需要下一步检查。',
                     '外层隧道正常吗？',
                     '错误示例：外层隧道正常。',
                     '如果另行完成了外层验收，故障才可能收敛到内层。',
                     '状态不一致，回调失败只是假设，不排除更早的无VIP跳过。']:
            with self.subTest(text=text):
                self.assertEqual(contradicted_constraints(text, self.constraints), [])

    def test_code_literals_are_not_treated_as_diagnosis(self):
        text = '字符串示例 `print("隧道正常")`。\n```python\nmessage="隧道整体正常"\n```\n现场数据路径未验证。'
        self.assertEqual(contradicted_constraints(text, self.constraints), [])

    def test_correction_retains_observations_without_echoing_bad_answer(self):
        result = self.execute([final('PRIVATE answer: 外层Croc隧道两端建立正常。'),
                               final('仅观测到服务状态和接口/连接，不能验证隧道数据路径，路由与业务仍未查。')])
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(self.planner.call_count, 3)
        self.assertEqual(self.probe.call_count, 2)
        context = self.planner.call_args_list[2][0][0]
        self.assertEqual(context['remaining_corrections'], 0)
        self.assertEqual(context['correction_feedback']['code'], 'unsupported_observation_claim')
        self.assertEqual(context['correction_feedback']['constraints'][0]['refs'], ['OBS-1', 'OBS-2'])
        self.assertNotIn('PRIVATE answer', str(context))
        self.assertNotIn('PRIVATE answer', str(self.audit.read(result['task'])))
        self.assertEqual(len(result['evidence']), 2)

    def test_repeat_overclaim_stops_and_keeps_good_evidence(self):
        bad = final('Croc隧道整体正常。')
        result = self.execute([bad, bad, final('unused')])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'unsupported_observation_claim')
        self.assertEqual(self.planner.call_count, 3)
        self.assertEqual(result['refs'], ['OBS-1', 'OBS-2'])
        self.assertIn('已取得', result['answer'])
        self.assertNotIn('Croc隧道整体正常', result['answer'])

    def test_observation_and_protocol_share_one_correction(self):
        result = self.execute([ModelFailure('model_invalid_json', 1), final('Croc隧道整体正常。'), final('unused')])
        self.assertEqual(result['reason'], 'unsupported_observation_claim')
        self.assertEqual(self.planner.call_count, 3)
        self.assertEqual(sum(e['action'] == 'correction' for e in result['events']), 1)

    def test_no_reads_does_not_claim_this_guard_verified_answer(self):
        planner = Mock(return_value=({'action': 'answer', 'answer': '隧道正常吗需要相关观测。',
                                     'refs': [], 'basis': 'general'}, 1))
        result = run('Python字符串示例', [], planner, self.lookup, self.probe, self.audit)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(planner.call_args[0][0]['observation_constraints'], [])
        self.probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
