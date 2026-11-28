"""组合检查整批校验、逐项预算与审计，以及失败及时收口。"""
if not __package__:
    import _bootstrap

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from chat_model import ModelFailure
from diagnostic_core import AuditStore
from unified_chat import run


def overview(device='remote-primary'):return ({'action':'read_overview','device':device},1)
def read(check,device='remote-primary'):return ({'action':'read','device':device,'check':check},1)
def answer(refs):return ({'action':'answer','answer':'局部观测已取得，业务与原因仍未核验。','refs':refs,'basis':'limited'},1)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.audit=AuditStore(Path(self.temp.name)/'a.db')
        self.probe=Mock(return_value={'ok':True,'output':'active','collected_at':'SIMULATION'})
    def tearDown(self):self.temp.cleanup()
    def execute(self,actions):
        self.planner=Mock(side_effect=actions)
        return run('远端主机当前角色',[],self.planner,Mock(return_value=[]),self.probe,self.audit)
    def test_overview_uses_two_reads_one_model_step_and_separate_obs(self):
        result=self.execute([overview(),answer(['OBS-1','OBS-2'])])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(self.probe.call_args_list[0][0],('remote-primary','services'))
        self.assertEqual(self.probe.call_args_list[1][0],('remote-primary','interfaces'))
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(self.planner.call_args_list[1][0][0]['remaining_reads'],2)
        self.assertEqual(len([r for r in self.audit.read(result['task']) if r[1]=='observation']),2)
    def test_arbitrary_check_or_command_rejected_before_probe(self):
        for extra in ({'checks':['routes']},{'command':'PRIVATE'},{'device':'router-1'}):
            self.probe.reset_mock()
            action={'action':'read_overview','device':'remote-primary'};action.update(extra)
            result=self.execute([(action,1)])
            self.probe.assert_not_called()
            self.assertEqual(result['status'],'incomplete')
            self.assertNotIn('PRIVATE',str(self.audit.read(result['task'])))
    def test_existing_one_check_rejects_entire_overview(self):
        result=self.execute([read('services'),overview()])
        self.assertEqual(self.probe.call_count,1)
        self.assertEqual(result['events'][-1]['reason'],'duplicate_read')
    def test_two_overviews_exhaust_original_four_read_limit(self):
        result=self.execute([overview(),overview('remote-backup'),read('routes')])
        self.assertEqual(self.probe.call_count,4)
        self.assertEqual(result['events'][-1]['reason'],'read_budget_reached')
    def test_insufficient_remaining_reads_execute_neither_check(self):
        result=self.execute([read('routes'),overview('remote-backup'),overview()])
        self.assertEqual(self.probe.call_count,3)
        self.assertEqual(result['events'][-1]['reason'],'read_budget_reached')
    def test_both_failed_overview_closes_without_another_model_call(self):
        self.probe.return_value={'ok':False,'output':'','error':'timeout','collected_at':'SIMULATION'}
        result=self.execute([overview(),answer([])])
        self.assertEqual(result['reason'],'device_evidence_unavailable')
        self.assertEqual(self.planner.call_count,1)
        self.assertEqual(self.probe.call_count,2)
        self.assertEqual(result['refs'],[])
        self.assertIn('不能证明设备损坏',result['answer'])
        self.assertIn('合成状态回放',result['answer'])
    def test_partial_success_retains_failed_check_and_allows_answer(self):
        self.probe.side_effect=[{'ok':True,'output':'active','collected_at':'SIMULATION'},
                                {'ok':False,'output':'','error':'timeout','collected_at':'SIMULATION'}]
        result=self.execute([overview(),answer(['OBS-1'])])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['refs'],['OBS-1'])
        self.assertEqual(result['evidence_scope']['failed_reads'],1)
    def test_model_timeout_digest_keeps_only_successful_references(self):
        self.probe.side_effect=[{'ok':True,'output':'PRIVATE RAW','collected_at':'SIMULATION'},
                                {'ok':False,'output':'','error':'timeout','collected_at':'SIMULATION'}]
        result=self.execute([overview(),ModelFailure('model_timeout')])
        self.assertEqual(result['refs'],['OBS-1'])
        self.assertIn('已取得',result['answer']);self.assertIn('未取得',result['answer'])
        self.assertNotIn('PRIVATE RAW',result['answer'])


if __name__=='__main__':unittest.main()
