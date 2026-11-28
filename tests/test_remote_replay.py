"""验证合成轨迹的引用、范围、分支和审计；不代表真实模型成绩。"""
if not __package__:
    import _bootstrap

import unittest
from evaluation.remote_replay import load_cases, replay


class RemoteReplayTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case['id']: case for case in load_cases()}

    def test_owner_existing_configuration_still_reports_missing_end_to_end_evidence(self):
        row = replay(self.cases['R01'])
        self.assertEqual(row['checks'], ['services', 'interfaces', 'routes'])
        self.assertIn('不能确认根因', row['result']['answer'])
        self.assertTrue(row['not_model_evaluation'])

    def test_owner_service_inconsistency_changes_followup_path(self):
        row = replay(self.cases['R02'])
        self.assertEqual(row['checks'], ['services', 'interfaces'])
        self.assertIn('不一致', row['result']['answer'])
        self.assertIn('不能确定', row['result']['answer'])

    def test_backup_stopped_is_not_declared_broken(self):
        row = replay(self.cases['R03'])
        self.assertIn('不能仅此判故障', row['result']['answer'])
        self.assertIn('合成状态回放', row['result']['answer'])
        self.assertEqual(row['result']['evidence_scope']['successful_reads'], 2)

    def test_offline_observation_cannot_be_cited_as_success(self):
        row = replay(self.cases['R04'])
        self.assertEqual(row['result']['refs'], [])
        self.assertEqual(row['result']['evidence_scope']['kind'], 'failed')
        self.assertEqual(row['result']['evidence_scope']['failed_reads'], 1)
        self.assertEqual(row['model_calls'], 0)
        self.assertEqual(row['live_device_reads'], 0)


if __name__ == '__main__':
    unittest.main()
