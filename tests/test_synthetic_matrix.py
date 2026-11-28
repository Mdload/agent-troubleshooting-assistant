"""题库缺项在外发前发现；跨设备与全失败轨迹可离线回放。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from devices.device_tools import DEVICES,CHECKS
from devices.diagnostic_core import AuditStore
from legacy.unified_chat import run
from evaluation.synthetic_matrix import validate_cases,make_probe,BASE_CHECKS

CASES=json.loads((Path(__file__).resolve().parents[1]/'evaluation/cross-device-fixtures.json').read_text(encoding='utf-8'))['cases']


class SyntheticMatrixTests(unittest.TestCase):
    def test_all_cells_present_and_return_independent_synthetic_observations(self):
        validate_cases(CASES)
        for case in CASES:
            probe=make_probe(case)
            for device in DEVICES:
                for check in BASE_CHECKS:
                    item=probe(device,check)
                    self.assertEqual(item['collected_at'],'SIMULATION')
                    self.assertNotEqual(item.get('error'),'synthetic_fixture_unavailable')
                    item['output']='modified'
                    self.assertNotEqual(probe(device,check)['output'],'modified')
                with self.assertRaisesRegex(ValueError,'Synthetic check not provided'):
                    probe(device,'logs')

    def test_missing_cell_rejected_before_any_planning(self):
        case=copy.deepcopy(CASES[0]);del case['matrix']['local-primary']['interfaces']
        with self.assertRaisesRegex(ValueError,'Incomplete check'): make_probe(case)

    def execute(self,case,actions):
        with tempfile.TemporaryDirectory() as tmp:
            return run(case['turns'][0],[],Mock(side_effect=actions),Mock(return_value=[]),
                       make_probe(case),AuditStore(Path(tmp)/'a.db'))

    def test_related_cross_device_sequence_has_no_missing_fixture(self):
        result=self.execute(CASES[1],[
            ({'action':'read','device':'local-primary','check':'services'},1),
            ({'action':'read','device':'remote-primary','check':'interfaces'},1),
            ({'action':'answer','answer':'已取得局部指标，仍缺端到端业务证据。','refs':['OBS-1','OBS-2'],'basis':'limited'},1)])
        self.assertEqual(result['status'],'answered')
        self.assertTrue(all(o['ok'] for o in result['evidence']))
        self.assertIn('合成状态回放',result['answer'])

    def test_all_timeout_case_can_close_with_unknown_status(self):
        case=CASES[3]
        self.assertTrue(all(not x['ok'] and x['error']=='timeout' for checks in case['matrix'].values() for x in checks.values()))
        result=self.execute(case,[
            ({'action':'read','device':'remote-primary','check':'services'},1),
            ({'action':'answer','answer':'读取超时，状态未核实，不能据此断言设备损坏。','refs':[],'basis':'limited'},1)])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['refs'],[])
        self.assertEqual(result['evidence_scope']['successful_reads'],0)
        self.assertTrue(result['answer'].startswith('以下为合成状态回放'))


if __name__=='__main__': unittest.main()
