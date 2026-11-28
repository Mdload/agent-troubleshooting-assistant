"""正常/异常/证据缺失分支与真实HTTP接线；全部为测试替身。"""
import datetime
import unittest
from unittest.mock import Mock
from legacy.ha_croc_case import selected,assess_case,croc_state,vip_state,answer_issues,stop_cause_overclaim
from tests.ha_case_test_support import VARIANTS,case_probe,case_planner
from tests.workflow_test_support import RunningApp


def observation(variant,check,number=1,device='local-backup'):
    return dict(case_probe(variant)(device,check),device=device,check=check,id='OBS-'+str(number),origin='device_read',freshness='current')


class CaseRuleTests(unittest.TestCase):
    def test_negated_recovery_statement_is_not_a_positive_recovery_claim(self):
        report=self.report('partial_vip')
        for text in ('不能据此判定整机健康或业务已恢复。','没有证据确认业务已恢复。',
                     '业务已恢复吗？','如果业务已恢复，再做端到端验收。',
                     '示例 `业务已恢复` 不能作为现场结论。'):
            self.assertEqual(answer_issues(report,text,['OBS-1','OBS-2']),[],text)
        for text in ('业务已恢复。','不能确认根因，但业务已恢复。'):
            self.assertIn('ha_case_limits',[x['id'] for x in answer_issues(report,text,['OBS-1','OBS-2'])])

    def test_observed_owner_vip_cannot_be_denied_when_service_unit_is_missing(self):
        report=self.report('missing_unit')
        self.assertEqual(report['nodes'][0]['vip_role'],'owner')
        self.assertEqual(report['nodes'][0]['croc_service'],'unknown')
        bad='服务情况可确认，未观测到VIP，tun16缺失。'
        self.assertIn('ha_observed_vip',[issue['id'] for issue in answer_issues(report,bad,['OBS-1','OBS-2'])])
        for text in ('接口已出现预期VIP；登记单元未确认，VRRP角色和全部进程状态未知。',
                     '一般规则：如果未观测到VIP，应另行核对角色。','未观测到VIP吗？'):
            self.assertEqual(answer_issues(report,text,['OBS-1','OBS-2']),[])

    def test_stop_reason_overclaim_is_rejected_but_unknown_questions_and_code_are_allowed(self):
        report=self.report('owner_stopped')
        recorded='croc-sar.service inactive，MainPID=0，Result=success，说明是干净停止而非崩溃。'
        self.assertEqual([x['id'] for x in answer_issues(report,recorded,['OBS-1','OBS-2'])],['ha_stop_cause'])
        for text in ['可以排除崩溃。','已经确定不是崩溃。','说明是手动停止，而不是崩溃。']:
            self.assertTrue(stop_cause_overclaim(text),text)
        for text in ['Result=success不能据此排除崩溃。','不排除崩溃。','能排除崩溃吗？',
                     '如果取得完整退出证据后才能排除崩溃。','错误示例：可以排除崩溃。',
                     '示例 `说明是干净停止而非崩溃`，现场原因仍未知。',
                     '```python\nx="排除崩溃"\n```\n原因未核实。',
                     '服务inactive且MainPID=0，停止触发者和时序未知。']:
            self.assertFalse(stop_cause_overclaim(text),text)

    def report(self,variant,device='local-backup'):
        return assess_case([observation(variant,'services',1,device),observation(variant,'interfaces',2,device)],
                           {device:['services','interfaces','logs','routes']},2)

    def test_case_only_applies_to_explicit_diagnostic_ha_fault(self):
        for mode,scene in [('knowledge','ha_topology'),('diagnostic','croc_local')]:
            self.assertFalse(selected('局端主机Croc业务不通',{'mode':mode,'scene':scene}))
        self.assertFalse(selected('局端主机现在状态',{'mode':'diagnostic','scene':'ha_topology'}))
        self.assertTrue(selected('远端主机业务不通',{'mode':'diagnostic','scene':'ha_topology'}))

    def test_normal_nonowner_does_not_depend_on_inventory_backup_name(self):
        for device in ('local-primary','local-backup','remote-primary','remote-backup'):
            r=self.report('nonowner_stopped',device)
            self.assertEqual(r['nodes'][0]['finding'],'non_owner_stop_expected')
            self.assertEqual(r['next_checks'],[]);self.assertFalse(r['root_cause_confirmed'])

    def test_inventory_backup_with_vip_is_an_owner_and_needs_logs(self):
        for device in ('local-backup','remote-backup'):
            r=self.report('owner_stopped',device)
            self.assertEqual(r['nodes'][0]['finding'],'owner_service_stopped')
            self.assertEqual(r['next_checks'],[{'device':device,'check':'logs'}])
            self.assertEqual(r['required_refs'],['OBS-1','OBS-2'])

    def test_running_nonowner_running_owner_partial_and_unknown_unit_are_distinct(self):
        for variant,finding in [('nonowner_running','non_owner_service_running'),('owner_running','owner_service_running'),
                               ('partial_vip','partial_vip'),('missing_unit','insufficient'),('read_failure','insufficient')]:
            self.assertEqual(self.report(variant)['nodes'][0]['finding'],finding)

    def test_missing_wrong_interface_and_conflicting_address_rows_remain_unknown(self):
        expected={'eth2':'192.0.2.15','eth3':'172.27.10.37'}
        for output in ['VIP absent','eth2 UP','eth2 UP 00:11:22:33:44:55 <UP>\neth3 UP',
                       'eth2 UP 172.27.10.37/24\neth3 UP 192.0.2.15/24',
                       'eth2 UP 192.0.2.15/24\neth2 UP\neth3 UP']:
            self.assertEqual(vip_state(output,expected),'unknown')
        self.assertEqual(vip_state('eth2 UP 192.0.2.150/24\neth3 DOWN',expected),'non_owner')

    def test_service_properties_order_duplicates_and_transitions_are_conservative(self):
        text=observation('owner_stopped','services')['output']
        self.assertEqual(croc_state(text,'croc-sar.service'),'stopped')
        for variant in [text.replace('LoadState=loaded','LoadState=not-found'),text+'\nMainPID=3',
                        text.replace('ActiveState=inactive','ActiveState=activating'),
                        text.replace('ActiveState=inactive','ActiveState=active').replace('SubState=dead','SubState=exited')]:
            self.assertEqual(croc_state(variant,'croc-sar.service'),'unknown')

    def test_synthetic_stale_and_far_apart_observations_cannot_confirm_case(self):
        for mutation in [dict(origin='synthetic',freshness='synthetic'),dict(freshness='stale')]:
            observations=[dict(observation('owner_stopped',c,i+1),**mutation) for i,c in enumerate(('services','interfaces'))]
            r=assess_case(observations,{'local-backup':['services','interfaces','logs']},2)
            self.assertEqual(r['nodes'][0]['finding'],'insufficient')
        observations=[observation('owner_stopped','services'),observation('owner_stopped','interfaces',2)]
        observations[0]['collected_at']=(datetime.datetime.utcnow()-datetime.timedelta(seconds=120)).isoformat()+'Z'
        r=assess_case(observations,{'local-backup':['services','interfaces','logs']},2)
        self.assertEqual(r['nodes'][0]['finding'],'insufficient')

    def test_guidance_cannot_expand_scope_duplicate_read_or_exceed_budget(self):
        observations=[observation('owner_stopped','services'),observation('owner_stopped','interfaces',2)]
        self.assertEqual(assess_case(observations,{'local-backup':['services','interfaces']},2)['next_checks'],[])
        self.assertEqual(assess_case(observations,{'local-backup':['services','interfaces','logs']},0)['next_checks'],[])
        observations.append(observation('owner_stopped','logs',3))
        self.assertEqual(assess_case(observations,{'local-backup':['services','interfaces','logs']},1)['next_checks'],[])

    def test_answer_must_cite_both_role_and_service_and_not_claim_repair(self):
        report=self.report('nonowner_stopped')
        self.assertTrue(answer_issues(report,'服务已停止',['OBS-1']))
        self.assertTrue(answer_issues(report,'局端备机Croc停止说明故障',['OBS-1','OBS-2']))
        self.assertTrue(answer_issues(report,'业务已恢复',['OBS-1','OBS-2']))
        self.assertEqual(answer_issues(report,'在正常HA假设下，无VIP时服务停止符合非owner预期，业务和根因未知。',['OBS-1','OBS-2']),[])

    def test_short_device_label_is_guarded_but_negated_fault_claim_is_allowed(self):
        report=self.report('nonowner_stopped')
        self.assertTrue(answer_issues(report,'备机Croc停止说明故障',['OBS-1','OBS-2']))
        self.assertEqual(answer_issues(report,'备机Croc停止并不说明故障',['OBS-1','OBS-2']),[])


class CaseHttpTests(unittest.TestCase):
    def test_missing_unit_vip_denial_corrects_once_without_new_reads(self):
        bad={'action':'answer','answer':'Croc服务未运行，未观测到VIP。',
             'refs':['OBS-1','OBS-2'],'basis':'evidence'}
        good=dict(bad,answer='接口已见预期VIP；登记服务单元尚未确认，VRRP角色、全部进程状态及根因未知。')
        for final,status in ((good,'answered'),(bad,'incomplete')):
            planner=Mock(side_effect=[({'action':'read_overview','device':'remote-backup'},0),(bad,0),(final,0)])
            with self.subTest(status=status),RunningApp(planner,case_probe('missing_unit'),lookup=lambda q:[]) as client:
                task=client.diagnostic_chat('远端备机当前Croc没运行，能确认服务情况吗？')[1]['task']
                client.login();client.approve(task['id'])
                result=client.resume(task['id'])[1]['result']
            self.assertEqual(result['status'],status,result)
            self.assertEqual(len(result['evidence']),2)
            self.assertEqual(planner.call_count,3)
            self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)
            self.assertIn('ha_observed_vip',[c['id'] for c in planner.call_args[0][0]['correction_feedback']['constraints']])

    def test_recorded_stop_cause_claim_gets_only_one_correction_and_keeps_evidence(self):
        bad={'action':'answer','answer':'Result=success说明是干净停止而非崩溃。',
             'refs':['OBS-1','OBS-2'],'basis':'evidence'}
        good=dict(bad,answer='按正常HA假设，持VIP而Croc服务停止与预期不一致。停止触发者、模式及业务根因未核实。')
        for final_action,status in [(good,'answered'),(bad,'incomplete')]:
            planner=Mock(side_effect=[({'action':'read_overview','device':'local-backup'},0),(bad,0),(final_action,0)])
            with self.subTest(status=status),RunningApp(planner,case_probe('owner_stopped'),lookup=lambda q:[]) as client:
                task=client.diagnostic_chat('局端备机当前Croc业务不通')[1]['task']
                client.login();client.approve(task['id'])
                result=client.resume(task['id'])[1]['result']
                self.assertEqual(result['status'],status)
                self.assertEqual(len(result['evidence']),2)
                self.assertEqual(planner.call_count,3)
                feedback=planner.call_args[0][0]['correction_feedback']
                self.assertEqual(feedback['constraints'][0]['id'],'ha_stop_cause')
                self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)
                if status=='incomplete':
                    self.assertEqual(result['reason'],'unsupported_case_claim')
                    self.assertNotIn('干净停止而非崩溃',result['answer'])

    def test_ha_variant_matrix_uses_one_approval_and_actual_feedback(self):
        expected={'nonowner_stopped':('non_owner_stop_expected',2),'owner_stopped':('owner_service_stopped',3),
                  'nonowner_running':('non_owner_service_running',3),'owner_running':('owner_service_running',4),
                  'partial_vip':('partial_vip',2),'missing_unit':('insufficient',2),'read_failure':('insufficient',2)}
        for variant in VARIANTS:
            with self.subTest(variant=variant),RunningApp(case_planner,Mock(side_effect=case_probe(variant)),lookup=Mock(return_value=[])) as client:
                requested=client.diagnostic_chat('局端备机当前Croc业务不通')[1]['task']
                self.assertEqual(set(requested['scope']['local-backup']),{'services','interfaces','routes','logs'})
                client.service.probe.assert_not_called();self.assertEqual(client.resume(requested['id'])[0],409)
                client.login();client.approve(requested['id'])
                finished=client.resume(requested['id'])[1];result=finished['result']
                self.assertEqual(finished['status'],'completed',finished)
                self.assertEqual(result['diagnostic_case']['nodes'][0]['finding'],expected[variant][0])
                self.assertEqual(finished['attempts'],expected[variant][1]);self.assertFalse(result['diagnostic_case']['repair_executed'])
                self.assertEqual(len([r for r in client.service.audit.read(requested['id']) if r[1]=='observation']),finished['attempts'])
                self.assertEqual(client.resume(requested['id'])[0],409)

    def test_false_backup_fault_is_corrected_once_and_retained_if_repeated(self):
        action={'action':'answer','answer':'局端备机Croc停止说明故障','refs':['OBS-1','OBS-2'],'basis':'evidence'}
        planner=Mock(side_effect=[({'action':'read_overview','device':'local-backup'},0),(action,0),(action,0)])
        with RunningApp(planner,Mock(side_effect=case_probe('nonowner_stopped')),lookup=Mock(return_value=[])) as client:
            task=client.diagnostic_chat('局端备机当前Croc业务不通')[1]['task'];client.login();client.approve(task['id'])
            result=client.resume(task['id'])[1]['result']
            self.assertEqual(result['status'],'incomplete');self.assertEqual(result['reason'],'unsupported_case_claim')
            self.assertNotIn('停止说明故障',result['answer']);self.assertEqual(planner.call_count,3)
            self.assertEqual(planner.call_args[0][0]['correction_feedback']['constraints'][0]['id'],'ha_nonowner_expected')


if __name__=='__main__': unittest.main()
