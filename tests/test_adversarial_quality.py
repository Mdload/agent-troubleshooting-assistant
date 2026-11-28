"""面试追问对应的语句反例与正式HTTP验收，零外部模型/设备。"""
import unittest
from unittest.mock import Mock
from legacy.ha_croc_case import assess_case,answer_issues,pair_role_overclaim,completion_overclaim,stop_cause_overclaim
from tests.ha_case_test_support import case_probe,case_planner
from legacy.conversation_topic import resolve_topic
from tests.workflow_test_support import RunningApp


class AdversarialQualityTests(unittest.TestCase):
    def owner_report(self):
        observations=[dict(case_probe('owner_running')('local-primary',check),device='local-primary',check=check,
            id='OBS-'+str(i),origin='device_read',freshness='current') for i,check in enumerate(('services','interfaces'),1)]
        return assess_case(observations,{'local-primary':['services','interfaces']},0)

    def test_vip_absence_is_distinct_from_absent_conflict_and_code_literal(self):
        report=self.owner_report()
        for text,expected in [('接口已有预期VIP，未见VIP冲突。',False),
            ('未观测到VIP地址异常，接口已有预期VIP。',False),
            ('接口已见VIP；错误字符串是 `未见VIP`。',False),
            ('当前未见VIP，但运行模式未核实。',True),
            ('当前未观测到VIP地址。',True),('不能据此断言未见VIP。',False)]:
            with self.subTest(text=text):
                issues=answer_issues(report,text,['OBS-1','OBS-2'])
                self.assertEqual(any(i['id']=='ha_observed_vip' for i in issues),expected)

    def test_qualifiers_before_contrast_cannot_hide_later_pair_assertion(self):
        for text in ('两台可能发生角色转换，但已确认脑裂。','目前不能确认原因，但两台同时持有VIP。'):
            self.assertTrue(pair_role_overclaim(text),text)
        for text in ('已经确认脑裂了吗？','无法确认两台同时持有VIP。','两台可能同时持有VIP，需要核实。'):
            self.assertFalse(pair_role_overclaim(text),text)

    def test_questions_and_contrast_for_recovery_and_stop_cause(self):
        self.assertFalse(completion_overclaim('业务已经恢复了吗？'))
        self.assertFalse(completion_overclaim('能否认定业务已恢复？'))
        self.assertTrue(completion_overclaim('业务已经恢复。'))
        self.assertTrue(stop_cause_overclaim('目前不能确认模式，但可以排除崩溃。'))
        self.assertFalse(stop_cause_overclaim('如果取得完整退出证据后才能排除崩溃。'))

    def test_role_aliases_follow_new_target_but_comparison_anchor_remains(self):
        for role in ('主机','主节点'):
            anchor='局端'+role+'的Croc启停规则是什么'
            result=resolve_topic('那远端备机呢',{'question':anchor,'domain':'topology','intent':'explain'})
            self.assertNotIn(role,result['question'])
            self.assertIn('远端备机',result['question'])
            self.assertEqual(result['anchor'],anchor)
        anchor='局端主机与备机的Croc启停有什么区别'
        result=resolve_topic('那备机呢',{'question':anchor,'domain':'topology','intent':'explain'})
        self.assertIn(anchor,result['question'])

    def test_new_backup_question_and_approval_have_consistent_target(self):
        planner=Mock(side_effect=case_planner);reads=[]
        def probe(device,check):
            reads.append(device)
            return case_probe('nonowner_stopped')(device,check)
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            old=client.diagnostic_chat('局端主机当前Croc业务不通',conversation_id='role')[1]['task']
            client.login();client.approve(old['id'])
            new=client.diagnostic_chat('那远端备机呢',conversation_id='role')[1]['task']
            self.assertNotIn('主机',new['question'])
            self.assertEqual(set(new['scope']),{'remote-backup'})
            self.assertEqual(client.resume(new['id'])[0],409)
            self.assertEqual(reads,[])
            client.approve(new['id'])
            result=client.resume(new['id'])[1]['result']
            self.assertEqual(result['status'],'answered',result)
        self.assertEqual(set(reads),{'remote-backup'})
        self.assertTrue(all('主机' not in call[0][0]['question'] for call in planner.call_args_list))

    def test_address_or_hostname_switch_clarifies_until_a_new_full_question(self):
        for identity in ('192.0.2.13','example-local-primary'):
            planner=Mock();probe=Mock()
            with self.subTest(identity=identity),RunningApp(planner,probe,lookup=lambda q:[]) as client:
                client.diagnostic_chat('局端主机'+identity+'当前Croc业务不通',conversation_id='bound')
                for question in ('那远端备机呢','那现在呢'):
                    result=client.diagnostic_chat(question,conversation_id='bound')[1]
                    self.assertEqual(result['status'],'clarify',result)
                self.assertEqual(len(client.store.list()),1)
                new=client.diagnostic_chat('远端备机当前Croc业务不通',conversation_id='bound')[1]['task']
                self.assertEqual(set(new['scope']),{'remote-backup'})
                self.assertNotIn(identity,new['question'])
            planner.assert_not_called();probe.assert_not_called()

    def test_same_explicit_identity_current_followup_does_not_need_retarget(self):
        anchor='局端主机192.0.2.13当前Croc业务不通'
        result=resolve_topic('那现在呢',{'question':anchor,'domain':'topology','intent':'live','target_hint':'局端主机'})
        self.assertFalse(result['needs_question'])
        self.assertIn('192.0.2.13',result['question'])


if __name__=='__main__':unittest.main()
