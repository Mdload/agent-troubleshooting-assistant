"""同一审批内的跨节点证据回放；替身数据不是实机验收或模型准确率。"""
import datetime
import unittest
from unittest.mock import Mock
from legacy.ha_croc_case import assess_case, assess_pairs, answer_issues
from tests.ha_case_test_support import case_probe, case_planner
from retrieval.topology_model import snapshot, peer_relation
from devices.diagnostic_report import build_report, investigation_plan
from devices.diagnostic_context import model_view
from tests.workflow_test_support import RunningApp


SCOPE={device:['services','interfaces'] for device in ('local-primary','local-backup')}


def pair_observations(first='owner_running',second='nonowner_stopped'):
    rows=[]
    now=datetime.datetime.utcnow().isoformat()+'Z'
    for device,variant in zip(SCOPE,(first,second)):
        for check in ('services','interfaces'):
            rows.append(dict(case_probe(variant)(device,check),device=device,check=check,
                id='OBS-'+str(len(rows)+1),origin='device_read',freshness='current',collected_at=now))
    return rows


class TopologyEvidenceTests(unittest.TestCase):
    def test_peer_relation_never_crosses_side_or_infers_inventory_owner(self):
        for device,peer in [('local-primary','local-backup'),('local-backup','local-primary'),
                            ('remote-primary','remote-backup'),('remote-backup','remote-primary')]:
            relation=peer_relation(device)
            self.assertEqual(relation['peer'],peer)
            self.assertFalse(relation['grants_device_access'])
            self.assertFalse(relation['configuration_verified_current'])
        self.assertIsNone(peer_relation('router-1'))
        self.assertIsNone(peer_relation('192.0.2.13'))

    def test_nonowner_evidence_prioritizes_approved_peer_interface_with_explicit_basis(self):
        rows=pair_observations('nonowner_stopped','owner_running')[:2]
        case=assess_case(rows,SCOPE,1)
        self.assertEqual(case['next_checks'],[{'device':'local-backup','check':'interfaces'}])
        need=case['cross_node_needs'][0]
        self.assertEqual(need['status'],'ready')
        self.assertEqual(need['because_refs'],['OBS-1','OBS-2'])
        self.assertEqual(need['refs'],[])
        plan=investigation_plan(case,rows,SCOPE,1)
        choice=plan['next_checks'][0]
        self.assertEqual(choice['relation_id'],'local_ha')
        self.assertEqual(choice['reason_source'],'host_topology_dependency')
        self.assertIn('已有批准范围',case['nodes'][0]['repair_advice'])
        self.assertNotIn('另行申请',case['nodes'][0]['repair_advice'])

    def test_peer_outside_scope_and_budget_exhaustion_are_distinct_without_access(self):
        rows=pair_observations('nonowner_stopped','owner_running')
        scope={'local-primary':['services','interfaces']}
        outside=assess_case(rows,scope,2)
        need=outside['cross_node_needs'][0]
        self.assertEqual(need['status'],'outside_scope')
        self.assertEqual(need['refs'],[])  # 注入的范围外观测也不作为对端依据。
        self.assertEqual(outside['next_checks'],[])
        exhausted=assess_case(rows[:2],SCOPE,0)
        self.assertEqual(exhausted['cross_node_needs'][0]['status'],'budget_exhausted')
        self.assertEqual(exhausted['next_checks'],[])
        report=build_report(exhausted,rows[:2],SCOPE,0,'answer_returned')
        self.assertTrue(any('预算已用完' in gap for gap in report['unknowns']))

    def test_attempted_unusable_peer_never_becomes_a_retry_recommendation(self):
        for variant in ('failed','stale','synthetic','duplicate','bad_time','far_time','unparseable'):
            rows=pair_observations('nonowner_stopped','owner_running')[:2]
            peer=pair_observations('nonowner_stopped','owner_running')[-1]
            if variant=='failed':peer['ok']=False
            elif variant=='stale':peer['freshness']='stale'
            elif variant=='synthetic':peer['origin']='synthetic'
            elif variant=='bad_time':peer['collected_at']='not-a-time'
            elif variant=='far_time':peer['collected_at']='2000-01-01T00:00:00Z'
            elif variant=='unparseable':peer['output']='eth9 UP'
            rows.append(peer)
            if variant=='duplicate':rows.append(dict(peer))
            with self.subTest(variant=variant):
                case=assess_case(rows,SCOPE,1)
                need=case['cross_node_needs'][0]
                self.assertEqual(need['status'],'evidence_unavailable')
                self.assertEqual(need['refs'],[])
                self.assertNotIn({'device':'local-backup','check':'interfaces'},case['next_checks'])
                report=build_report(case,rows,SCOPE,1,'answer_returned')
                self.assertTrue(any('不自动重试' in gap for gap in report['unknowns']))

    def test_available_peer_evidence_and_nontriggering_states_do_not_add_reads(self):
        rows=pair_observations('nonowner_stopped','nonowner_stopped')
        case=assess_case(rows,SCOPE,0)
        self.assertEqual([need['status'] for need in case['cross_node_needs']],['evidence_available']*2)
        self.assertEqual(case['next_checks'],[])
        self.assertEqual(case['pairs'][0]['finding'],'resource_missing_observed')
        for variant in ('owner_running','owner_stopped','missing_unit','read_failure'):
            with self.subTest(variant=variant):
                self.assertEqual(assess_case(pair_observations(variant)[:2],SCOPE,2)['cross_node_needs'],[])

    def test_http_nonowner_redirects_next_check_to_peer_under_same_approval(self):
        reads=[];contexts=[];order=[]
        def probe(device,check):
            reads.append((device,check))
            return case_probe('nonowner_stopped' if device==order[0] else 'owner_running')(device,check)
        def planner(context):
            contexts.append(model_view(context))
            return case_planner(context)
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('局端主机和备机当前Croc业务不通，核对VIP持有者。')[1]['task']
            order.extend(task['scope'])
            self.assertEqual(client.resume(task['id'])[0],409)
            self.assertEqual(reads,[])
            client.login();client.approve(task['id'])
            code,response=client.resume(task['id'])
            self.assertEqual(client.resume(task['id'])[0],409)
        result=response['result']
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'answered',result)
        self.assertEqual(reads[:4],[(order[0],'services'),(order[0],'interfaces'),
                                (order[1],'interfaces'),(order[1],'services')])
        self.assertLessEqual(len(reads),task['execution_limits']['max_reads'])
        self.assertEqual(len(reads),len(set(reads)))
        self.assertTrue(all(check in task['scope'][device] for device,check in reads))
        plan=contexts[1]['investigation_plan']
        self.assertEqual(plan['next_checks'][0]['check'],'interfaces')
        self.assertEqual(plan['cross_node_needs'][0]['because_refs'],['OBS-1','OBS-2'])
        self.assertNotIn('cross_node_needs',contexts[1]['diagnostic_case'])
        self.assertEqual(len(result['refs']),4)
        pair=result['diagnostic_case']['pairs'][0]
        self.assertTrue(all(resource['observed_holders']==[order[1]] for resource in pair['resources']))
        self.assertFalse(result['diagnostic_report']['root_cause_confirmed'])

    def test_registry_projection_has_unknown_roles_versions_and_no_access_grant(self):
        data=snapshot()
        self.assertEqual(len(data['ha_resource_groups']),2)
        self.assertEqual({node['current_role'] for node in data['nodes']},{'unknown'})
        self.assertTrue(all(node['software_version'] is None for node in data['nodes']))
        self.assertFalse(data['grants_device_access'])
        self.assertFalse(data['current_state_verified'])
        data['ha_resource_groups'][0]['expected_vips'].clear()
        self.assertTrue(snapshot()['ha_resource_groups'][0]['expected_vips'])

    def test_owner_follows_observed_vips_instead_of_inventory_primary_label(self):
        observations=pair_observations('nonowner_stopped','owner_running')
        report=assess_case(observations,SCOPE,0)
        pair=report['pairs'][0]
        self.assertEqual(pair['finding'],'single_resource_holder_observed')
        self.assertTrue(all(resource['observed_holders']==['local-backup'] for resource in pair['resources']))
        self.assertEqual(pair['refs'],['OBS-2','OBS-4'])
        self.assertEqual(set(report['required_refs']),{'OBS-1','OBS-2','OBS-3','OBS-4'})
        self.assertEqual(report['next_checks'],[])
        self.assertFalse(pair['simultaneous_snapshot'])
        self.assertFalse(pair['vrrp_role_verified'])

    def test_overlapping_missing_and_split_resources_remain_observed_patterns(self):
        for first,second,finding in [('owner_running','owner_running','resource_overlap_observed'),
            ('nonowner_stopped','nonowner_stopped','resource_missing_observed')]:
            with self.subTest(finding=finding):
                pair=assess_pairs(pair_observations(first,second),SCOPE)[0]
                self.assertEqual(pair['finding'],finding)
                self.assertFalse(pair['root_cause_confirmed'])
        rows=pair_observations('partial_vip','partial_vip')
        rows[-1]['output']='eth2 UP\neth3 UP 172.27.10.37/24'
        self.assertEqual(assess_pairs(rows,SCOPE)[0]['finding'],'resource_distribution_split')

    def test_missing_failed_stale_synthetic_or_duplicate_interface_is_not_a_pair_fact(self):
        for kind in ('missing','failed','stale','synthetic','duplicate'):
            rows=pair_observations()
            if kind=='missing':rows.pop()
            elif kind=='failed':rows[-1]['ok']=False
            elif kind=='stale':rows[-1]['freshness']='stale'
            elif kind=='synthetic':rows[-1]['origin']='synthetic'
            else:rows.append(dict(rows[-1]))
            with self.subTest(kind=kind):
                pair=assess_pairs(rows,SCOPE)[0]
                self.assertEqual(pair['finding'],'insufficient')
                self.assertFalse(pair['evidence_complete'])
                self.assertEqual(pair['resources'],[])

    def test_pair_requires_approved_same_side_interfaces_and_does_not_add_reads(self):
        rows=pair_observations()
        for scope in ({'local-primary':['interfaces']},
            {'local-primary':['interfaces'],'local-backup':['services']},
            {'local-primary':['interfaces'],'remote-backup':['interfaces']}):
            self.assertEqual(assess_pairs(rows,scope),[])
        self.assertEqual(assess_case(rows,{'local-primary':['services','interfaces']},0)['next_checks'],[])

    def test_time_skew_or_wrong_interface_keeps_distribution_unknown(self):
        rows=pair_observations()
        rows[-1]['collected_at']=(datetime.datetime.utcnow()-datetime.timedelta(seconds=61)).isoformat()+'Z'
        pair=assess_pairs(rows,SCOPE)[0]
        self.assertEqual(pair['finding'],'collection_window_exceeded')
        self.assertFalse(pair['evidence_complete'])
        for output in ('eth2 UP\neth3 UP\neth9 UP 192.0.2.15/24',
                       'eth2 UP\neth2 UP 192.0.2.15/24\neth3 UP'):
            rows=pair_observations();rows[-1]['output']=output
            self.assertEqual(assess_pairs(rows,SCOPE)[0]['finding'],'insufficient')

    def test_pair_explanation_requires_both_observations_in_final_citations(self):
        rows=[row for row in pair_observations() if row['check']=='interfaces']
        report=assess_case(rows,SCOPE,0)
        self.assertTrue(answer_issues(report,'采集时VIP分布符合单持局部预期。',['OBS-2']))
        self.assertEqual(answer_issues(report,'采集时VIP分布符合单持局部预期；完整根因和业务未知。',['OBS-2','OBS-4']),[])

    def test_sequential_snapshots_cannot_confirm_simultaneous_dual_owner_or_split_brain(self):
        report=assess_case(pair_observations('owner_running','owner_running'),SCOPE,0)
        refs=report['required_refs']
        for text in ('已经确认脑裂。','两台同时持有VIP。'):
            self.assertIn('ha_pair_nonatomic',[issue['id'] for issue in answer_issues(report,text,refs)])
        for text in ('重复VIP需要复核，不能确认脑裂。','两次观测可能跨越切换，无法确认两台同时持有VIP。',
                     '示例 `已经确认脑裂` 缺少证据，不采用。','能确认脑裂吗？'):
            self.assertEqual(answer_issues(report,text,refs),[])

    def test_formal_http_two_nodes_follow_requester_approval_and_frozen_limits(self):
        reads=[]
        def probe(device,check):
            reads.append((device,check))
            return case_probe('owner_stopped' if device=='local-primary' else 'nonowner_stopped')(device,check)
        planner=Mock(side_effect=case_planner)
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            code,response=client.diagnostic_chat('局端主机和备机当前Croc业务不通，请核对VIP分布。')
            self.assertEqual(code,200)
            task=response['task']
            self.assertEqual(set(task['scope']),set(SCOPE))
            self.assertEqual(client.resume(task['id'])[0],409)
            self.assertEqual(reads,[])
            client.login();client.approve(task['id'])
            code,response=client.resume(task['id'])
        result=response['result']
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'answered',result)
        self.assertTrue({(device,check) for device in SCOPE for check in ('services','interfaces')}<=set(reads))
        self.assertLessEqual(len(reads),task['execution_limits']['max_reads'])
        self.assertEqual(len(reads),len(set(reads)))
        self.assertTrue(all(check in task['scope'][device] for device,check in reads))
        self.assertEqual(result['diagnostic_case']['pairs'][0]['finding'],'single_resource_holder_observed')
        self.assertEqual(len(result['refs']),4)
        self.assertFalse(result['diagnostic_case']['root_cause_confirmed'])


if __name__=='__main__':unittest.main()
