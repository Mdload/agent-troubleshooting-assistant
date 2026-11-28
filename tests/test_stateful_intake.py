"""Multi-turn HA intake with controlled actions, real HTTP and no device access."""
if not __package__:
    import _bootstrap

import unittest
from unittest.mock import Mock
import threading
from contextlib import closing
from chat_model import ModelFailure

from tests.workflow_test_support import RunningApp, current_probe_fixture
from tests.test_diagnostic_dialogues import PAIR, post_question, run_task


def clarify(brief, question='要核对哪一端？', continued=False):
    return {'action': 'clarify', 'question': question, 'brief': brief, 'continues_topic': continued}


def prepare(scope, brief, continued=True):
    return {'action': 'prepare_diagnostic', 'scope': scope, 'brief': brief, 'continues_topic': continued}


class StatefulIntakeTests(unittest.TestCase):
    def test_method_question_failure_never_becomes_a_symptom_question(self):
        question='我想验证局端到远端有没有通，应该要怎么做'
        for action in ({'action':'answer','answer':'先区分管理地址和tun16，再分别检查选路与应答。','refs':[],'basis':'limited'},
                       {'action':'answer','answer':None}):
            with self.subTest(valid=action.get('answer') is not None):
                def plan(context):
                    self.assertNotIn('diagnostic_options',context)
                    return action,0
                planner=Mock(side_effect=plan)
                tool=Mock()
                with RunningApp(planner,tool,lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    _,reply=post_question(client,question)
                    self.assertEqual('answered' if action['answer'] else 'incomplete',reply['status'],reply)
                    self.assertNotIn('你看到的是',reply['answer'])
                    self.assertNotEqual('clarify',reply['route'])
                    self.assertEqual([],client.store.list())
                    tool.assert_not_called()

    def test_whole_topology_status_reads_registered_nodes_without_asking_for_a_symptom(self):
        from device_registry import DEVICES,profile
        from ha_runbooks import local_intent
        self.assertEqual(local_intent('远端设备现在有异常吗',[],{})[:2],
                         ('topology_health',['remote-primary','remote-backup']))
        for q in ('当前拓扑有故障吗，不要查设备','假如当前拓扑有故障，怎么办','怎么看当前拓扑有没有故障'):
            self.assertIsNone(local_intent(q,[],{}),q)
        for fault in ('local-primary','healthy','timeout'):
            with self.subTest(fault=fault):
                def fixture(device,check):
                    if fault=='timeout' and device=='remote-backup':return {'ok':False,'error':'timeout','output':''}
                    primary=device.endswith('primary')
                    if check=='interfaces':
                        output='\n'.join(port+' UP'+(' '+vip+'/24' if primary else '')
                                         for port,vip in profile(device)['ha_vips'].items())
                    else:
                        running=primary and device!=fault
                        output=('Id='+profile(device)['croc_unit']+'\nLoadState=loaded\n'+
                                ('ActiveState=active\nSubState=running\nMainPID=42' if running else
                                 'ActiveState=inactive\nSubState=dead\nMainPID=0'))
                    return dict(current_probe_fixture(device,check),output=output)
                tool=Mock(side_effect=fixture)
                planner=Mock(side_effect=AssertionError('clear topology check must progress locally'))
                with RunningApp(planner,tool,lookup=lambda _:[]) as client:
                    client.service.auto_read_only=True
                    client.enter_diagnostic('diagnosis')
                    done=threading.Event()
                    background=client.service.run_background
                    def track(*args):
                        try:return background(*args)
                        finally:done.set()
                    client.service.run_background=track
                    _,reply=post_question(client,'那当前拓扑里有故障的设备吗')
                    self.assertEqual(reply['status'],'investigation_started',reply)
                    self.assertEqual(reply['task']['scope'],{d:['interfaces','services'] for d in DEVICES})
                    self.assertTrue(done.wait(6))
                    result=client.store.get(reply['task']['id'])['result']
                    first=result['answer'].splitlines()[0]
                    self.assertIn('信息不完整' if fault=='timeout' else '不一致' if fault!='healthy' else '未发现',first,
                                  str(client.service.audit.read(reply['task']['id'])[-3:]))
                    self.assertIn('路由器',result['answer'])
                    self.assertNotIn('你看到的是',result['answer'])
                self.assertEqual(tool.call_count,8)
                planner.assert_not_called()

    def test_backup_pair_probes_the_selected_chassis_tunnel_in_both_directions(self):
        from diagnostic_probes import extended_commands
        from device_registry import tunnel_endpoints
        from diagnostic_intake import bounded_request
        topic={'intake':{'phase':'scope_proposed','brief':{'goal':'icmp_reachability',
                           'devices':['local-primary','remote-backup'],'path':'management'}}}
        plan=bounded_request('两端的隧道互相ping一下',topic)
        self.assertEqual(plan['scope'],{'local-primary':['ping_tunnel_remote_backup'],
                                        'remote-backup':['ping_tunnel_peer']})
        self.assertEqual(tunnel_endpoints('remote-backup'),('203.0.113.134','203.0.113.1'))
        self.assertIn('203.0.113.134',extended_commands('local-primary','ping_tunnel_remote_backup')[0])
        planner=Mock(side_effect=AssertionError('fixed tunnel pair must not use the model'))
        tool=Mock(side_effect=lambda d,c:dict(current_probe_fixture(d,c),
                  output='ICMP_TX=3\nICMP_RX=3\nICMP_MODE=kernel_route\nICMP_ROUTE_DEV=tun16'))
        with RunningApp(planner,tool,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _,initial=post_question(client,'局端主机和远端备机相互ping一下')
            run_task(client,initial['task'])
            tool.reset_mock()
            _,proposed=post_question(client,'两端的隧道互相ping一下')
            self.assertEqual(proposed['task']['scope'],plan['scope'])
            tool.assert_not_called()
            result=run_task(client,proposed['task'])
            self.assertIn('203.0.113.134',result['answer'])
            self.assertNotIn('203.0.113.133',result['answer'])
            self.assertEqual({m['target'] for m in result['goal_assessment']['measurements']},
                             {'203.0.113.134','203.0.113.1'})
            self.assertEqual(len(result['refs']),2)
            from topology_observations import project
            facts=project('local-primary','ping_tunnel_remote_backup',result['evidence'][0]['output'])
            self.assertIn('203.0.113.134',facts['icmp'])
            from device_registry import allows_check
            self.assertFalse(allows_check('remote-backup','ping_tunnel_remote_backup'))
        self.assertCountEqual([c.args for c in tool.call_args_list],
                             [('local-primary','ping_tunnel_remote_backup'),('remote-backup','ping_tunnel_peer')])

    def test_reciprocal_communication_request_respects_path_and_topic(self):
        from diagnostic_intake import bounded_request,connectivity_measurement
        topic={'intake':{'brief':{'goal':'tunnel','path':'tunnel','devices':['local-primary','remote-primary']},
                         'phase':'scope_proposed'}}
        self.assertEqual(bounded_request('两端的隧道互相ping一下',topic)['scope'],
                         {'local-primary':['ping_tunnel_peer'],'remote-primary':['ping_tunnel_peer']})
        self.assertFalse(bounded_request('能否互相通信',None))
        for q in ('不要测两端互通','如果两端能互相通信会怎样','只看业务终端能不能互相通信','按指定源地址实测两端互通'):
            self.assertFalse(connectivity_measurement(q))

    def test_communication_followup_compares_paths_and_continues_corrected_pair(self):
        from device_registry import profile
        def sample(device, check):
            if check == 'interfaces':
                active = device.endswith('primary')
                output = '\n'.join(port+' UP'+(' '+address+'/24' if active else '')
                                   for port,address in profile(device)['ha_vips'].items())
            elif check == 'ping_remote_primary':
                output = 'ICMP_ROUTE_UNAVAILABLE=1'
            else:
                output = 'ICMP_TX=3\nICMP_RX=3\nICMP_MODE=kernel_route\nICMP_ROUTE_DEV=tun16'
            return dict(current_probe_fixture(device,check),output=output)
        probe=Mock(side_effect=sample)
        planner=Mock(side_effect=AssertionError('fixed pair must use the host runner'))
        with RunningApp(planner,probe,lookup=lambda _:[]) as client:
            client.service.auto_read_only=True
            client.enter_diagnostic('diagnosis')
            completed=threading.Event()
            background=client.service.run_background
            def track(*args):
                try:return background(*args)
                finally:completed.set()
            client.service.run_background=track
            _, opening=post_question(client,'远端和局端现在通吗')
            self.assertEqual(opening['status'],'investigation_started',opening)
            self.assertTrue(completed.wait(5))
            _, proposal=post_question(client,'我就想知道能不能互相通信')
            self.assertEqual(proposal['status'],'approval_required',proposal)
            scope={'local-primary':['ping_remote_primary','ping_tunnel_peer'],
                   'remote-primary':['ping_local_primary','ping_tunnel_peer']}
            self.assertEqual(proposal['task']['scope'],scope)
            self.assertEqual(probe.call_count,4)
            result=run_task(client,proposal['task'])
            self.assertCountEqual([c.args for c in probe.call_args_list[4:]],
                                  [(d,c) for d,checks in scope.items() for c in checks])
            self.assertEqual(set(result['refs']),{'OBS-1','OBS-2','OBS-3','OBS-4'})
            for term in ('tun16','双向','管理地址','未发包','业务收发'):
                self.assertIn(term,result['answer'])
            _, pair=post_question(client,'两端互相ping一下')
            run_task(client,pair['task'])
            before=probe.call_count
            _, method=post_question(client,'你不要指定源地址去ping不行吗')
            self.assertIn('系统选源',method['answer'])
            _, retry=post_question(client,'继续')
            self.assertEqual(retry['status'],'approval_required',retry)
            self.assertEqual(retry['task']['scope'],pair['task']['scope'])
            self.assertEqual(probe.call_count,before)
            repeated=run_task(client,retry['task'])
            self.assertEqual(len(repeated['refs']),2)
        planner.assert_not_called()

    def test_overview_keeps_observed_icmp_when_planner_approved_extra_directions(self):
        from diagnostic_goal import assess_goal
        scope={'local-primary':['ping_remote_primary','ping_remote_backup'],
               'remote-primary':['ping_local_primary']}
        evidence=[dict(device=d,check=c,id='OBS-'+str(i+1),origin='device_read',ok=True,
                       freshness='current',output='ICMP_ROUTE_UNAVAILABLE=1' if i==0 else 'ICMP_TX=3\nICMP_RX=3')
                  for i,(d,c) in enumerate((('local-primary','ping_remote_primary'),('remote-primary','ping_local_primary')))]
        result=assess_goal('connectivity_overview',evidence,scope)
        self.assertEqual(result['refs'],['OBS-1','OBS-2'])
        self.assertIn('收到 3 个应答',result['answer'])
        self.assertIn('未发包',result['answer'])

    def test_tunnel_peer_checks_use_registered_endpoint_and_require_matching_route(self):
        from device_tools import build_command,probe
        import subprocess
        for device,target in [('local-primary','203.0.113.133'),('remote-primary','203.0.113.1')]:
            command=build_command(device,'ping_tunnel_peer')[-1]
            self.assertIn('ip -4 route get '+target,command)
            self.assertNotIn(' -I ',command)
            runner=Mock(return_value=subprocess.CompletedProcess([],0,b'IDENTITY_OK\nICMP_TUNNEL_PATH_MISMATCH=1\n',b''))
            result=probe(device,'ping_tunnel_peer',runner)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['output'].strip(),'ICMP_TUNNEL_PATH_MISMATCH=1')

    def test_passive_socket_correlation_respects_manual_approval_and_device(self):
        output = ('ppp6 UNKNOWN 192.0.2.66/32\nppp6 UNKNOWN <POINTOPOINT,UP>\n'
                  'ESTAB 0 0 [::ffff:192.0.2.66]:41000 [::ffff:198.51.100.1]:9965')
        tool = Mock(side_effect=lambda device,check: dict(current_probe_fixture(device,check),output=output))
        planner = Mock(side_effect=AssertionError('fixed socket correlation needs no planner'))
        with RunningApp(planner, tool, lookup=lambda _:[]) as client:
            client.service.auto_read_only = False
            client.enter_diagnostic('diagnosis')
            _, proposed = post_question(client,'帮我核对远端备机的 PPP 拨号地址和 Croc 的 9965 连接。')
            self.assertEqual(proposed['status'],'approval_required')
            task = proposed['task']
            self.assertEqual(task['scope'], {'remote-backup':['interfaces']})
            self.assertEqual(client.call('/api/tasks/'+task['id']+'/start', {})[0],409)
            tool.assert_not_called()
            result = run_task(client, task)
        tool.assert_called_once_with('remote-backup','interfaces')
        planner.assert_not_called()
        self.assertIn('匹配 1 条已建立连接',result['answer'])
        self.assertEqual(result['refs'],['OBS-1'])

    def test_socket_sample_keeps_ambiguous_addresses_and_distinct_connections_separate(self):
        from link_inspection import ppp_summary
        output = ('ppp5 UP 192.0.2.50/32 192.0.2.51/32\neth1 UP 192.0.2.51/24\n'
                  'ESTAB 0 0 192.0.2.50:42000 198.51.100.1:9965\n'
                  'ESTAB 0 0 192.0.2.50:42000 198.51.100.1:9965\n'
                  'ESTAB 0 0 192.0.2.50:42001 198.51.100.1:9965\n'
                  'ESTAB 0 0 192.0.2.51:43000 198.51.100.1:9965')
        answer = ppp_summary(output, ('eth1','ppp5'), 'PPP 对应哪些 9965 连接？')
        self.assertIn('匹配 2 条',answer)
        self.assertIn('无法唯一归属',answer)
        self.assertEqual(ppp_summary(output, ('eth1','ppp5'), '看看 VIP 归属'), '')

    def test_resuming_a_direction_requires_matching_history_and_an_execution_request(self):
        from diagnostic_intake import resume_probe
        scope = {'remote-backup':['ping_local_primary']}
        question = '接着之前的那两台设备，按照上次的方向重测一次，仅确认有无回包。'
        self.assertEqual(resume_probe(question, {'scope':scope})['scope'], scope)
        self.assertFalse(resume_probe(question, None)['scope'])
        self.assertFalse(resume_probe(question, {'scope':dict(scope, **{'local-primary':['ping_remote_primary']})})['scope'])
        for changed in ('不要' + question, '解释一下' + question, question + '顺便双向测一下。',
                        '照原先的去向查路由表', '回到刚才那两台，只看有没有回应。'):
            self.assertIsNone(resume_probe(changed, {'scope':scope}), changed)

    def test_unknown_devices_are_accepted_only_for_clarification(self):
        from unified_chat import intake_decision, ProtocolFailure
        brief = {'goal':'icmp_reachability', 'devices':None, 'missing':'destination'}
        decision = intake_decision({'action':'clarify', 'brief':brief, 'question':'要测到哪台设备？'}, {}, True)
        self.assertEqual(decision['status'], 'clarify')
        self.assertEqual(decision['intake_brief']['devices'], [])
        with self.assertRaises(ProtocolFailure):
            intake_decision({'action':'prepare_diagnostic', 'brief':brief, 'scope':{}}, {}, True)

    def test_direction_limits_and_ambiguous_references(self):
        from diagnostic_intake import bounded_request
        for suffix in ('只查正向', '不要反向', '不做双向测试'):
            plan = bounded_request('从远端备机 ping 局端主机，' + suffix)
            self.assertEqual(plan['scope'], {'remote-backup': ['ping_local_primary']})
        ambiguous = bounded_request('远端主机和远端备机都有问题，先从局端主机 ping 它的管理地址')
        self.assertFalse(ambiguous['scope'])
        self.assertTrue(ambiguous['question'])
        self.assertIsNone(bounded_request('从远端主机 ping 局端备机可以证明什么？'))

    def test_probe_followups_explain_method_and_follow_route_correction(self):
        planner=Mock(side_effect=AssertionError('已知的探测追问无需模型'))
        probe=Mock(side_effect=lambda device,check:dict(current_probe_fixture(device,check),output=(
            'ICMP_TX=3\nICMP_RX=0\n' if check.startswith('ping_') else 'default dev tun7 table 200')))
        done=threading.Event()
        with RunningApp(planner,probe,lookup=Mock(side_effect=AssertionError('无需检索'))) as client:
            client.service.auto_read_only=True
            client.enter_diagnostic('diagnosis')
            original=client.service.run_background
            def track(*args):
                try:return original(*args)
                finally:done.set()
            client.service.run_background=track
            _,pending=post_question(client,'局端和远端互ping一下')
            run_task(client,pending['task'])
            _,method=post_question(client,'为什么没应答啊，你是怎么进的设备')
            self.assertEqual('answered',method['status'],method)
            self.assertIn('固定了源管理地址（-I）',method['answer'])
            self.assertEqual(2,probe.call_count)
            _,reply=post_question(client,'不是，你看看路由表啊，你这样指定网口乱ping能通吗')
            self.assertEqual('investigation_started',reply['status'],reply)
            self.assertTrue(done.wait(5))
            self.assertEqual({'local-primary':['policy_routes'],'remote-primary':['policy_routes']},reply['task']['scope'])
            self.assertCountEqual([call.args for call in probe.call_args_list[2:]],
                                  [('local-primary','policy_routes'),('remote-primary','policy_routes')])
            _,reply=post_question(client,'不要指定网口发ping包')
            self.assertIn('让系统选源地址和出口',reply['answer'])
            self.assertEqual(4,probe.call_count)
            planner.assert_not_called()

    def test_generic_fault_does_not_promote_ping_loss_to_topology_outage(self):
        scope={'local-primary':['ping_remote_primary'],'remote-primary':['ping_local_primary']}
        def planner(context):
            if 'diagnostic_options' in context:
                return prepare(scope,{'goal':'fault'},False),0
            catalog=context['tool_catalog']
            if catalog:
                device=next(iter(catalog))
                return {'action':'read','device':device,'check':next(iter(catalog[device]['checks']))},0
            return {'action':'answer','answer':'局端和远端不通。','basis':'evidence','refs':['OBS-1','OBS-2']},0
        probe=Mock(side_effect=lambda device,check:dict(current_probe_fixture(device,check),
                   output='ICMP_TX=3\nICMP_RX=0\nICMP_ROUTE_SRC=192.0.2.4\nICMP_ROUTE_DEV=tun16\n'))
        with RunningApp(planner,probe,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _,reply=post_question(client,'帮我查一下两边的连通故障。')
            result=run_task(client,reply['task'])
            self.assertIn('两个方向都没有收到 ping 应答',result['answer'])
            self.assertIn('出口 tun16',result['answer'])
            self.assertNotIn('局端和远端不通',result['answer'])

    def test_runbook_uses_evidence_to_choose_next_step_and_retains_targets(self):
        from device_registry import profile
        cases=(('远端的视频画面收不到了','logs','发现服务与 VIP 分布不一致'),
               ('帮我排查远端的 Croc 传输中断','policy_routes','业务端到端收发仍待验证'))
        for question,last_check,conclusion in cases:
            with self.subTest(question=question):
                planner=Mock(side_effect=AssertionError('本地调查无需模型'))
                done=threading.Event()
                def fixture(device,check):
                    active=device=='remote-backup'
                    config=profile(device)
                    if check=='interfaces':
                        output='\n'.join(port+' UP'+(' '+address+'/24' if active else '')
                                         for port,address in config['ha_vips'].items())
                    elif check=='services':
                        state='running' if active and last_check=='policy_routes' else 'stopped'
                        output='Id='+config['croc_unit']+'\nLoadState=loaded\n'
                        output+=('ActiveState=active\nSubState=running\nMainPID=88' if state=='running' else
                                 'ActiveState=inactive\nSubState=dead\nMainPID=0')
                    else:
                        output='LOG_RECORDS_RETURNED=1\nfixture' if check=='logs' else 'default dev tun7 table 200'
                    return dict(current_probe_fixture(device,check),output=output)
                probe=Mock(side_effect=fixture)
                with RunningApp(planner,probe,lookup=Mock(side_effect=AssertionError('不检索'))) as client:
                    client.service.auto_read_only=True
                    client.enter_diagnostic('diagnosis')
                    original=client.service.run_background
                    def track(*args):
                        try:return original(*args)
                        finally:done.set()
                    client.service.run_background=track
                    _,reply=post_question(client,question)
                    self.assertEqual('investigation_started',reply['status'],reply)
                    self.assertTrue(done.wait(5))
                    task=client.store.get(reply['task']['id'])
                    self.assertEqual('completed',task['status'],task)
                    self.assertIn(conclusion,task['result']['answer'])
                    self.assertFalse(task['result']['business_path']['end_to_end_verified'])
                    self.assertEqual([('remote-backup','interfaces'),('remote-primary','interfaces'),
                                      ('remote-backup','services'),('remote-primary','services'),
                                      ('remote-backup',last_check)],[call.args for call in probe.call_args_list])
                    done.clear()
                    _,followup=post_question(client,'接着看策略路由。')
                    self.assertEqual('investigation_started',followup['status'],followup)
                    self.assertEqual({'remote-backup':['policy_routes'],'remote-primary':['policy_routes']},
                                     followup['task']['scope'])
                    self.assertTrue(done.wait(5))
                    planner.assert_not_called()

    def test_model_selects_runbook_without_constructing_tool_scope(self):
        from device_registry import profile
        def planner(context):
            self.assertIn('ha_state',context['investigation_runbooks'])
            return {'action':'investigate','runbook':'ha_state','devices':['remote-primary','remote-backup']},0
        def fixture(device,check):
            output='\n'.join(port+' UP'+(' '+address+'/24' if device=='remote-backup' else '')
                             for port,address in profile(device)['ha_vips'].items())
            return dict(current_probe_fixture(device,check),output=output if check=='interfaces' else '')
        probe=Mock(side_effect=fixture)
        with RunningApp(Mock(side_effect=planner),probe,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _,reply=post_question(client,'现场这套到底哪台顶着？')
            self.assertEqual('approval_required',reply['status'],reply)
            self.assertEqual({'remote-primary':['interfaces','services'],'remote-backup':['interfaces','services']},reply['task']['scope'])
            probe.assert_not_called()
            result=run_task(client,reply['task'])
            self.assertIn('远端备机持有全部预期VIP',result['answer'])
            self.assertEqual([],result['model_trace'])

    def test_runbook_cannot_choose_outside_target_or_expand_restricted_question(self):
        for question in ('请处理远端备机目前的状况。','只查远端备机接口。'):
            with self.subTest(question=question):
                planner=Mock(return_value=({'action':'investigate','runbook':'croc_fault',
                                            'devices':['local-primary']},0))
                probe=Mock()
                with RunningApp(planner,probe,lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    _,reply=post_question(client,question)
                    if reply['status']=='approval_required':
                        self.assertEqual({'remote-backup':['interfaces']},reply['task']['scope'])
                    else:
                        self.assertEqual([],client.store.list())
                probe.assert_not_called()

    def test_reciprocal_ping_continues_completed_overview_through_background_approval(self):
        from device_registry import profile
        from tests.test_diagnostic_dialogues import approve

        scenarios=(('远端和局端现在通的吗','你互相发起ping包探测一下啊','remote-primary',False),
                   ('局端跟远端这会儿还连得上吗？','让两端各发一轮 ICMP','remote-backup',False),
                   ('局端和远端是通的吗？','两边分别ping一下','remote-primary',True))
        for opening,followup,remote,timeout in scenarios:
            with self.subTest(followup=followup):
                planner=Mock(return_value=({'action':'prepare_diagnostic','scope':{'remote':['ping']}},1805))
                lookup=Mock(return_value=[])
                done=threading.Event()
                def fixture(device,check):
                    if check=='interfaces':
                        owner=device in ('local-primary',remote)
                        lines=[f'{port} UP' + (f' {ip}/24' if owner else '') for port,ip in profile(device)['ha_vips'].items()]
                        return dict(current_probe_fixture(device,check),output='\n'.join(lines))
                    if timeout and device==remote:return {'ok':False,'error':'timeout','output':''}
                    replies=3 if device=='local-primary' else 0
                    return dict(current_probe_fixture(device,check),output=f'ICMP_TX=3\nICMP_RX={replies}')
                probe=Mock(side_effect=fixture)
                with RunningApp(planner,probe,lookup=lookup) as client:
                    client.service.auto_read_only=True
                    client.enter_diagnostic('diagnosis')
                    original=client.service.run_background
                    def track(*args):
                        try:return original(*args)
                        finally:done.set()
                    client.service.run_background=track
                    _,overview=post_question(client,opening)
                    self.assertEqual('investigation_started',overview['status'])
                    self.assertTrue(done.wait(5))
                    done.clear()
                    _,reply=post_question(client,followup)
                    self.assertEqual('approval_required',reply['status'],reply)
                    task=reply['task']
                    target='ping_'+remote.replace('-','_')
                    self.assertEqual({'local-primary':[target],remote:['ping_local_primary']},task['scope'])
                    self.assertEqual(4,probe.call_count)
                    self.assertIn('每个方向发 3 个 ping 包',reply['answer'])
                    code,approved=approve(client,task,background=True)
                    self.assertEqual(200,code,approved)
                    self.assertTrue(done.wait(5))
                    completed=client.store.get(task['id'])
                    self.assertEqual('completed',completed['status'],completed)
                    self.assertCountEqual([c.args for c in probe.call_args_list[4:]],
                                          [('local-primary',target),(remote,'ping_local_primary')])
                    result=completed['result']
                    self.assertEqual(1 if timeout else 2,len(result['refs']))
                    self.assertIn('收到 3 个应答',result['answer'])
                    if timeout:
                        self.assertEqual('unverified',result['goal_assessment']['status'])
                        self.assertIn('没有可判读的探测结果',result['answer'])
                    else:
                        self.assertIn('只有一个方向收到 ping 应答',result['answer'])
                    planner.assert_not_called()
                    lookup.assert_not_called()

    def test_reciprocal_ping_scope_respects_explicit_targets_and_topic_boundaries(self):
        from diagnostic_intake import bounded_request
        no_topic=bounded_request('互相 ping 一下')
        self.assertFalse(no_topic['scope'])
        self.assertIn('同一端',no_topic['question'])
        named=bounded_request('局端和远端互ping一下')
        self.assertEqual({'local-primary':['ping_remote_primary'],'remote-primary':['ping_local_primary']},named['scope'])
        for question in ('不要互相发起ping包','互相 ping 是什么意思？','局端和远端互 ping 203.0.113.1'):
            with self.subTest(question=question):
                plan=bounded_request(question)
                self.assertFalse(plan and plan['scope'])
        explicit=bounded_request('局端备机和远端主机相互ping一下')
        self.assertEqual({'local-backup':['ping_remote_primary'],'remote-primary':['ping_local_backup']},explicit['scope'])
        previous={'intake':{'phase':'scope_proposed','brief':{'goal':'tunnel','path':'tunnel',
                    'devices':['local-primary','local-backup','remote-primary','remote-backup']}}}
        self.assertFalse(bounded_request('互相ping一下',previous)['scope'])

    def test_current_side_to_side_question_starts_passive_checks_without_model(self):
        from device_registry import DEVICES,profile
        expected={device:['interfaces'] for device in DEVICES}
        for question in ('远端和局端现在通的吗','局端跟远端这会儿还连得上吗？',
                         '局端和远端是通的吗？','当前局端到远端的 Croc 隧道通不通？'):
            with self.subTest(question=question):
                planner=Mock(return_value=({'action':'prepare_diagnostic','goal':'management_path',
                                            'scope':{'remote':['ping']}},4480))
                done=threading.Event()
                def fixture(device,check):
                    if device=='remote-backup':return {'ok':False,'error':'timeout','output':''}
                    ports='\n'.join(port+' UP' for port in profile(device)['ha_vips'])
                    tunnel='tun16 UP 192.0.2.1/24' if device=='local-primary' else 'INTERFACE_ABSENT=tun16'
                    return dict(current_probe_fixture(device,check),output=ports+'\n'+tunnel)
                probe=Mock(side_effect=fixture)
                with RunningApp(planner,probe,lookup=lambda _:[]) as client:
                    client.service.auto_read_only=True
                    client.enter_diagnostic('diagnosis')
                    original=client.service.run_background
                    def track(*args):
                        try:return original(*args)
                        finally:done.set()
                    client.service.run_background=track
                    code,response=post_question(client,question)
                    self.assertEqual(200,code,response)
                    self.assertEqual('investigation_started',response['status'],response)
                    self.assertEqual(expected,response['task']['scope'])
                    self.assertTrue(response['task']['automatic_read'])
                    self.assertTrue(done.wait(5))
                    result=client.store.get(response['task']['id'])['result']
                    self.assertEqual('unverified',result['goal_assessment']['status'])
                    self.assertIn('局端主机：发现 tun16',result['answer'])
                    self.assertIn('远端备机：未取得本轮有效接口结果',result['answer'])
                    self.assertEqual({o['id'] for o in result['evidence'] if o['ok']},set(result['refs']))
                    local=next(x for x in result['goal_assessment']['interface_facts'] if x['device']=='local-primary')
                    self.assertEqual({'state':'UP','line':'tun16 UP 192.0.2.1/24'},local['interfaces']['tun16'])
                    self.assertFalse(result['model_trace'])
                    self.assertCountEqual([c.args for c in probe.call_args_list],[(d,'interfaces') for d in expected])
                    _,reply=post_question(client,'我问的是管理地址能不能互相访问。')
                    self.assertEqual('clarify',reply['status'],reply)
                    self.assertIn('哪台',reply['answer'])
                    self.assertNotIn('业务数据',reply['answer'])
                    self.assertEqual(4,probe.call_count)
                    planner.assert_not_called()

    def test_connectivity_explanation_and_general_space_do_not_start_a_probe(self):
        from diagnostic_intake import bounded_request
        for question in ('平时局端和远端为什么能通？','怎么判断局端和远端现在通不通？',
                         '假如现在局端和远端不通，会怎样？','现在局端和远端通吗，先别查。'):
            with self.subTest(question=question):self.assertIsNone(bounded_request(question))
        planner,probe=Mock(),Mock()
        with RunningApp(planner,probe,lookup=lambda _:[]) as client:
            client.service.auto_read_only=True
            code,response=post_question(client,'远端和局端现在通的吗',space='general')
            self.assertEqual(200,code,response)
            self.assertTrue(response['diagnostic_suggested'])
            self.assertEqual([],client.store.list())
        planner.assert_not_called();probe.assert_not_called()

    def test_interface_findings_distinguish_absent_missing_and_conflicting_output(self):
        from link_inspection import check_interfaces
        output='tun16 UP 192.0.2.1/24\ntun16 DOWN\nINTERFACE_ABSENT=tun7\neth2@peer LOWERLAYERDOWN'
        findings=check_interfaces(output,('tun16','tun7','eth2','eth3'))
        self.assertEqual({'state':'unconfirmed','line':None},findings['tun16'])
        self.assertEqual({'state':'absent','line':'INTERFACE_ABSENT=tun7'},findings['tun7'])
        self.assertEqual({'state':'LOWERLAYERDOWN','line':'eth2@peer LOWERLAYERDOWN'},findings['eth2'])
        self.assertEqual({'state':'unconfirmed','line':None},findings['eth3'])

    def test_automatic_passive_read_uses_background_http_and_starts_before_model(self):
        probe=Mock(side_effect=current_probe_fixture)
        done=threading.Event()
        planner=Mock(side_effect=AssertionError('本地只读结果不得发送给模型'))
        with RunningApp(planner,probe,lookup=lambda _:[]) as client:
            client.service.auto_read_only=True
            client.enter_diagnostic('diagnosis')
            original=client.service.run_background
            def track(*args):
                try:return original(*args)
                finally:done.set()
            client.service.run_background=track
            code,result=post_question(client,'只核对远端备机的 Keepalived 服务，其他项不用查。')
            self.assertEqual(200,code,result)
            self.assertEqual('investigation_started',result['status'],result)
            self.assertTrue(result['task']['automatic_read'])
            self.assertTrue(done.wait(5))
            probe.assert_called_once_with('remote-backup','services')
            task=client.store.get(result['task']['id'])
            self.assertEqual('completed',task['status'],task)
            self.assertEqual(1,task['attempts'])
            self.assertEqual([],task['result']['model_trace'])
            self.assertFalse(task['data_use_approved'])
            planner.assert_not_called()

    def test_auto_read_does_not_run_ping_mixed_requests_or_negated_ping(self):
        questions=(
            ('从局端主机 ping 远端备机的管理地址。',{'local-primary':['ping_remote_backup']}),
            ('从局端主机 ping 远端备机的管理地址，顺便查局端主机的服务。',
             {'local-primary':['ping_remote_backup','services']}),
            ('不要从局端主机 ping 远端备机的管理地址。',None),
        )
        for question, scope in questions:
            with self.subTest(question=question):
                probe=Mock()
                with RunningApp(Mock(),probe,lookup=lambda _:[]) as client:
                    client.service.auto_read_only=True
                    client.enter_diagnostic('diagnosis')
                    code,result=post_question(client,question)
                    self.assertEqual(200,code,result)
                    if scope:
                        self.assertEqual('approval_required',result['status'])
                        self.assertEqual(scope,result['task']['scope'])
                        with self.assertRaisesRegex(ValueError,'invalid_scope'):
                            from approval_store import AUTO_READ_ACTOR, LOCAL_READ_USE
                            task=result['task']['id']
                            with closing(client.store.connect()) as db:owner=client.store.row(db,task)['owner']
                            client.store.decide(task,True,AUTO_READ_ACTOR,LOCAL_READ_USE,owner,server_owned=True)
                    else:
                        self.assertEqual('current_unverified',result['status'])
                        self.assertEqual([],client.store.list())
                probe.assert_not_called()

    def test_failed_intake_consumes_answer_and_only_asks_for_remaining_endpoints(self):
        # Bad tool name reproduces the screenshot's invalid_action_or_citation.
        opening = '啥意思，是都不通吗'
        probe = Mock(side_effect=lambda device, check: dict(
            current_probe_fixture(device, check), output='ICMP_TX=3\nICMP_RX=2'))

        def planner(context):
            if context['workflow_policy']['mode'] == 'diagnostic':
                raise AssertionError('Fixed ICMP probe must not wait for the model')
            return prepare({'local-primary':['ping']}, {'goal':'management_path'}), 0

        with RunningApp(planner, probe, lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, first = post_question(client, opening)
            self.assertEqual('clarify', first['status'])
            _, second = post_question(client, '想确认能否互相访问')
            self.assertEqual('clarify', second['status'], second)
            self.assertIn('哪台', second['answer'])
            self.assertNotIn('还是两端的业务', second['answer'])
            self.assertNotEqual(first['answer'], second['answer'])
            _, third = post_question(client, '局端主机到远端备机')
            self.assertEqual('approval_required', third['status'], third)
            self.assertEqual({'local-primary':['ping_remote_backup']}, third['task']['scope'])
            probe.assert_not_called()
            result = run_task(client, third['task'])
            probe.assert_called_once_with('local-primary','ping_remote_backup')
            self.assertIn('收到 2 个应答', result['answer'])
            self.assertNotIn('业务恢复', result['answer'])
            self.assertEqual([],result['model_trace'])

    def test_endpoint_and_check_selection_do_not_depend_on_a_well_formed_model_action(self):
        cases = [
            ('从远端主机 ping 局端备机的管理地址。', {'remote-primary':['ping_local_backup']}),
            ('测一下局端备机到远端主机的管理网是否可达。', {'local-backup':['ping_remote_primary']}),
            ('局端主机和远端备机的管理地址双向各 ping 一次。',
             {'local-primary':['ping_remote_backup'],'remote-backup':['ping_local_primary']}),
            ('只核对远端备机 Keepalived 服务，其他项不用查。', {'remote-backup':['services']}),
            ('请查局端主机现在的隧道接口。', {'local-primary':['interfaces']}),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                planner = Mock(side_effect=ModelFailure('model_invalid_json'))
                probe = Mock()
                with RunningApp(planner, probe, lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    _, result = post_question(client, question)
                    self.assertEqual('approval_required', result['status'], result)
                    self.assertEqual(expected, result['task']['scope'])
                probe.assert_not_called()

    def test_directed_probe_does_not_guess_from_two_names_or_probe_other_addresses(self):
        questions = (
            '局端主机和远端备机的管理地址能互访吗？',
            '从局端主机 ping 远端备机的业务地址。',
            '如果从局端主机 ping 远端备机的管理地址，怎么理解丢包？',
            '从局端主机 ping 远端备机的管理地址，先别查。',
            '从局端主机 ping 远端备机的管理地址 203.0.113.3。',
        )
        for question in questions:
            with self.subTest(question=question):
                action = {'action':'answer','answer':'当前未核实。','basis':'limited','refs':[]}
                with RunningApp(Mock(return_value=(action,0)), Mock(), lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    _, result = post_question(client, question)
                    self.assertNotEqual('approval_required', result['status'], result)

    def test_business_followup_is_not_replaced_by_management_ping(self):
        opening='这俩是不是都不通？'
        probe=Mock()
        planner=Mock(return_value=(prepare({'local-primary':['ping']}, {'goal':'management_path'}), 0))
        with RunningApp(planner, probe, lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            self.assertEqual('clarify', post_question(client, opening)[1]['status'])
            _, result=post_question(client,'想确认两端的业务数据能否传过去。')
            self.assertEqual('current_unverified', result['status'], result)
            self.assertEqual('business_path', result['diagnostic_goal'])
            self.assertIn('核对发送和接收',result['answer'])
            self.assertEqual([],client.store.list())
        probe.assert_not_called()

    def test_clarification_updates_passive_goal_and_claim_preserves_brief(self):
        scenarios = [
            ('那台是不是已经接手了？', '看远端两台的虚拟地址。', 'vip_ownership', None, PAIR),
            ('这次数据从哪出去？', '查局端备机的策略路由。', 'routing', 'business', {'local-backup':['routes']}),
            ('程序现在是哪个状态？', '只看远端备机。', 'device_state', None, {'remote-backup':['services']}),
        ]
        for start, supplement, goal, path, scope in scenarios:
            with self.subTest(goal=goal):
                calls = []
                base = {'goal':goal,'path':path,'missing':'device'}

                def planner(context):
                    calls.append(context)
                    policy = context['workflow_policy']
                    if context['question'] == start:
                        return clarify(base), 0
                    if 'diagnostic_options' in context:
                        self.assertEqual(policy['diagnostic_intake']['brief']['goal'], goal)
                        self.assertEqual(policy['diagnostic_intake']['brief']['missing'], 'device')
                        self.assertEqual(policy['diagnostic_intake']['phase'], 'awaiting_input')
                        self.assertEqual(context['history'], [])
                        return prepare(scope, {'devices':list(scope),'missing':None}), 0
                    self.assertEqual(policy['diagnostic_brief'],
                        {'goal':goal,'path':path,'devices':sorted(scope),'missing':None})
                    if not context['observations']:
                        device = next(iter(context['tool_catalog']))
                        check = next(iter(context['tool_catalog'][device]['checks']))
                        return {'action':'read','device':device,'check':check}, 0
                    return {'action':'answer','answer':'已获得采集时状态，完整状态仍待核实。',
                            'refs':['OBS-1'],'basis':'limited'}, 0

                probe = Mock(side_effect=current_probe_fixture)
                with RunningApp(planner,probe,lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    self.assertEqual(post_question(client,start)[1]['status'],'clarify')
                    code, result = post_question(client,supplement)
                    self.assertEqual(code,200,result)
                    self.assertEqual(result['status'],'approval_required',result)
                    self.assertEqual(result['task']['diagnostic_goal'],goal)
                    self.assertEqual(result['task']['scope'],scope)
                    self.assertIn(start,result['task']['question'])
                    probe.assert_not_called()
                    finished=run_task(client,result['task'])
                    self.assertEqual(finished['status'],'answered',finished)
                self.assertEqual(calls[0]['tool_catalog'],{})
                self.assertEqual(calls[1]['tool_catalog'],{})
                self.assertCountEqual([call.args for call in probe.call_args_list],
                                      [(device,check) for device,checks in scope.items() for check in checks])

    def test_rephrased_repeat_gets_corrected_and_keeps_progress_if_unsolved(self):
        start = '想看一下是不是换过来了。'
        initial = {'goal':'vip_ownership','missing':'device'}
        for recovered in (True,False):
            with self.subTest(recovered=recovered):
                seen = []

                def planner(context):
                    seen.append(context)
                    if context['question'] == start:
                        return clarify(initial), 0
                    if context['correction_feedback'] and recovered:
                        return prepare(PAIR, {'missing':None}), 0
                    return clarify(initial,'你指的是局端还是远端？',True), 0

                probe=Mock()
                with RunningApp(planner,probe,lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    self.assertEqual(post_question(client,start)[1]['status'],'clarify')
                    _, result=post_question(client,'就是靠近发送端的那一组。')
                    self.assertEqual(len(seen),3)
                    self.assertEqual(seen[-1]['correction_feedback']['code'],'intake_no_progress')
                    self.assertEqual(result['status'],'approval_required' if recovered else 'current_unverified')
                    self.assertEqual(len(client.store.list()),int(recovered))
                    probe.assert_not_called()

    def test_unavailable_tools_do_not_turn_a_known_goal_into_a_proxy_read(self):
        scenarios = [('这台现在加载的是哪个配置？','live_configuration')]
        for question,goal in scenarios:
            with self.subTest(goal=goal):
                plan=Mock(return_value=(prepare({'local-primary':['services']},{'goal':goal},False),0))
                probe=Mock()
                with RunningApp(plan,probe,lookup=lambda _:[]) as client:
                    client.enter_diagnostic('diagnosis')
                    _, result=post_question(client,question)
                    self.assertEqual(result['status'],'current_unverified')
                    self.assertIn('无法',result['answer'])
                    self.assertEqual(client.store.list(),[])
                probe.assert_not_called()

        plan=Mock(return_value=({'action':'unavailable','brief':{'goal':'device_state'}},0))
        with RunningApp(plan,Mock(),lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, result=post_question(client,'能直接看这台的板载温度吗？')
            self.assertEqual(result['status'],'current_unverified')
            self.assertEqual(result['diagnostic_goal'],'device_state')
            self.assertEqual(result['model_trace'][0]['action'],'unavailable')
            self.assertEqual(client.store.list(),[])

    def test_new_topic_and_conversation_do_not_inherit_incomplete_goal(self):
        start='当前是不是远端备机顶上去了？'
        draft={'goal':'vip_ownership','devices':['remote-backup'],'missing':'device'}
        calls=[]

        def planner(context):
            calls.append(context)
            if context['question']==start:
                return clarify(draft),0
            return prepare({'local-primary':['services']},{'goal':'device_state'},False),0

        with RunningApp(planner,Mock(),lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            client.enter_diagnostic('separate')
            post_question(client,start)
            _, unrelated=post_question(client,'现在看看局端主机的服务状态。','separate')
            self.assertNotIn('diagnostic_intake',calls[-1]['workflow_policy'])
            self.assertEqual(unrelated['status'],'approval_required')
            _, replaced=post_question(client,'现在看看局端主机的服务状态。')
            self.assertEqual(replaced['task']['diagnostic_goal'],'device_state')
            self.assertEqual(replaced['task']['scope'],{'local-primary':['services']})
            self.assertNotIn(start,replaced['task']['question'])

    def test_model_cannot_expand_scope_via_brief_or_scope(self):
        decision=prepare({'remote-backup':['services']},
                         {'goal':'device_state','devices':['remote-backup']},False)
        probe=Mock()
        with RunningApp(lambda _: (decision,0),probe,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, result=post_question(client,'请排查局端主机当前 Croc 服务为什么异常。')
            self.assertEqual(result['status'],'clarify')
            self.assertEqual(client.store.list(),[])
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
