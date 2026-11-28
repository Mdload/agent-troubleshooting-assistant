"""跨调查历史快照、版本/时间边界和正式HTTP归属；全部为替身。"""
import copy
import unittest
from unittest.mock import Mock
from devices.diagnostic_report import build_report
from legacy.ha_croc_case import assess_case
from tests.ha_case_test_support import case_probe,case_planner
from tests.test_topology_evidence import pair_observations,SCOPE
from devices.snapshot_comparison import compare_snapshots,object_fingerprint
from tests.workflow_test_support import RunningApp


def task(ident,variant,stamp):
    rows=pair_observations(variant)[:2]
    for row in rows:row['collected_at']=stamp
    scope={'local-primary':['services','interfaces']}
    report=build_report(assess_case(rows,scope,0),rows,scope,0,'answer_returned')
    return {'id':ident,'conversation_id':'comparison','scene':'ha_topology','scope':scope,'status':'completed',
            'result':{'evidence':rows,'diagnostic_report':report}}


class SnapshotComparisonTests(unittest.TestCase):
    def setUp(self):
        self.before=task('a'*32,'nonowner_stopped','2026-10-18T10:00:00Z')
        self.after=task('b'*32,'owner_running','2026-10-19T10:00:00Z')

    def test_changes_have_two_times_and_refs_but_cannot_claim_current_or_recovered(self):
        before,after=copy.deepcopy(self.before),copy.deepcopy(self.after)
        result=compare_snapshots(before,after)
        self.assertEqual(result['changed_fields'],2)
        self.assertEqual({r['check'] for r in result['entries']},{'services','interfaces'})
        self.assertTrue(all(r['before']['task_id']!=r['after']['task_id'] for r in result['entries']))
        self.assertEqual((before,after),(self.before,self.after))
        for key in ('current_state_verified','recovery_verified','root_cause_confirmed','grants_device_access'):
            self.assertFalse(result[key])

    def test_registry_fingerprint_changes_with_identity_or_expected_object_not_inventory_role(self):
        from unittest.mock import patch
        baseline=object_fingerprint('local-primary','interfaces')
        from devices.device_registry import DEVICES,PROFILES
        with patch.dict(DEVICES['local-primary'],hostname='different-identity'):
            self.assertNotEqual(object_fingerprint('local-primary','interfaces'),baseline)
        with patch.dict(PROFILES['local_ha']['ha_vips'],eth2='192.0.2.99'):
            self.assertNotEqual(object_fingerprint('local-primary','interfaces'),baseline)
        self.assertEqual(object_fingerprint('local-primary','interfaces'),baseline)

    def test_legacy_or_changed_definition_or_missing_state_is_not_called_a_change(self):
        for variant in ('legacy','definition_changed','stale','failed','duplicate','fact_duplicate','mixed_fact_duplicate','missing'):
            after=copy.deepcopy(self.after)
            facts=after['result']['diagnostic_report']['facts']
            observations=after['result']['evidence']
            if variant=='legacy':
                for f in facts:f.pop('object_fingerprint')
            elif variant=='definition_changed':
                for f in facts:f['object_fingerprint']='changed'
            elif variant=='stale':
                for o in observations:o['freshness']='stale'
            elif variant=='failed':
                for o in observations:o['ok']=False
            elif variant=='duplicate':observations.extend(copy.deepcopy(observations))
            elif variant in ('fact_duplicate','mixed_fact_duplicate'):
                repeated=copy.deepcopy(facts)
                if variant=='mixed_fact_duplicate':
                    for f in facts:f.pop('object_fingerprint')
                facts.extend(repeated)
            else:after['result']['evidence']=[]
            with self.subTest(variant=variant):
                result=compare_snapshots(self.before,after)
                self.assertEqual(result['comparable_fields'],0)
                self.assertTrue(all(r['status']=='unavailable' for r in result['entries']))

    def test_reverse_or_equal_collection_time_is_unavailable_not_recovery(self):
        for stamp in ('2026-10-18T09:59:59Z','2026-10-18T10:00:00Z'):
            after=task('b'*32,'owner_running',stamp)
            result=compare_snapshots(self.before,after)
            self.assertEqual(result['changed_fields'],0)
            self.assertTrue(all(r['reason']=='time_not_increasing' for r in result['entries']))

    def test_scene_conversation_running_and_identical_task_are_rejected(self):
        for key,value in (('scene','croc_local'),('conversation_id','other'),('status','running'),('id',self.before['id'])):
            after=copy.deepcopy(self.after);after[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):compare_snapshots(self.before,after)

    def test_missing_checks_and_different_devices_are_not_added_to_common_scope(self):
        after=copy.deepcopy(self.after);after['scope']={'local-backup':['services','interfaces']}
        result=compare_snapshots(self.before,after)
        self.assertEqual(result['entries'],[])

    def test_unchanged_values_still_have_distinct_collection_time_and_raw_outputs(self):
        after=task('b'*32,'nonowner_stopped','2026-10-19T10:00:00Z')
        result=compare_snapshots(self.before,after)
        self.assertEqual(result['changed_fields'],0)
        self.assertEqual(result['comparable_fields'],2)
        self.assertTrue(all(r['status']=='unchanged' and r['before']['raw_output'] for r in result['entries']))

    def test_formal_http_checks_two_owners_and_rejects_forged_owner_and_missing_csrf(self):
        state={'variant':'nonowner_stopped'};reads=[]
        def probe(device,check):
            reads.append((device,check));return case_probe(state['variant'])(device,check)
        planner=Mock(side_effect=case_planner)
        with RunningApp(planner,probe,lookup=lambda q:[],prefetch_overview=True) as client:
            ids=[]
            for variant in ('nonowner_stopped','owner_running'):
                state['variant']=variant
                requested=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task']
                client.login();client.approve(requested['id']);client.resume(requested['id']);ids.append(requested['id'])
            before_calls=planner.call_count;before_reads=len(reads)
            # 单独验证归属/CSRF拒绝；此前两次完整调查已接近每分钟POST限流。
            import app
            payload={'before_task':ids[0],'after_task':ids[1]}
            code,result=client.call('/api/task-comparison',payload)
            self.assertEqual(code,200,result)
            self.assertEqual(result['changed_fields'],2)
            self.assertEqual(planner.call_count,before_calls);self.assertEqual(len(reads),before_reads)
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/task-comparison',payload,browser=other)[0],404)
            self.assertEqual(client.call('/api/task-comparison',dict(payload,owner='forged'))[0],400)
            self.assertEqual(client.call('/api/task-comparison',payload,token=False)[0],403)
            self.assertEqual(client.call('/api/task-comparison',dict(payload,before_task='bad'))[0],409)
            self.assertEqual(client.call('/api/task-comparison',dict(payload,before_task=ids[1]))[0],409)
            self.assertEqual(client.call('/api/tasks/'+ids[0])[1]['attempts'],2)
            self.assertEqual(client.call('/api/tasks/'+ids[1])[1]['attempts'],2)


if __name__=='__main__':unittest.main()
