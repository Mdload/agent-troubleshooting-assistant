"""Investigation continuity through HTTP and the production model serializer."""
from contextlib import closing
import io
import json
import threading
import unittest
from unittest.mock import patch

from server.approval_store import ApprovalStore, SINGLE_APPROVAL
from agent.chat_model import make_model
from legacy.lab_workflow import WorkflowService
import tests.test_diagnostic_continuity as test_diagnostic_continuity
from tests.test_diagnostic_dialogues import post_question, run_task
from tests.workflow_test_support import RunningApp, current_probe_fixture


FOLLOWUPS = (
    '把刚刚的检测发现归纳一下',
    '两边互访的测试做完了，你发现了什么？',
    '这种一边有回应另一边没动静的情况，你怎么理解？',
    '你刚刚实际拿到了哪些反馈？',
    '回顾一下刚刚那轮排查，帮我列出尚未排除的可能性',
)


class ModelTransport:
    def __init__(self):
        self.requests = []
        self.answer = None
        self.request_history = False

    def open(self, request, timeout):
        messages = json.loads(request.data.decode())['messages']
        context = json.loads(messages[1]['content'])
        self.requests.append(context)
        observations = context['observations']
        reply = {'action': 'answer', 'refs': [o['id'] for o in observations if o['ok']],
                 'basis': 'limited' if observations else 'general',
                 'answer': '\n'.join(o['output'] + (' [' + o['id'] + ']' if o['ok'] else '')
                                     for o in observations) or '还没有检测记录。'}
        if self.request_history and context.get('history_recall'):
            reply = {'action':'recall_history','task_id':context['history_recall']['default_task_id']}
        elif self.answer:
            reply = self.answer(context)
        return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(reply, ensure_ascii=False)}}]}).encode())


class InvestigationContextTests(unittest.TestCase):
    measure = test_diagnostic_continuity.DiagnosticContinuityTests.measure

    def setUp(self):
        self.failed = False
        self.timeout = False
        self.reads = []
        self.transport = ModelTransport()
        self.planner = make_model({'LLM_BASE_URL': 'https://api.inferera.com/v1',
            'LLM_API_KEY': 'test-placeholder', 'LLM_MODEL_ID': 'investigation-fixture'}, self.transport)
        self.client = RunningApp(self.planner, self.probe, lookup=lambda _: []).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.service.auto_read_only = True
        self.client.enter_diagnostic('diagnosis')

    def probe(self, device, check):
        self.reads.append((device, check))
        if check == 'policy_routes':
            output = '100: from all lookup main\ndefault via 192.0.2.1 dev eth0\n'
        elif check == 'ping_remote_primary' and not self.timeout:
            output = 'ICMP_ROUTE_UNAVAILABLE=1'
        else:
            output = 'ICMP_TX=3\nICMP_RX={}\nICMP_MODE=kernel_route\nICMP_ROUTE_DEV=tun16\n'.format(
                0 if self.timeout else 3)
        value = dict(current_probe_fixture(device, check), output=output)
        if self.failed:
            value.update(ok=False, output='', error='timeout')
        return value

    def ask(self, question, recall=False):
        # Each test declares a planner decision. This double does not classify
        # a question's wording or claim to predict a real model's behavior.
        with patch.object(self.transport,'request_history',recall):
            return post_question(self.client, question)[1]

    def read_backup_routes(self):
        finished = threading.Event()
        original = self.client.service.run_background

        def tracked(*args):
            try:
                return original(*args)
            finally:
                finished.set()

        with patch.object(self.client.service, 'run_background', tracked):
            result = self.ask('改查远端备机的路由表')
            self.assertEqual('investigation_started', result['status'], result)
            self.assertTrue(finished.wait(5))
        return result['task']['id']

    def test_paraphrases_reuse_original_citations_in_serialized_knowledge_request(self):
        task, measured = self.measure()
        original = {entry['id']: entry['collected_at'] for entry in measured['evidence']}
        for question in FOLLOWUPS:
            with self.subTest(question=question):
                self.transport.requests.clear()
                reply = self.ask(question,recall=True)
                self.assertEqual('answered', reply['status'], reply)
                self.assertTrue(self.transport.requests, reply)
                self.assertEqual([],self.transport.requests[0]['observations'])
                self.assertTrue(self.transport.requests[0].get('history_recall'))
                payload = self.transport.requests[-1]
                self.assertEqual({}, payload['tool_catalog'])
                self.assertEqual(0, payload['remaining_reads'])
                self.assertEqual(4, len(payload['observations']), payload['observations'])
                self.assertEqual(4, len(reply['evidence']), reply)
                self.assertEqual('historical', reply['evidence_scope']['kind'])
                for entry in payload['observations'] + reply['evidence']:
                    self.assertEqual('historical', entry['freshness'])
                    self.assertEqual(task['id'], entry['task_id'])
                    self.assertEqual(original[entry['source_ref']], entry['collected_at'])
                self.assertEqual(set(e['id'] for e in reply['evidence']), set(reply['refs']))
        self.assertEqual(4, len(self.reads))

    def test_recalling_original_probe_after_route_detour_uses_original_task(self):
        original, _ = self.measure()
        self.read_backup_routes()
        for question in ('刚才局端到远端的管理地址为什么没有发包？',
                         '最早那次双向测试有几个包回来了？'):
            with self.subTest(question=question):
                reply = self.ask(question,recall=True)
                self.assertEqual('answered', reply['status'], reply)
                self.assertTrue(reply['evidence'], reply)
                self.assertEqual({original['id']}, {e['task_id'] for e in reply['evidence']})
        self.assertEqual(5, len(self.reads))

    def test_automatic_local_route_data_never_reaches_model(self):
        task, _ = self.measure()
        local = self.read_backup_routes()
        self.transport.requests.clear()
        self.ask(FOLLOWUPS[-1],recall=True)
        self.assertTrue(self.transport.requests)
        for context in self.transport.requests:
            payload = json.dumps(context, ensure_ascii=False)
            self.assertNotIn('192.0.2.1', payload)
            self.assertFalse(any(e['task_id'] == local for e in context['observations']))
        self.assertTrue(any(e['task_id']==task['id'] for view in self.transport.requests for e in view['observations']))
        self.assertEqual(5, len(self.reads))

    def test_recalled_ping_retains_typed_counts_and_no_send_has_no_counts(self):
        self.measure()
        self.ask(FOLLOWUPS[0],recall=True)
        rows=self.transport.requests[-1]['observations']
        no_send=next(o for o in rows if o['check']=='ping_remote_primary')
        self.assertEqual('route_unavailable',no_send['measurement']['outcome'])
        self.assertNotIn('sent',no_send['measurement'])
        self.assertNotIn('received',no_send['measurement'])
        measured=next(o for o in rows if o['check']=='ping_tunnel_peer')
        self.assertEqual(3,measured['measurement']['sent'])
        self.assertEqual(3,measured['measurement']['received'])
        self.assertEqual('tun16',measured['measurement']['route']['interface'])
        self.assertNotIn('ICMP_TX',json.dumps(rows))

    def test_wrong_recalled_counts_get_one_correction_with_canonical_measurement(self):
        self.timeout=True
        task,_=self.measure()
        self.transport.requests.clear()
        def answer(view):
            if view.get('history_recall'):
                return {'action':'recall_history','task_id':task['id'],'checks':['ping_remote_primary']}
            if not view.get('correction_feedback'):
                return {'action':'answer','basis':'limited','refs':['OBS-1'],
                        'answer':'当时 3 个包全部收到应答。[OBS-1]'}
            return {'action':'answer','basis':'limited','refs':['OBS-1'],
                    'answer':'当时发出 3 个探测，收到 0 个应答，原因待核实。[OBS-1]'}
        self.transport.answer=answer
        reply=self.ask(FOLLOWUPS[0])
        self.assertEqual('answered',reply['status'],reply)
        self.assertNotIn('全部收到应答',reply['answer'])
        self.assertEqual(3,len(self.transport.requests))
        feedback=self.transport.requests[-1]['correction_feedback']
        fact=feedback['constraints'][0]['measurement']
        self.assertEqual({'sent':3,'received':0}, {k:fact[k] for k in ('sent','received')})
        self.assertEqual(4,len(self.reads))

    def test_local_task_directory_does_not_imply_unexecuted(self):
        self.measure()
        local=self.read_backup_routes()
        self.transport.requests.clear()
        reply=self.ask(FOLLOWUPS[0],recall=True)
        for view in self.transport.requests:
            tasks=view['investigation_context']['related_tasks']
            self.assertEqual('not_disclosed',next(t['state'] for t in tasks if t['task_id']==local))
            self.assertNotIn('192.0.2.1',json.dumps(view))
        self.assertIn('192.0.2.1',reply['answer'])

    def test_mixed_recap_uses_recorded_outcomes_instead_of_unseen_execution_claims(self):
        self.timeout=True
        self.measure()
        local=self.read_backup_routes()
        self.transport.requests.clear()
        self.transport.answer=lambda view: {
            'action':'answer','basis':'limited','refs':['OBS-1'],
            'answer':'当时只完成了一项检查，远端备机路由还没有执行。[OBS-1]'}
        reply=self.ask('刚才的探测和路由表检查分别查到了什么？',recall=True)
        self.assertEqual('answered',reply['status'],reply)
        self.assertNotIn('还没有执行',reply['answer'])
        self.assertIn('应答 0/3',reply['answer'])
        self.assertIn('本地历史检查',reply['answer'])
        self.assertIn('192.0.2.1',reply['answer'])
        self.assertEqual(len(self.transport.requests), 1)
        self.assertTrue(any(o['task_id']==local for o in reply['evidence']))
        self.assertNotIn('192.0.2.1',json.dumps(self.transport.requests))
        self.assertEqual(5,len(self.reads))

    def test_device_scoped_recall_filters_model_and_fallback_without_new_reads(self):
        self.measure()
        self.ask('更正一下，本轮只分析远端主机，不再读设备。')
        self.transport.requests.clear()
        def answer(view):
            if view.get('history_recall'):
                return {'action':'recall_history','task_id':'latest','devices':['remote-primary']}
            return {'action':'answer','basis':'limited','refs':['OBS-999'],'answer':'无效引用'}
        self.transport.answer=answer
        reply=self.ask(FOLLOWUPS[0])
        directory=self.transport.requests[0]['history_recall']['tasks']
        self.assertEqual({'remote-primary'},set(directory[0]['devices']))
        for view in self.transport.requests[1:]:
            self.assertEqual({'remote-primary'},{o['device'] for o in view['observations']})
            self.assertEqual({'remote-primary'},set(view['investigation_context']['registered_ha_resources']))
        self.assertEqual({'remote-primary'},{o['device'] for o in reply['evidence']})
        self.assertEqual(4,len(self.reads))

    def test_registered_ha_context_uses_only_approved_recalled_device_registry(self):
        self.measure()
        self.transport.answer=lambda view: ({'action':'recall_history','task_id':'latest','devices':['remote-primary']}
            if view.get('history_recall') else {'action':'answer','basis':'limited','refs':['OBS-1'],
                'answer':'仅说明当时探测结果。[OBS-1]'})
        self.ask(FOLLOWUPS[0])
        registry=self.transport.requests[-1]['investigation_context']['registered_ha_resources']
        self.assertEqual({'remote-primary'},set(registry))
        self.assertEqual({'eth0':'198.51.100.29'},registry['remote-primary']['expected_vips'])
        self.assertEqual('croc2.0smr.service',registry['remote-primary']['croc_unit'])
        self.assertEqual('static_expected_registry',registry['remote-primary']['origin'])

    def test_numeric_source_metric_and_polarity_are_checked_through_http(self):
        self.timeout=True
        task,_=self.measure()
        limiter_now=self.client.server.request_limiter.clock()
        ticks=iter(range(1,100))
        self.client.server.request_limiter.clock=lambda:limiter_now+61*next(ticks)
        cases=(('3 个包全部收到应答。',[],'incomplete'),
               ('丢包 0/3。[OBS-1]',['OBS-1'],'incomplete'),
               ('丢包 3/3。[OBS-1]',['OBS-1'],'answered'),
               ('这三个包并未全部收到应答。[OBS-1]',['OBS-1'],'answered'))
        for answer,refs,status in cases:
            with self.subTest(answer=answer):
                self.transport.requests.clear()
                self.transport.answer=lambda view: ({'action':'recall_history','task_id':task['id'],
                    'checks':['ping_remote_primary']} if view.get('history_recall') else
                    {'action':'answer','basis':'limited','refs':refs,'answer':answer})
                reply=self.ask(FOLLOWUPS[0])
                self.assertEqual(status,reply['status'],reply)
                self.assertEqual(3 if status=='incomplete' else 2,len(self.transport.requests))
        self.assertEqual(4,len(self.reads))

    def test_universal_probe_claim_requires_every_cited_measurement_through_http(self):
        self.measure()
        def answer(view):
            if view.get('history_recall'):
                return {'action':'recall_history','task_id':'latest'}
            no_send=next(o['id'] for o in view['observations'] if o['measurement']['outcome']=='route_unavailable')
            measured=next(o['id'] for o in view['observations'] if o['measurement']['outcome']=='measured')
            return {'action':'answer','basis':'limited','refs':[no_send,measured],
                    'answer':'这两项探测的应答统计都是 3/3 ['+no_send+']['+measured+']。'}
        self.transport.answer=answer
        reply=self.ask(FOLLOWUPS[0])
        self.assertEqual('incomplete',reply['status'],reply)
        self.assertEqual(4,len(self.reads))

    def test_persisted_device_focus_is_enforced_for_defaults_widening_and_new_correction(self):
        self.measure()
        self.ask('更正一下，本轮只分析远端主机，局端主机留待后续，不再读设备。')
        limiter_now=self.client.server.request_limiter.clock()
        ticks=iter(range(1,100))
        self.client.server.request_limiter.clock=lambda:limiter_now+61*next(ticks)
        self.transport.requests.clear()
        reply=self.ask('按我补充的范围，解释已有证据和下一步缺项',recall=True)
        self.assertEqual({'remote-primary'},{o['device'] for o in reply['evidence']})
        for view in self.transport.requests[1:]:
            self.assertEqual({'remote-primary'},{o['device'] for o in view['observations']})
        self.transport.answer=lambda view: {'action':'recall_history','task_id':'latest','devices':['local-primary']}
        reply=self.ask('按我补充的范围，解释已有证据和下一步缺项')
        self.assertEqual('incomplete',reply['status'],reply)
        self.assertFalse(reply['evidence'])
        self.transport.answer=None
        self.ask('更正一下，改为只分析局端主机，远端留待后续。')
        reply=self.ask('按我补充的范围，解释已有证据和下一步缺项',recall=True)
        self.assertEqual({'local-primary'},{o['device'] for o in reply['evidence']})
        self.ask('更正一下，取消设备分析范围限制，恢复原范围。')
        reply=self.ask('按我补充的范围，解释已有证据和下一步缺项',recall=True)
        self.assertEqual({'local-primary','remote-primary'},{o['device'] for o in reply['evidence']})
        self.assertEqual(4,len(self.reads))

    def test_effective_device_focus_does_not_cross_account_conversation_or_mode_generation(self):
        data={'username':'scope-alice','password':'offline-only-password-123','allow_storage':True}
        with patch('server.user_accounts.ITERATIONS',1000):
            self.assertEqual(200,self.client.call('/api/account/register',data)[0])
            self.client.enter_diagnostic('diagnosis')
            self.measure()
            self.ask('更正一下，本轮只分析远端主机，不再读设备。')
            alice=self.client.opener
            self.client.opener=self.client.new_browser()
            try:
                self.assertEqual(200,self.client.call('/api/account/register',dict(data,username='scope-bob'))[0])
                self.client.enter_diagnostic('diagnosis')
                self.measure()
                reply=self.ask(FOLLOWUPS[0],recall=True)
                self.assertEqual({'local-primary','remote-primary'},{o['device'] for o in reply['evidence']})
            finally:
                self.client.opener=alice
            self.client.enter_diagnostic('other')
            _,pending=post_question(self.client,'实测局端和远端能不能互相通信','other')
            run_task(self.client,pending['task'])
            with patch.object(self.transport,'request_history',True):
                _,reply=post_question(self.client,FOLLOWUPS[0],'other')
            self.assertEqual({'local-primary','remote-primary'},{o['device'] for o in reply['evidence']})
            self.client.call('/api/conversations/diagnosis/mode',{'mode':'qa','scene':None})
            self.client.enter_diagnostic('diagnosis')
            self.measure()
            reply=self.ask(FOLLOWUPS[0],recall=True)
            self.assertEqual({'local-primary','remote-primary'},{o['device'] for o in reply['evidence']})
        self.assertEqual(16,len(self.reads))

    def test_correction_survives_recent_window_and_service_reconstruction(self):
        self.measure()
        limiter_now = self.client.server.request_limiter.clock()
        ticks = iter(range(1,100))
        self.client.server.request_limiter.clock = lambda: limiter_now+61*next(ticks)
        self.ask('更正一下，这次只分析管理地址，隧道留待后续验证。')
        for _ in range(12):
            code, reply = post_question(self.client,'继续说说刚才测试的发现。')
            self.assertEqual(200,code,reply)
        store = ApprovalStore(self.client.store.path)
        self.client.server.workflow = WorkflowService(store, self.planner, self.probe, lookup=lambda _: [])
        self.transport.requests.clear()
        self.ask('这个调查接下来怎么处理？',recall=True)
        self.assertTrue(self.transport.requests)
        payload = self.transport.requests[-1]
        context = json.dumps(payload.get('investigation_context', {}), ensure_ascii=False)
        self.assertIn('实测局端和远端能不能互相通信', context)
        self.assertIn('只分析管理地址，隧道留待后续验证', context)
        self.assertEqual({}, payload['tool_catalog'])
        self.assertEqual(4, len(self.reads))

    def test_new_failure_shadows_success_and_distinguishes_no_route_from_no_reply(self):
        first, _ = self.measure()
        self.ask(FOLLOWUPS[0],recall=True)
        self.assertTrue(any('未发包' in e['output'] for e in self.transport.requests[-1]['observations']))
        self.timeout = True
        second, _ = self.measure()
        self.ask(FOLLOWUPS[0],recall=True)
        observations = self.transport.requests[-1]['observations']
        self.assertEqual({second['id']}, {e['task_id'] for e in observations})
        self.assertTrue(all('0/3' in e['output'] for e in observations))
        self.failed = True
        third, _ = self.measure()
        reply = self.ask(FOLLOWUPS[0],recall=True)
        observations = self.transport.requests[-1]['observations']
        self.assertEqual({third['id']}, {e['task_id'] for e in observations})
        self.assertTrue(all(e['ok'] is False and '未取得' in e['output'] for e in observations))
        self.assertFalse(reply.get('refs'))
        self.assertNotIn(first['id'], json.dumps(observations))
        self.assertEqual(12, len(self.reads))

    def test_explicitly_recalling_earliest_probe_keeps_its_distinct_sources(self):
        first, _ = self.measure()
        self.timeout = True
        self.measure()
        self.transport.requests.clear()
        reply = self.ask('最早那次双向测试有几个包回来了？',recall=True)
        self.assertEqual([],self.transport.requests[0]['observations'])
        self.assertEqual(first['id'],self.transport.requests[0]['history_recall']['default_task_id'])
        evidence = reply['evidence']
        self.assertEqual({first['id']},{entry['task_id'] for entry in evidence})
        self.assertTrue(any('3/3' in entry['output'] for entry in evidence))
        self.assertTrue(all(entry['freshness']=='historical' for entry in evidence))
        self.assertEqual(8,len(self.reads))

    def test_unrelated_question_and_its_followup_do_not_reuse_diagnostic_context(self):
        self.measure()
        self.transport.requests.clear()
        for question in ('Python 的列表是什么？','它还能做什么？'):
            with self.subTest(question=question):
                reply = self.ask(question)
                self.assertEqual('answered',reply['status'],reply)
                self.assertFalse(self.transport.requests[-1].get('investigation_context'))
                self.assertFalse(self.transport.requests[-1]['observations'])
        self.assertEqual(4,len(self.reads))

    def test_self_contained_new_requests_do_not_receive_device_results(self):
        for question in ('讲讲光合作用的过程。', '写一个冒泡排序函数。',
                         '给我一份番茄炒蛋的做法。'):
            with self.subTest(question=question):
                self.measure()
                self.transport.requests.clear()
                reply = self.ask(question)
                self.assertEqual('answered',reply['status'],reply)
                self.assertTrue(self.transport.requests)
                self.assertFalse(reply.get('evidence'))
                for context in self.transport.requests:
                    self.assertFalse(context['observations'])
                    self.assertFalse(context.get('investigation_context'))
        self.assertEqual(12,len(self.reads))

    def test_time_or_evidence_words_in_an_unrelated_question_are_not_antecedents(self):
        for question in ('在高压锅开始排气之前需要放水吗？','天文学家已经看到哪些超新星？'):
            with self.subTest(question=question):
                self.measure()
                self.transport.requests.clear()
                reply = self.ask(question)
                self.assertEqual('answered',reply['status'],reply)
                self.assertTrue(self.transport.requests)
                self.assertFalse(reply.get('evidence'),reply)
                self.assertTrue(all(not view['observations'] for view in self.transport.requests))

    def test_open_ended_continuation_refusal_to_retest_and_correction_reuse_real_evidence(self):
        task, _ = self.measure()
        for question in ('把咱们已经证实的事和还拿不准的事分开说',
                         '上一项已经做完了，不要重复测，先解释差别',
                         '这事还有哪一环缺证据？',
                         '你是不是把没有发包当成丢包了？按实际执行情况更正'):
            with self.subTest(question=question):
                self.transport.requests.clear()
                reply = self.ask(question,recall=True)
                self.assertEqual('answered',reply['status'],reply)
                self.assertTrue(self.transport.requests,reply)
                payload = self.transport.requests[-1]
                self.assertEqual({},payload['tool_catalog'])
                self.assertEqual({task['id']},{e['task_id'] for e in payload['observations']})
        self.assertEqual(4,len(self.reads))

    def test_ambiguous_recap_fetches_scoped_history_only_after_planner_action(self):
        task, _ = self.measure()
        final = {'action':'answer','basis':'evidence','refs':['OBS-1'],
                 'answer':'这次取得了历史探测结果。[OBS-1]'}
        self.transport.answer = lambda view: (
            {'action':'recall_history','task_id':task['id'],'checks':['ping_remote_primary']}
            if not view['observations'] else final)
        self.transport.requests.clear()
        reply = self.ask('别从头来，把刚刚卡住的地方接着往下分析')
        self.assertEqual('answered',reply['status'],reply)
        first,second = self.transport.requests
        self.assertEqual([],first['observations'])
        self.assertTrue(first.get('history_recall'))
        self.assertEqual({},first['tool_catalog'])
        self.assertFalse(second.get('history_recall'))
        self.assertEqual(['ping_remote_primary'],[row['check'] for row in second['observations']])
        self.assertEqual(task['id'],reply['evidence'][0]['task_id'])
        self.assertEqual('historical',reply['evidence'][0]['freshness'])
        self.assertEqual(4,len(self.reads))

    def test_ambiguous_new_topic_answers_without_recalling_device_observations(self):
        self.measure()
        self.transport.answer = lambda view: {'action':'answer','basis':'general','refs':[],
            'answer':'植物光合作用可用于温室栽培和生态研究。'}
        for question in ('刚才提到的光合作用有什么实际应用？','最早进行光合作用的生物有哪些？'):
            with self.subTest(question=question):
                self.transport.requests.clear()
                reply = self.ask(question)
                self.assertEqual('answered',reply['status'],reply)
                self.assertFalse(reply.get('evidence'))
                self.assertEqual(1,len(self.transport.requests))
                self.assertEqual([],self.transport.requests[0]['observations'])
        self.assertEqual(4,len(self.reads))

    def test_recall_rejects_foreign_tasks_and_unlisted_checks(self):
        task, _ = self.measure()
        self.client.enter_diagnostic('other')
        _, pending = post_question(self.client,'实测局端和远端能不能互相通信','other')
        foreign = pending['task']
        run_task(self.client,foreign)
        for request in ({'task_id':foreign['id']},{'task_id':task['id'],'checks':['services']}):
            with self.subTest(request=request):
                self.transport.answer = lambda view: dict(action='recall_history',**request)
                self.transport.requests.clear()
                reply = self.ask('别从头来，把刚刚卡住的地方接着往下分析')
                self.assertEqual('incomplete',reply['status'],reply)
                self.assertFalse(reply.get('evidence'))
                self.assertTrue(self.transport.requests[0].get('history_recall'))
                self.assertTrue(all(not view['observations'] for view in self.transport.requests))
        self.assertEqual(8,len(self.reads))

    def test_only_one_history_recall_is_allowed_in_the_same_model_run(self):
        task, _ = self.measure()
        self.transport.answer = lambda view: {'action':'recall_history','task_id':task['id']}
        self.transport.requests.clear()
        reply = self.ask('别从头来，把刚刚卡住的地方接着往下分析')
        self.assertEqual('incomplete',reply['status'],reply)
        first,second = self.transport.requests
        self.assertTrue(first.get('history_recall'))
        self.assertFalse(second.get('history_recall'))
        self.assertEqual(1,len([e for e in reply['events'] if e['action']=='recall_history']))
        self.assertEqual(4,len(self.reads))

    def test_skipping_recall_cannot_assert_current_site_state(self):
        self.measure()
        self.transport.answer = lambda view: {'action':'answer','basis':'general','refs':[],
            'answer':'目前隧道已经恢复通信。'}
        reply = self.ask('别从头来，把刚刚卡住的地方接着往下分析')
        self.assertEqual('incomplete',reply['status'],reply)
        self.assertNotIn('目前隧道已经恢复通信',reply['answer'])
        self.assertFalse(reply['evidence'])
        self.assertEqual(4,len(self.reads))

    def test_catalog_omitted_tasks_do_not_supply_latest_scope_observations(self):
        initial, _ = self.measure()
        limiter_now = self.client.server.request_limiter.clock()
        ticks = iter(range(1,100))
        self.client.server.request_limiter.clock = lambda: limiter_now+61*next(ticks)
        for _ in range(12):
            self.read_backup_routes()
        self.transport.requests.clear()
        reply = self.ask(FOLLOWUPS[0],recall=True)
        offered = {entry['task_id'] for entry in self.transport.requests[0]['history_recall']['tasks']}
        self.assertNotIn(initial['id'],offered)
        self.assertTrue(all(entry['task_id'] in offered for request in self.transport.requests
                            for entry in request['observations']))
        self.assertTrue(reply['evidence'])
        self.assertTrue(all(entry['task_id'] in offered for entry in reply['evidence']))
        self.assertEqual(16,len(self.reads))

    def test_completion_claims_are_guarded_even_when_no_history_was_recalled(self):
        self.measure()
        limiter_now = self.client.server.request_limiter.clock()
        ticks = iter(range(1,200))
        self.client.server.request_limiter.clock = lambda: limiter_now+61*next(ticks)
        cases = (('故障已经修复，业务已恢复。','incomplete'),
                 ('尚未确认根因，业务已恢复。','incomplete'),
                 ('业务可用。','incomplete'),
                 ('业务可用！','incomplete'),
                 ('业务完全畅通。','incomplete'),
                 ('业务恢复正常。','incomplete'),
                 ('业务恢复通常需要多久，取决于故障类型。','answered'),
                 ('业务可用性的定义是业务提供服务的能力。','answered'),
                 ('业务恢复正常的过程需要结合故障类型。','answered'),
                 ('如果业务恢复，还应补充两端收发测试。','answered'),
                 ('尚未确认故障是否修复，也不能确认业务是否恢复。','answered'))
        for recall in (False,True):
            for claim, status in cases:
                with self.subTest(claim=claim,recall=recall):
                    self.transport.answer = lambda view: {'action':'answer','basis':'general','refs':[],
                        'answer':claim}
                    reply = self.ask('别从头来，把刚刚卡住的地方接着往下分析',recall=recall)
                    self.assertEqual(status,reply['status'],reply)
                    self.assertEqual(recall,bool(reply['evidence']))
        self.assertEqual(4,len(self.reads))

    def test_another_conversation_and_mode_generation_cannot_reuse_diagnostic_context(self):
        self.measure()
        self.client.enter_diagnostic('other')
        for conversation in ('other','diagnosis'):
            with self.subTest(conversation=conversation):
                if conversation == 'diagnosis':
                    self.client.call('/api/conversations/diagnosis/mode', {'mode':'qa','scene':None})
                    self.client.enter_diagnostic('diagnosis')
                self.transport.requests.clear()
                code, reply = post_question(self.client,FOLLOWUPS[0],conversation)
                self.assertEqual(200,code,reply)
                self.assertFalse(reply.get('evidence'))
                for context in self.transport.requests:
                    self.assertFalse(context.get('investigation_context'))
                    self.assertFalse(context['observations'])
        self.assertEqual(4,len(self.reads))

    def test_other_account_and_deleted_history_cannot_recover_original_evidence(self):
        data = {'username':'alice','password':'offline-only-password-123','allow_storage':True}
        with patch('server.user_accounts.ITERATIONS',1000):
            self.assertEqual(200,self.client.call('/api/account/register',data)[0])
            self.client.enter_diagnostic('diagnosis')
            self.measure()
            code, saved = self.client.call('/api/history', {'id':'diagnosis','title':'调查',
                'messages':[{'role':'user','content':'实测局端和远端能不能互相通信'}],'expected_revision':0})
            self.assertEqual(200,code,saved)
            alice = self.client.opener
            self.client.opener = self.client.new_browser()
            try:
                self.assertEqual(200,self.client.call('/api/account/register',dict(data,username='bob'))[0])
                self.client.enter_diagnostic('diagnosis')
                self.assertFalse(self.ask(FOLLOWUPS[0]).get('evidence'))
            finally:
                self.client.opener = alice
            code, deleted = self.client.call('/api/history/diagnosis/delete', {'expected_revision':saved['revision']})
            self.assertEqual(200,code,deleted)
            self.client.enter_diagnostic('diagnosis')
            self.transport.requests.clear()
            self.assertFalse(self.ask(FOLLOWUPS[0]).get('evidence'))
            for payload in self.transport.requests:
                self.assertFalse(payload.get('investigation_context'))
                self.assertFalse(payload['observations'])
        self.assertEqual(4,len(self.reads))

    def test_withdrawn_latest_use_does_not_revive_an_older_attempt(self):
        self.measure()
        recent, _ = self.measure()
        # Diagnostic use is persisted with the task. Model a withdrawn binding
        # in the isolated DB because completed tasks have no public revoke operation.
        with closing(self.client.store.connect()) as db, db:
            db.execute('UPDATE workflow_data_permissions SET approved_use=NULL WHERE task=?',(recent['id'],))
        self.transport.requests.clear()
        for question in (FOLLOWUPS[0],'所以到底通不通？'):
            self.assertFalse(self.ask(question).get('evidence'))
        for context in self.transport.requests:
            self.assertFalse(context['observations'])
        self.assertEqual(8,len(self.reads))

    def test_clearing_topic_does_not_restore_an_old_success_when_latest_use_is_withdrawn(self):
        self.measure()
        recent, _ = self.measure()
        with closing(self.client.store.connect()) as db, db:
            db.execute('UPDATE workflow_data_permissions SET approved_use=NULL WHERE task=?',(recent['id'],))
        self.ask('Python 的列表是什么？')
        reply = self.ask('最近一次现场探测的情况')
        self.assertFalse(reply.get('evidence'),reply)
        self.assertNotIn('3/3',reply['answer'])
        self.assertEqual(8,len(self.reads))

    def test_browser_echo_of_local_output_stays_out_of_model_history(self):
        self.read_backup_routes()
        mode = self.client.call('/api/conversations/diagnosis/mode')[1]
        self.transport.requests.clear()
        code, reply = self.client.call('/api/chat', {'conversation_id':'diagnosis',
            'question':'解释一下 Linux 策略路由的原理', 'allow_context':True,'chat_space':'ha',
            'interaction_mode':'diagnostic','interaction_revision':mode['revision'],'approval_flow':SINGLE_APPROVAL,
            'history':[{'role':'assistant','content':'最近一次结果是下一跳 192.0.2.1 [OBS-1]'}]})
        self.assertEqual(200,code,reply)
        self.assertTrue(self.transport.requests)
        self.assertNotIn('192.0.2.1',json.dumps(self.transport.requests))
        self.assertEqual(1,len(self.reads))

    def test_historical_citations_cannot_support_a_current_status_or_root_cause_claim(self):
        self.measure()
        self.transport.answer = lambda context: {'action':'answer','basis':'evidence','refs':['OBS-1'],
            'answer':'现在隧道已通，根因已经确认。[OBS-1]'}
        self.transport.requests.clear()
        reply = self.ask(FOLLOWUPS[0],recall=True)
        self.assertEqual('incomplete',reply['status'],reply)
        self.assertNotIn('现在隧道已通',reply['answer'])
        self.assertEqual('historical',reply['evidence_scope']['kind'])
        self.assertEqual(3,len(self.transport.requests))
        self.assertEqual(4,len(self.reads))

    def test_current_positive_and_negative_assertions_need_current_observations(self):
        self.measure()
        cases = (('目前隧道已经恢复通信。', 'incomplete'),
                 ('当前管理地址仍然不通。', 'incomplete'),
                 ('现在两端仍处于中断状态。', 'incomplete'),
                 ('当时没有发出探测，当前是否已通仍未核实。', 'answered'))
        for claim, status in cases:
            with self.subTest(claim=claim):
                self.transport.answer = lambda context: {'action':'answer','basis':'evidence',
                    'refs':['OBS-1'],'answer':claim+'[OBS-1]'}
                self.transport.requests.clear()
                reply = self.ask(FOLLOWUPS[0],recall=True)
                self.assertEqual(status,reply['status'],reply)
                self.assertEqual('historical',reply['evidence_scope']['kind'])
                self.assertEqual(3 if status=='incomplete' else 2,len(self.transport.requests))
                if status=='incomplete':
                    self.assertNotIn(claim,reply['answer'])
        self.assertEqual(4,len(self.reads))

    def test_mixed_recap_returns_both_sources_without_transporting_local_route_data(self):
        original, _ = self.measure()
        local = self.read_backup_routes()
        for question in (FOLLOWUPS[0], '刚才的探测和路由表检查分别查到了什么？',
                         '刚才的探测和路由表检查结果说明什么？'):
            with self.subTest(question=question):
                self.transport.requests.clear()
                reply = self.ask(question,recall=True)
                self.assertEqual('answered',reply['status'],reply)
                evidence = reply['evidence']
                self.assertEqual({original['id'],local},{e['task_id'] for e in evidence})
                self.assertEqual(5,len(evidence))
                self.assertEqual(len(evidence),len({e['id'] for e in evidence}))
                self.assertEqual({e['id'] for e in evidence},set(reply['refs']))
                self.assertIn('192.0.2.1',reply['answer'])
                for context in self.transport.requests:
                    self.assertNotIn('192.0.2.1',json.dumps(context))
                    self.assertFalse(any(e['task_id']==local for e in context['observations']))
        self.assertEqual(5,len(self.reads))

    def test_new_failure_during_recap_masks_old_success_before_transport(self):
        self.measure()
        entered = threading.Event()
        resume = threading.Event()
        original = self.client.service.explain_investigation
        responses = []
        self.transport.requests.clear()

        def held(*args):
            entered.set()
            if not resume.wait(5):
                raise AssertionError('recap was not resumed')
            return original(*args)

        with patch.object(self.client.service,'explain_investigation',held):
            thread = threading.Thread(target=lambda:responses.append(post_question(self.client,FOLLOWUPS[0])))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.failed = True
                self.measure()
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1,len(responses))
        _, result = responses[0]
        self.assertFalse(any(e['ok'] for e in result.get('evidence',[])),result)
        self.assertFalse(any(e['ok'] for view in self.transport.requests for e in view['observations']))
        self.assertEqual(8,len(self.reads))

    def test_new_failure_masks_cached_latest_local_answer_after_general_detour(self):
        self.measure()
        self.ask('Python 的列表是什么？')
        question = '最近一次现场探测的情况'
        entered = threading.Event()
        resume = threading.Event()
        from legacy.lab_workflow import topology_answer
        responses = []

        def held(*args):
            result = topology_answer(*args)
            if args[-2] == question and not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise AssertionError('local recap was not resumed')
            return result

        with patch('legacy.lab_workflow.topology_answer',held):
            thread = threading.Thread(target=lambda:responses.append(post_question(self.client,question)))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.failed = True
                self.measure()
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1,len(responses))
        _, reply = responses[0]
        self.assertFalse(any(entry['ok'] for entry in reply.get('evidence',[])),reply)
        self.assertNotIn('3/3',reply['answer'])
        self.assertEqual(8,len(self.reads))
