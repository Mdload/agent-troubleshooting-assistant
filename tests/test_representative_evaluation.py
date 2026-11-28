"""新题评测工具不将真值传入模型，保持原运行权限与隔离。"""
if not __package__:
    import _bootstrap

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from evaluation.ha_model import isolated_io
from evaluation.representative import DEFAULT_SUITE, load_suite, run_case, run_suite, offline_planner


class RepresentativeEvaluationTests(unittest.TestCase):
    def test_preparation_never_reads_config_and_rejects_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'scope'
            loader = Mock(side_effect=AssertionError('No config read'))
            report = run_suite(DEFAULT_SUITE, output, ids=['A25'], config_loader=loader)
            loader.assert_not_called()
            self.assertEqual(report['status'], 'prepared')
            with self.assertRaises(FileExistsError): run_suite(DEFAULT_SUITE, output, ids=['A25'])

    def test_expected_answers_and_rubric_do_not_enter_planner_input(self):
        case = copy.deepcopy(next(c for c in load_suite(DEFAULT_SUITE)['cases'] if c['id'] == 'A25'))
        case['required_points'] = ['PRIVATE_RUBRIC_MARKER']
        planner = Mock(side_effect=offline_planner)
        with isolated_io() as state:
            row = run_case(case, [], planner, state)
        for context in row['model_inputs']:
            import json
            self.assertNotIn('PRIVATE_RUBRIC_MARKER', json.dumps(context))
            self.assertNotIn('expected_finding', context)
        self.assertEqual(row['host_finding'], 'owner_service_stopped')
        self.assertEqual(len(row['synthetic_reads']), 3)
        self.assertIsNone(row['semantic_review']['verdict'])

    def test_unapproved_cancelled_expired_and_stale_stop_without_extra_reads(self):
        cases = load_suite(DEFAULT_SUITE)['cases']
        for ident, reads in (('A32', 2), ('A34', 0), ('A35', 1), ('A36', 1)):
            case = next(c for c in cases if c['id'] == ident)
            with self.subTest(ident=ident), isolated_io() as state:
                row = run_case(case, [], offline_planner, state)
                self.assertTrue(all(row['automatic_checks'].values()), row['automatic_checks'])
                self.assertEqual(len(row['synthetic_reads']), reads)
                if ident != 'A32': self.assertEqual(row['adapter_calls'], [])
                if ident in ('A35','A36'):
                    self.assertEqual(row['result']['reason'],'investigation_inactive')
                    self.assertNotIn('引用未通过',row['result']['answer'])

    def test_setup_error_is_recorded_with_all_unexecuted_cases(self):
        loader = Mock(side_effect=ValueError('PRIVATE_CONFIG_DIAGNOSTIC'))
        with tempfile.TemporaryDirectory() as directory:
            report = run_suite(DEFAULT_SUITE, Path(directory)/'failed', 'model', ['A01'], loader)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(report['unexecuted_cases'], ['A01'])
            self.assertNotIn('PRIVATE_CONFIG_DIAGNOSTIC', str(report))


if __name__ == '__main__': unittest.main()
