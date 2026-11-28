"""证据卡、检查目的和正式HTTP保留证据的回归；全部为合成替身。"""
import copy
import unittest
from unittest.mock import Mock
from agent.chat_model import ModelFailure
from devices.diagnostic_report import build_report, investigation_plan
from legacy.ha_croc_case import assess_case
from tests.test_topology_evidence import pair_observations, SCOPE
from tests.ha_case_test_support import case_probe, case_planner
from tests.workflow_test_support import RunningApp


class DiagnosticReportTests(unittest.TestCase):
    def build(self, rows=None, scope=None, remaining=0, terminal='answer_returned'):
        rows = pair_observations() if rows is None else rows
        scope = SCOPE if scope is None else scope
        return build_report(assess_case(rows, scope, remaining), rows, scope, remaining, terminal)

    def test_facts_have_one_original_ref_time_and_no_role_or_root_cause_promotion(self):
        report = self.build()
        self.assertEqual(len(report['facts']), 4)
        self.assertEqual({f['refs'][0] for f in report['facts']}, {'OBS-1','OBS-2','OBS-3','OBS-4'})
        self.assertTrue(all(f['collected_at'] and f['kind']=='parsed_observation' for f in report['facts']))
        self.assertTrue(all('角色' not in f['text'] and '正常' not in f['text'] for f in report['facts']))
        self.assertFalse(report['root_cause_confirmed'])
        self.assertFalse(report['model_answer_semantically_verified'])
        self.assertTrue(all(a['assumption'] for a in report['assessments']))

    def test_failed_stale_synthetic_duplicate_and_bad_time_do_not_become_confirmed_cards(self):
        for variant in ('failed','stale','synthetic','duplicate','bad_time'):
            rows=pair_observations()
            if variant=='failed': rows[0]['ok']=False
            elif variant=='stale': rows[0]['freshness']='stale'
            elif variant=='synthetic': rows[0]['origin']='synthetic'
            elif variant=='duplicate': rows.append(dict(rows[0]))
            else: rows[0]['collected_at']='not-a-time'
            with self.subTest(variant=variant):
                report=self.build(rows)
                self.assertNotIn('OBS-1',[f['refs'][0] for f in report['facts']])
                self.assertTrue(report['unknowns'])

    def test_missing_unit_retains_vip_fact_without_inventing_service_status(self):
        rows=pair_observations('missing_unit','nonowner_stopped')
        report=self.build(rows)
        local=[f for f in report['facts'] if f['device']=='local-primary']
        self.assertEqual([f['check'] for f in local],['interfaces'])
        self.assertIn('全部预期VIP',local[0]['text'])
        self.assertTrue(any('输出不足' in item for item in report['unknowns']))

    def test_single_target_report_does_not_disclose_other_target_evidence(self):
        scope={'local-primary':['interfaces']}
        report=self.build(scope=scope)
        self.assertEqual(len(report['facts']),1)
        self.assertEqual(report['facts'][0]['refs'],['OBS-2'])
        self.assertFalse(any(a['kind']=='sequential_pair_assessment' for a in report['assessments']))

    def test_far_apart_snapshots_keep_individual_facts_without_pair_assessment(self):
        rows=pair_observations()
        rows[-1]['collected_at']='2000-01-01T00:00:00Z'
        report=self.build(rows)
        self.assertEqual(len(report['facts']),4)
        self.assertFalse(any(a['kind']=='sequential_pair_assessment' for a in report['assessments']))

    def test_check_purpose_adapts_to_vip_service_mismatch_and_stays_in_scope(self):
        scope={'local-primary':['services','interfaces','logs']}
        rows=pair_observations('owner_stopped')[:2]
        original=copy.deepcopy(rows)
        case=assess_case(rows,scope,2)
        plan=investigation_plan(case,rows,scope,2)
        self.assertEqual([(c['device'],c['check']) for c in plan['next_checks']],[('local-primary','logs')])
        self.assertIn('持有预期VIP',plan['next_checks'][0]['purpose'])
        self.assertFalse(plan['grants_device_access'])
        self.assertEqual(rows,original)
        self.assertEqual(investigation_plan(case,rows,scope,0)['next_checks'],[])
        self.assertEqual(investigation_plan(case,rows,{'local-primary':['interfaces']},2)['next_checks'],[])

    def test_failed_check_is_not_recommended_again_and_terminal_plan_cannot_continue(self):
        rows=pair_observations('owner_stopped')[:2]
        rows.append(dict(rows[0],id='OBS-3',check='logs',ok=False))
        scope={'local-primary':['services','interfaces','logs']}
        report=self.build(rows,scope,1,'model_truncated')
        plan=report['investigation']
        self.assertEqual(plan['next_checks'],[])
        self.assertEqual(plan['attempted_failures'][0]['ref'],'OBS-3')
        self.assertTrue(plan['run_ended'])
        self.assertIn('新调查申请',plan['continuation_note'])
        self.assertFalse(plan['suggestions_execute_tools'])

    def test_http_cross_node_report_follows_approval_and_uses_exact_observations(self):
        reads=[]
        def probe(device,check):
            reads.append((device,check))
            return case_probe('owner_running' if device=='local-primary' else 'nonowner_stopped')(device,check)
        with RunningApp(case_planner,probe,lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('局端主机和备机当前Croc业务不通，比较VIP。')[1]['task']
            self.assertEqual(client.resume(task['id'])[0],409)
            self.assertFalse(reads)
            client.login();client.approve(task['id'])
            result=client.resume(task['id'])[1]['result']
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/tasks/'+task['id'],browser=other)[0],404)
        report=result['diagnostic_report']
        self.assertLessEqual(len(reads),task['execution_limits']['max_reads'])
        self.assertEqual(len(reads),len(set(reads)))
        self.assertTrue(all(check in task['scope'][device] for device,check in reads))
        self.assertEqual(len(report['facts']),4)
        self.assertEqual({f['refs'][0] for f in report['facts']},{'OBS-1','OBS-2','OBS-3','OBS-4'})
        self.assertEqual(len([a for a in report['assessments'] if a['kind']=='sequential_pair_assessment']),1)
        self.assertTrue(all(e.get('reason_source')=='host_check_contract' for e in result['events'] if e['action']=='read'))

    def test_vip_pair_logs_require_usable_evidence_scope_and_budget(self):
        rows = pair_observations('nonowner_stopped', 'nonowner_stopped')
        scope = dict(SCOPE)
        scope['local-primary'] = SCOPE['local-primary'] + ['logs']
        plan = self.build(rows, scope, 1, terminal=None)['investigation']
        choices = plan['next_checks']
        self.assertEqual([(c['device'], c['check']) for c in choices], [('local-primary', 'logs')])
        self.assertEqual(choices[0]['because_refs'], ['OBS-2', 'OBS-4'])
        self.assertIn('VIP', choices[0]['purpose'])

        failed_log = dict(rows[0], id='OBS-5', check='logs', ok=False)
        stale_peer = rows[:-1] + [dict(rows[-1], freshness='stale')]
        for label, evidence, approved, remaining in (
            ('unapproved', rows, SCOPE, 1),
            ('stale_peer', stale_peer, scope, 1),
            ('attempted', rows + [failed_log], scope, 1),
            ('no_budget', rows, scope, 0),
        ):
            with self.subTest(label=label):
                plan = self.build(evidence, approved, remaining, terminal=None)['investigation']
                self.assertEqual(plan['next_checks'], [])

    def test_http_missing_vips_can_continue_to_logs_under_the_same_approval(self):
        probe = Mock(side_effect=case_probe('nonowner_stopped'))
        with RunningApp(case_planner, probe, lookup=lambda q: []) as client:
            task = client.diagnostic_chat('局端主机和备机当前Croc业务不通，请核对VIP和日志。')[1]['task']
            self.assertEqual(client.resume(task['id'])[0], 409)
            probe.assert_not_called()
            client.login()
            client.approve(task['id'])
            completed = client.resume(task['id'])[1]

        self.assertEqual(completed['status'], 'completed')
        result = completed['result']
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['diagnostic_case']['pairs'][0]['finding'], 'resource_missing_observed')
        reads = [(item['device'], item['check']) for item in result['evidence']]
        self.assertCountEqual(reads[:4], [(device, check) for device in SCOPE for check in SCOPE[device]])
        self.assertCountEqual(reads[4:], [(device, 'logs') for device in SCOPE])
        self.assertEqual(probe.call_count, len(reads))
        self.assertLessEqual(len(reads), task['execution_limits']['max_reads'])
        self.assertFalse(result['diagnostic_report']['root_cause_confirmed'])

    def test_http_model_truncation_keeps_facts_and_does_not_retry_or_claim_completion(self):
        planner=Mock(side_effect=ModelFailure('model_truncated',usage=9))
        with RunningApp(planner,case_probe('owner_stopped'),lookup=lambda q:[],prefetch_overview=True) as client:
            task=client.diagnostic_chat('局端备机当前Croc业务不通')[1]['task']
            client.login();client.approve(task['id'])
            result=client.resume(task['id'])[1]['result']
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['reason'],'model_truncated')
        self.assertEqual(planner.call_count,1)
        self.assertEqual(len(result['diagnostic_report']['facts']),2)
        self.assertTrue(result['diagnostic_report']['investigation']['run_ended'])


if __name__=='__main__': unittest.main()
