"""Group related questions, observations and approval transitions through the actual API."""
import time
import unittest

from approval_store import DIAGNOSTIC_DATA_USE, SINGLE_APPROVAL
from device_registry import profile
from workflow_test_support import RunningApp, current_probe_fixture


PAIR = {'remote-primary': ['interfaces'], 'remote-backup': ['interfaces']}
READ = {'action': 'read', 'device': 'local-primary', 'check': 'services'}
UNSUPPORTED = {'action': 'answer', 'answer': '已确认两端全都正常，业务可用。',
               'refs': ['OBS-1'], 'basis': 'evidence'}


def propose(goal, scope, continues=False):
    return {'action': 'prepare_diagnostic', 'goal': goal, 'scope': scope, 'continues_topic': continues}


def post_question(client, question, conversation='diagnosis', space='ha'):
    mode = client.call('/api/conversations/' + conversation + '/mode')[1]
    return client.call('/api/chat', {'conversation_id': conversation, 'question': question, 'history': [],
        'interaction_mode': mode['mode'], 'interaction_revision': mode['revision'], 'chat_space': space,
        'allow_context': True, 'approval_flow': SINGLE_APPROVAL})


def approve(client, task, background=False):
    return client.call('/api/tasks/' + task['id'] + '/decision',
                       {'approved': True, 'data_use': DIAGNOSTIC_DATA_USE, 'background': background})


def run_task(client, task):
    code, body = approve(client, task)
    if code != 200:
        raise AssertionError(body)
    code, result = client.call('/api/tasks/' + task['id'] + '/start', {})
    if code != 200:
        raise AssertionError(result)
    return result['result']


class DiagnosticDialogues(unittest.TestCase):
    def test_paraphrases_keep_vip_goal_and_different_observations_change_conclusion(self):
        followups = ('那现在谁在顶？', '这次轮到备用那台了吗？', '所以现在在哪边？')
        environments = ('remote-backup', 'both', 'missing', 'unavailable', 'stale')
        scenarios = [(question, 'remote-primary') for question in followups]
        scenarios.extend((followups[0], owner) for owner in environments)
        for question, owner in scenarios:
            with self.subTest(owner=owner, question=question):
                initial = '想看一下远端这一组的切换。'
                reads = []

                def planner(context):
                    if 'diagnostic_options' not in context:
                        # A premature or overconfident model answer must not decide VIP ownership.
                        return UNSUPPORTED, 0
                    self.assertFalse(context['tool_catalog'])
                    if context['question'] == initial:
                        return {'action': 'clarify', 'question': '你想了解切换规则还是核对当前接管节点？'}, 0
                    self.assertEqual(context['question'], question)
                    self.assertEqual(context['workflow_policy']['recent_topic']['question'], initial)
                    return propose('vip_ownership', PAIR, True), 0

                def probe(device, check):
                    reads.append((device, check))
                    item = current_probe_fixture(device, check)
                    if owner == 'unavailable' and device == 'remote-backup':
                        return dict(item, ok=False, output='')
                    if owner == 'stale' and device == 'remote-backup':
                        item['collected_at'] = '2000-01-01T00:00:00Z'
                    holding = owner in (device, 'both')
                    rows = [name + ' UP' + (' ' + vip + '/24' if holding else '')
                            for name, vip in profile(device)['ha_vips'].items()]
                    return dict(item, output='\n'.join(rows))

                with RunningApp(planner, probe, lookup=lambda q: []) as client:
                    self.assertEqual(client.enter_diagnostic('diagnosis')[0], 200)
                    self.assertEqual(post_question(client, initial)[1]['status'], 'clarify')
                    code, pending = post_question(client, question)
                    self.assertEqual(code, 200)
                    self.assertEqual(pending['task']['scope'], PAIR)
                    self.assertIn(initial, pending['task']['question'])
                    self.assertIn(question, pending['task']['question'])
                    self.assertFalse(reads)
                    result = run_task(client, pending['task'])
                self.assertCountEqual(reads, [(device, 'interfaces') for device in PAIR])
                assessment = result['goal_assessment']
                self.assertEqual(assessment['goal'], 'vip_ownership')
                self.assertEqual(result['status'], 'answered', result.get('reason'))
                self.assertNotIn('业务可用', result['answer'])
                if owner in PAIR:
                    self.assertEqual(assessment['status'], 'observed')
                    self.assertIn(('远端主机' if owner == 'remote-primary' else '远端备机') + '持有', result['answer'])
                elif owner in ('both', 'missing'):
                    self.assertEqual(assessment['status'], 'inconsistent')
                    self.assertIn('重复VIP' if owner == 'both' else '均未出现', result['answer'])
                else:
                    self.assertEqual(assessment['status'], 'unverified')
                    self.assertEqual(result['refs'], [])

    def test_missing_evidence_never_certifies_path_goals(self):
        cases = [
            ('business_path', '现在两个业务终端能发能收吗？', 'services',
             'Id=croc-sar.service\nLoadState=loaded\nActiveState=active\nSubState=running\nMainPID=38', 'Croc 运行'),
            ('business_path', '应用的数据到底到对面了没？', 'services',
             'Id=croc-sar.service\nLoadState=loaded\nActiveState=inactive\nMainPID=0', 'Croc 停止'),
            ('management_path', '设备之间能相互连吗？我指的是登录用的地址。', 'services',
             'service snapshot', '采集端通过登记管理路径取得状态'),
            ('tunnel', '那个虚拟通道还在不在？', 'interfaces', 'tun16 UNKNOWN 198.51.100.1/24', '存在 tun16'),
            ('tunnel', '隧道是不是没起来？', 'interfaces', 'INTERFACE_ABSENT=tun16', '未发现 tun16'),
            ('routing', '从这个源出来的数据实际走哪条？', 'routes', '0: from all lookup local', '取得主路由表和策略规则'),
            ('routing', '这股业务绕到哪去了？', 'routes', '', None),
        ]
        for goal, question, check, output, detail in cases:
            with self.subTest(goal=goal, question=question):
                scope = {'local-primary': [check]}
                reads = []

                def planner(context):
                    if 'diagnostic_options' in context:
                        return propose(goal, scope), 0
                    self.assertEqual(context['workflow_policy']['diagnostic_goal'], goal)
                    if context['observations']:
                        return UNSUPPORTED, 0
                    return dict(READ, check=check), 0

                def probe(device, check):
                    reads.append((device, check))
                    return dict(current_probe_fixture(device, check), output=output)

                with RunningApp(planner, probe, lookup=lambda q: []) as client:
                    client.enter_diagnostic('diagnosis')
                    _, pending = post_question(client, question)
                    self.assertFalse(reads)
                    result = run_task(client, pending['task'])
                self.assertEqual(reads, [('local-primary', check)])
                self.assertEqual(result['status'], 'answered', result.get('reason'))
                self.assertEqual(result['goal_assessment']['status'], 'unverified')
                self.assertEqual(result['basis'], 'limited')
                self.assertNotIn(UNSUPPORTED['answer'], result['answer'])
                if detail:
                    self.assertIn(detail, result['answer'])
                    self.assertEqual(result['refs'], ['OBS-1'])
                else:
                    self.assertEqual(result['refs'], [])
                if goal == 'business_path':
                    self.assertFalse(result['business_path']['end_to_end_verified'])

    def test_same_words_without_context_ask_for_specific_target(self):
        def planner(context):
            self.assertFalse(context['workflow_policy'].get('recent_topic'))
            return {'action': 'clarify', 'question': '你想核对局端还是远端的主备归属？'}, 0

        with RunningApp(planner, lookup=lambda q: []) as client:
            client.enter_diagnostic('diagnosis')
            _, result = post_question(client, '那现在谁在顶？')
            self.assertEqual(result['status'], 'clarify')
            self.assertEqual(client.store.list(), [])

    def test_outside_scope_requires_a_new_approval_for_only_the_new_check(self):
        scope = {'local-primary': ['services', 'interfaces']}
        new_scope = {'remote-primary': ['interfaces']}
        reads = []
        followup = '远端也照这个查一下。'

        def planner(context):
            if 'diagnostic_options' in context:
                return propose('tunnel', scope), 0
            if not context['observations']:
                if 'remote-primary' in context['tool_catalog']:
                    self.assertEqual(set(context['tool_catalog']), {'remote-primary'})
                    self.assertIn(followup, context['question'])
                    return {'action': 'read', 'device': 'remote-primary', 'check': 'interfaces'}, 0
                return READ, 0
            if context['observations'][0]['device'] == 'remote-primary':
                return UNSUPPORTED, 0
            if not context.get('task_thread'):
                self.assertEqual(context['scope_evidence_refs'], ['OBS-1'])
                return {'action': 'clarify', 'question': '接下来查局端接口还是也要核对另一端？'}, 0
            self.assertEqual(context['task_thread'][-1]['content'], followup)
            self.assertNotIn('interfaces', context['scope_options']['local-primary']['checks'])
            if not context['correction_feedback']:
                # Invalid proposal cannot add an already-approved check or an arbitrary target.
                return {'action': 'request_scope', 'scope': {**new_scope, 'local-primary': ['interfaces']}}, 0
            return {'action': 'request_scope', 'scope': new_scope}, 0

        def probe(device, check):
            reads.append((device, check))
            return dict(current_probe_fixture(device, check), output='tun16 UNKNOWN')

        with RunningApp(planner, probe, lookup=lambda q: []) as client:
            client.enter_diagnostic('diagnosis')
            _, initial = post_question(client, '先核对一下局端隧道。')
            task = initial['task']
            path = '/api/tasks/' + task['id']
            self.assertEqual(approve(client, task, True)[0], 200)
            try:
                for _ in range(120):
                    progress = client.store.get(task['id']).get('progress', {})
                    if progress.get('stage') == 'awaiting_input':
                        break
                    time.sleep(.025)
                else:
                    self.fail('did not wait for clarification')
                self.assertEqual(client.call(path+'/input', {'text': followup, 'message_id': 'supplement'})[0], 200)
                for _ in range(120):
                    parent = client.store.get(task['id'])
                    if parent['status'] == 'incomplete':
                        break
                    time.sleep(.025)
                else:
                    self.fail('did not generate pending scope')
                child = parent['result']['followup_task']
                self.assertEqual(child['scope'], new_scope)
                self.assertEqual(child['status'], 'pending')
                self.assertFalse(child['data_use_approved'])
                self.assertEqual(parent['scope'], scope)
                self.assertEqual(parent['result']['evidence'][0]['id'], 'OBS-1')
                self.assertEqual(client.call('/api/tasks/' + child['id'] + '/start', {})[0], 409)
                self.assertEqual(reads, [('local-primary', 'services')])
                result = run_task(client, child)
                self.assertIn('远端主机采集时存在 tun16', result['answer'])
                self.assertEqual(reads, [('local-primary', 'services'), ('remote-primary', 'interfaces')])
                self.assertEqual(len(client.store.list()), 2)
            finally:
                if client.store.get(task['id'])['status'] in ('approved', 'running'):
                    client.call(path+'/cancel', {})


if __name__ == '__main__':
    unittest.main()
