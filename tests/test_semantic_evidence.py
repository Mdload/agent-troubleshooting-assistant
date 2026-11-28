"""Finite historical-claim and selection regressions from serialized R1 evidence."""
import unittest

from legacy.investigation_context import recall_history, analysis_device_focus
from legacy.unified_chat import historical_claim_issues
from devices.measurement_evidence import measurement_claim_issues, project_measurement


class HistoricalEnumerationTests(unittest.TestCase):
    def test_shared_negative_predicate_covers_nominal_enumeration_only(self):
        valid=('当前状态、端到端业务可用和根因都还未核实。',
               '当前业务可用与否、根因是否确定均尚未确认。',
               '当前状态、业务恢复和根因都仍待核实。')
        invalid=('尚未确认根因，业务已恢复。',
                 '当前业务已恢复和根因都还未核实。',
                 '当前业务可用而根因未核实。',
                 '当前业务可用，根因未核实。')
        for answer in valid:
            with self.subTest(answer=answer):
                self.assertEqual([],historical_claim_issues(answer))
        for answer in invalid:
            with self.subTest(answer=answer):
                self.assertTrue(historical_claim_issues(answer))


class RecallDeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.context={'directory':[{'task_id':'pair','checks':['interfaces','services'],
            'devices':{'remote-primary':['interfaces'],'remote-backup':['services']}}],
            'records':[{'task_id':'pair','device':'remote-primary','check':'interfaces'},
                       {'task_id':'pair','device':'remote-backup','check':'services'},
                       {'task_id':'omitted','device':'remote-backup','check':'services'}]}

    def test_device_filter_follows_catalog_and_does_not_revive_omitted_records(self):
        rows=recall_history(self.context,{'action':'recall_history','task_id':'latest','devices':['remote-backup']})
        self.assertEqual([{'task_id':'pair','device':'remote-backup','check':'services'}],rows)
        self.assertEqual(2,len(recall_history(self.context,{'action':'recall_history','task_id':'latest'})))

    def test_device_selector_rejects_unknown_empty_duplicates_and_wrong_task_scope(self):
        for devices in ([],['remote-backup','remote-backup'],['local-primary'],['router-1'],
                        'remote-backup',None,[None]):
            with self.subTest(devices=devices):
                with self.assertRaises(ValueError):
                    recall_history(self.context,dict(action='recall_history',task_id='pair',devices=devices))

    def test_echoed_device_mapping_can_only_narrow_exact_device_check_pairs(self):
        select=lambda devices, **options: recall_history(self.context,dict(
            action='recall_history',task_id='pair',devices=devices,**options))
        want=self.context['records'][1:2]
        self.assertEqual(want, select({'remote-backup':['services']}))
        self.assertEqual([],select({'remote-backup':['services']},checks=['interfaces']))
        for mapping in ({}, {'remote-primary':['services']}, {'local-primary':['interfaces']},
                        {'remote-backup':[]}, {'remote-backup':['services','services']}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(ValueError):select(mapping)

    def test_only_explicit_positive_focus_clauses_change_registered_device_selection(self):
        cases=((['本轮只分析远端备机，主机留待后续。'],['remote-backup']),
               (['本轮只分析 remote-backup，不再读设备。'],['remote-backup']),
               (['本轮只分析远端主机。','解释一下 Python 列表。'],['remote-primary']),
               (['本轮只分析远端主机。','更正，取消设备分析范围限制。'],None),
               (['本轮只分析远端主机。','不再只分析远端主机，改为只分析局端备机。'],['local-backup']),
               (['解释“本轮只分析远端主机”这句话是什么意思。'],None),
               (['解释本轮只分析远端主机是什么意思。'],None),
               (['本轮只分析远端主机。','解释取消设备分析范围限制是什么意思。'],['remote-primary']))
        for questions,want in cases:
            with self.subTest(questions=questions):
                self.assertEqual(want,analysis_device_focus(questions))


class ICMPMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'id':'OBS-1','ok':True,'measurement':{'kind':'icmp','outcome':'measured','sent':3,'received':0}},
                   {'id':'OBS-2','ok':True,'measurement':{'kind':'icmp','outcome':'measured','sent':3,'received':3}}]

    def test_explicit_claims_are_checked_against_their_cited_measurements(self):
        wrong=('当时从局端主机 ping 远端主机的管理地址 198.51.100.133，3 个包全部收到应答，'
               '说明那一刻到远端主机管理地址的 ICMP 是通的。')
        for answer in (wrong,'应答 3/3。[OBS-1]','发出二个包，收到零个应答。[OBS-1]'):
            with self.subTest(answer=answer):
                issues=measurement_claim_issues(answer,self.rows,['OBS-1'])
                self.assertTrue(issues)
                self.assertEqual('OBS-1',issues[0]['ref'])
                self.assertEqual(0,issues[0]['measurement']['received'])
        for answer in ('应答 0/3。[OBS-1]','当时发出三个包，收到零个应答。[OBS-1]',
                       '未收到应答。[OBS-1]','3 个包全部收到应答。[OBS-2]',
                       '应答 0/3。[OBS-1] 应答 3/3。[OBS-2]',
                       '如果 3 个包全部收到应答，还需要测试业务。[OBS-1]',
                       '不能说 3 个包全部收到应答。[OBS-1]'):
            with self.subTest(answer=answer):
                self.assertEqual([],measurement_claim_issues(answer,self.rows,['OBS-1','OBS-2']))

    def test_all_replied_count_is_not_just_equality_and_qualification_is_local(self):
        self.rows[1]['measurement'].update(sent=2,received=2)
        for answer in ('3 个包全部收到应答。[OBS-2]',
                       '如果三包全回可以说明局部结果，但实际应答 3/3。[OBS-1]',
                       '如果三包全回可以说明局部结果但实际应答 3/3。[OBS-1]',
                       '并非 3/3，实际发出 3 个包，收到 3 个应答。[OBS-1]'):
            with self.subTest(answer=answer):
                self.assertTrue(measurement_claim_issues(answer,self.rows,['OBS-1','OBS-2']))

    def test_typed_projection_keeps_route_values_and_never_invents_no_send_counts(self):
        fact=project_measurement('local-primary','ping_remote_primary',
            'ICMP_TX=3\nICMP_RX=0\nICMP_ROUTE_DEV=tun16\nICMP_ROUTE_SRC=192.0.2.2\nICMP_ROUTE_VIA=192.0.2.1\n')
        self.assertEqual({'sent':3,'received':0},{k:fact[k] for k in ('sent','received')})
        self.assertEqual({'source':'192.0.2.2','interface':'tun16','gateway':'192.0.2.1'},fact['route'])
        self.assertEqual('198.51.100.133',fact['target_ip'])
        for check,output,outcome in (('ping_remote_primary','ICMP_ROUTE_UNAVAILABLE=1','route_unavailable'),
                ('ping_tunnel_peer','ICMP_TUNNEL_PATH_MISMATCH=1','tunnel_path_mismatch')):
            value=project_measurement('local-primary',check,output)
            self.assertEqual(outcome,value['outcome'])
            self.assertNotIn('sent',value)
            self.assertNotIn('received',value)
        self.assertIsNone(project_measurement('local-primary','ping_remote_primary','ICMP_TX=1\nICMP_RX=3\n'))

    def test_source_binding_cannot_be_removed_or_universally_satisfied_by_one_row(self):
        self.assertTrue(measurement_claim_issues('3 个包全部收到应答。',self.rows[:1],[]))
        self.assertTrue(measurement_claim_issues('应答 3/3。',self.rows,[]))
        self.assertEqual([],measurement_claim_issues('应答 0/3。',self.rows[:1],[]))
        self.rows.append({'id':'OBS-3','ok':True,'measurement':{'kind':'icmp','outcome':'route_unavailable'}})
        self.assertTrue(measurement_claim_issues('这两项探测的应答统计都是 3/3 [OBS-2][OBS-3]。',self.rows,['OBS-2','OBS-3']))
        for references in ('[OBS-2][OBS-3]', '[OBS-3] [OBS-2]'):
            self.assertTrue(measurement_claim_issues('这两项探测的应答统计都是 3/3。'+references,
                                                     self.rows,['OBS-2','OBS-3']))
        self.assertEqual([],measurement_claim_issues('应答 0/3 [OBS-1]，另一项应答 3/3 [OBS-2]。',self.rows,['OBS-1','OBS-2']))

    def test_loss_and_negated_all_preserve_metric_and_polarity(self):
        for answer in ('丢包 3/3。[OBS-1]','这三个包并未全部收到应答。[OBS-1]',
                       '丢包三个。[OBS-1]','接口地址 198.51.100.29/24。[OBS-1]'):
            with self.subTest(answer=answer):
                self.assertEqual([],measurement_claim_issues(answer,self.rows,['OBS-1']))
        for answer in ('丢包 0/3。[OBS-1]','丢包零个。[OBS-1]',
                       '这三个包并未全部收到应答。[OBS-2]'):
            with self.subTest(answer=answer):
                self.assertTrue(measurement_claim_issues(answer,self.rows,['OBS-1','OBS-2']))
        self.rows[0]['measurement']['received']=2
        self.assertEqual([],measurement_claim_issues('未全部收到应答。[OBS-1]',self.rows,['OBS-1']))
        self.assertTrue(measurement_claim_issues('一包也未收到应答。[OBS-1]',self.rows,['OBS-1']))


if __name__=='__main__':
    unittest.main()
