"""当前状态的宿主约束；全部采用隔离HTTP及工具边界夹具，无实际设备或模型调用。"""
if not __package__:
    import _bootstrap

import datetime
import unittest
from unittest.mock import Mock,patch
from evidence_scope import mark_observation
from question_routing import classify
from tests.workflow_test_support import RunningApp,current_probe_fixture,fake_probe


def answer(text='服务管理器状态为active；VIP、HA和业务仍未核实。',refs=None,basis='evidence'):
    return {'action':'answer','answer':text,'refs':refs or ['OBS-1'],'basis':basis},1


SOURCE={'id':'KB-001','kind':'historical_case','historical_finding':'历史记录中该设备曾运行。',
        'evidence_date':'2026-09-28','live_verified':False}
READ=({'action':'read','device':'local-backup','check':'services'},1)


class CurrentRoutingTests(unittest.TestCase):
    def test_status_questions_without_now_still_need_approval(self):
        questions=['远端主机PPP拨上号了吗','局端主机有没有VIP','远端备机在线吗',
                   '局端主机正常吗','局端主机Croc运行着吗','局端主机Croc运行吗',
                   '局端备机Croc没运行正常吗','远端主机状态是什么','局端主机VIP还在吗',
                   '局端主机有VIP吗','远端主机PPP拨号成功了没有','远端主机ppp1是UP吗']
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(classify(question)['route'],'diagnostic')

    def test_mechanism_and_instructions_do_not_trigger_reads(self):
        for question in ['局端备机为什么不运行Croc','如何排查当前Croc异常',
                         '如果局端备机Croc没运行该怎么判断','局端备机正常情况下没有VIP的原理是什么']:
            with self.subTest(question=question):
                self.assertEqual(classify(question)['route'],'lab_knowledge')

    def test_current_state_without_target_is_unknown_and_needs_clarification(self):
        route=classify('现在设备的状态')
        self.assertEqual(route['route'],'clarify');self.assertIn('未核实',route['answer'])

    def test_no_read_instruction_cannot_make_current_question_knowledge(self):
        planner=Mock();probe=Mock()
        with RunningApp(planner,probe) as client:
            result=client.chat('局端备机现在Croc运行吗，不要检查，只解释')[1]
            self.assertEqual(result['status'],'current_unverified')
            self.assertIn('当前状态未核实',result['answer'])
            self.assertEqual(client.store.list(),[]);planner.assert_not_called();probe.assert_not_called()


    def test_conversational_status_asks_for_fresh_approval_not_archive_qa(self):
        for question in ('局端主机情况怎么样', '远端备机怎么样', '远端主机还能用吗',
                         '局端主机正在运行哪些程序', '局端备机有哪些服务在运行'):
            with self.subTest(question=question):
                self.assertEqual(classify(question)['route'], 'diagnostic')
        for question in ('2026-09-28局端主机状态怎么样', '9月28日当时局端备机有没有VIP'):
            with self.subTest(question=question):
                self.assertEqual(classify(question)['route'], 'lab_knowledge')
        for question in ('局端主机平时负责什么', '如果局端主机不通怎么排查'):
            self.assertEqual(classify(question)['route'], 'lab_knowledge')
        self.assertEqual(classify('对比9月28日，局端主机现在状态怎么样')['route'], 'diagnostic')
        today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        self.assertEqual(classify(today+'局端主机状态怎么样')['route'],'diagnostic')


    def test_old_history_cannot_answer_a_new_status_question_before_consent(self):
        planner=Mock();probe=Mock()
        with RunningApp(planner,probe) as client:
            mode=client.call('/api/conversations/fresh/mode',{'mode':'assistant','scene':None})[1]
            history=[{'role':'user','content':'局端主机情况怎么样'},
                     {'role':'assistant','content':'2026-09-28局端主机持有VIP，Croc运行。'}]
            result=client.call('/api/chat',{'question':'局端主机情况怎么样','history':history,
                'conversation_id':'fresh','interaction_mode':'assistant','interaction_revision':mode['revision'],
                'allow_context':False})[1]
            self.assertEqual(result['status'],'approval_required')
            self.assertEqual(result['task']['attempts'],0)
            self.assertIn('local-primary',result['task']['scope'])
            planner.assert_not_called();probe.assert_not_called()


class CollectionWindowTests(unittest.TestCase):
    def test_timestamp_and_clock_must_match_this_read(self):
        began=datetime.datetime(2026,10,8,8,0,0);finished=began+datetime.timedelta(seconds=20)
        base={'ok':True,'address':'192.0.2.14','output':'active','clock_source':'collector_utc'}
        cases=[('2026-10-08T08:00:00Z','collector_utc','current'),
               ('2026-09-28T08:00:00Z','collector_utc','stale'),
               ('2026-10-08T08:01:00Z','collector_utc','stale'),
               ('2026-10-08T08:00:00Z','device_clock','unconfirmed'),
               ('not-a-time','collector_utc','unconfirmed')]
        for timestamp,source,expected in cases:
            with self.subTest(timestamp=timestamp,clock=source):
                item=dict(base,collected_at=timestamp,clock_source=source,origin='device_read')
                result=mark_observation(item,'local-backup','services',(began,finished))
                self.assertEqual(result['freshness'],expected)


class CurrentEvidenceHttpTests(unittest.TestCase):
    def resume(self,client):
        task=client.diagnostic_chat('局端备机现在的服务状态')[1]['task']
        client.login();client.approve(task['id'])
        return client.resume(task['id'])[1]

    def test_rag_only_claim_of_current_state_is_rejected(self):
        claim=answer('现在局端备机完全正常。',['KB-001'])
        planner=Mock(return_value=claim);probe=Mock()
        with RunningApp(planner,probe,lookup=Mock(return_value=[SOURCE])) as client:
            result=self.resume(client)
            self.assertEqual(result['status'],'incomplete');self.assertEqual(result['attempts'],0)
            self.assertEqual(result['result']['reason'],'current_evidence_required')
            self.assertIn('状态未核实',result['result']['answer'])
            self.assertNotIn('现在局端备机完全正常',result['result']['answer'])
            self.assertEqual(planner.call_count,2);probe.assert_not_called()

    def test_correction_can_obtain_current_evidence_within_approved_scope(self):
        planner=Mock(side_effect=[answer('现在运行。',['KB-001']),READ,answer()])
        probe=Mock(side_effect=current_probe_fixture)
        with RunningApp(planner,probe,lookup=Mock(return_value=[SOURCE])) as client:
            result=self.resume(client)
            self.assertEqual(result['status'],'completed');self.assertEqual(result['attempts'],1)
            self.assertEqual(result['result']['refs'],['OBS-1'])
            self.assertEqual(result['result']['evidence'][0]['freshness'],'current')
            self.assertEqual(planner.call_count,3);probe.assert_called_once()

    def test_old_snapshot_cannot_be_promoted_even_with_address_and_origin(self):
        probe=Mock(return_value={'ok':True,'address':'192.0.2.14','origin':'device_read',
            'clock_source':'collector_utc','collected_at':'2026-09-28T08:00:00Z','output':'active'})
        planner=Mock(side_effect=[READ,answer('现在局端备机完全正常。'),answer('现在局端备机完全正常。')])
        with RunningApp(planner,probe,lookup=Mock(return_value=[])) as client:
            result=self.resume(client)['result']
            self.assertEqual(result['status'],'incomplete');self.assertEqual(result['refs'],[])
            self.assertEqual(result['evidence'][0]['freshness'],'stale')
            self.assertEqual(result['evidence_scope']['current_reads'],0)
            self.assertIn('状态未核实',result['answer']);self.assertNotIn('完全正常',result['answer'])

    def test_synthetic_read_is_retained_but_cannot_complete_current_diagnosis(self):
        planner=Mock(side_effect=[READ,answer(),answer()])
        with RunningApp(planner,Mock(side_effect=fake_probe),lookup=Mock(return_value=[])) as client:
            result=self.resume(client)
            self.assertEqual(result['status'],'incomplete');self.assertEqual(result['attempts'],1)
            self.assertEqual(result['result']['evidence'][0]['freshness'],'synthetic')
            self.assertIn('不代表真实设备现状',result['result']['answer'])

    def test_failed_read_does_not_accept_model_claim_or_historical_fallback(self):
        planner=Mock(side_effect=[READ,answer('现在正常。',['KB-001']),answer('现在正常。',['KB-001'])])
        probe=Mock(return_value={'ok':False,'error':'timeout'})
        with RunningApp(planner,probe,lookup=Mock(return_value=[SOURCE])) as client:
            result=self.resume(client)['result']
            self.assertEqual(result['status'],'incomplete');self.assertIn('状态未核实',result['answer'])
            self.assertEqual(result['refs'],[]);self.assertNotIn('现在正常',result['answer'])

    def test_current_claim_must_reference_current_obs(self):
        planner=Mock(side_effect=[READ,answer('现在运行。',['KB-001']),answer('现在运行。',['KB-001'])])
        with RunningApp(planner,Mock(side_effect=current_probe_fixture),lookup=Mock(return_value=[SOURCE])) as client:
            result=self.resume(client)['result']
            self.assertEqual(result['status'],'incomplete')
            self.assertEqual(result['reason'],'current_evidence_required')

    def test_limited_local_answer_with_current_state_refs_is_not_missing_evidence(self):
        planner=Mock(side_effect=[READ,answer(basis='limited')])
        with RunningApp(planner,Mock(side_effect=current_probe_fixture),lookup=Mock(return_value=[])) as client:
            task=self.resume(client)
            self.assertEqual(task['status'],'completed')
            result=task['result']
            self.assertEqual(result['basis'],'limited')
            self.assertEqual(result['refs'],['OBS-1'])
            self.assertEqual(planner.call_count,2)
            self.assertFalse(any(e.get('action')=='correction' for e in result['events']))

    def test_limited_and_general_labels_cannot_replace_current_state_evidence(self):
        for basis in ('limited','general'):
            planner=Mock(return_value=answer('现在状态完全正常。',['KB-001'],basis))
            probe=Mock()
            with self.subTest(basis=basis),RunningApp(planner,probe,lookup=Mock(return_value=[SOURCE])) as client:
                result=self.resume(client)['result']
                self.assertEqual(result['status'],'incomplete')
                self.assertEqual(result['reason'],'current_evidence_required')
                probe.assert_not_called()

    def test_history_of_previous_read_still_requires_new_approval(self):
        history=[{'role':'user','content':'检查局端备机'},
                 {'role':'assistant','content':'昨天服务active，已读取OBS-1。'}]
        planner=Mock();probe=Mock()
        with RunningApp(planner,probe) as client:
            result=client.chat('那它现在在线吗',history=history)[1]
            self.assertEqual(result['status'],'current_unverified')
            self.assertEqual(client.store.list(),[])
            planner.assert_not_called();probe.assert_not_called()

    def test_legacy_knowledge_http_cannot_answer_current_state_from_snapshot(self):
        with RunningApp() as client,patch('qa.answer') as old_answer:
            result=client.call('/api/ask',{'question':'局端备机现在的状态','allow_new_topology':True})[1]
            self.assertEqual(result['status'],'current_unverified')
            self.assertIn('当前状态未核实',result['answer']);old_answer.assert_not_called()


if __name__=='__main__':unittest.main()
