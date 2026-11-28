"""Verify fixed tool arguments and the approved investigation against substitute devices."""
if not __package__:
    import _bootstrap

import os
from contextlib import closing
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from approval_store import DIAGNOSTIC_DATA_USE
from device_registry import PING_TARGETS, checks_for
from device_tools import build_command, commands_for, probe
from device_worker import dispatch
from diagnostic_probes import KEEPALIVED_AWK
from investigation_limits import requested_limits
from tests.test_diagnostic_dialogues import post_question, run_task
from tests.workflow_test_support import RunningApp, current_probe_fixture


class ExtendedDiagnostics(unittest.TestCase):
    def test_completed_goal_skips_final_model_and_optional_explanation_keeps_answer(self):
        from diagnostic_core import AuditStore
        from unified_chat import run

        required = {'local-primary': ['policy_routes']}
        read = {'action': 'read', 'device': 'local-primary', 'check': 'policy_routes'}
        answer = '策略规则和路由表已采集，实际转发路径仍需核实。[OBS-1]'
        proposal = {'action': 'answer', 'answer': answer, 'refs': ['OBS-1'], 'basis': 'limited'}
        for checks in (['policy_routes'], ['policy_routes', 'interfaces']):
            with self.subTest(checks=checks), tempfile.TemporaryDirectory() as tmp:
                planner = Mock(side_effect=[(read, 5), (proposal, 7)])
                tool = Mock(side_effect=current_probe_fixture)
                result = run('查看局端主机的策略路由', [], planner, lambda _: [], tool,
                             AuditStore(Path(tmp) / 'audit.db'),
                             permitted_checks={'local-primary': checks},
                             question_policy={'mode': 'diagnostic', 'diagnostic_goal': 'routing',
                                              'required_checks': required})
                tool.assert_called_once_with('local-primary', 'policy_routes')
                self.assertEqual(result['status'], 'answered', result)
                self.assertEqual(result['refs'], ['OBS-1'])
                if len(checks) == 1:
                    planner.assert_called_once()
                    self.assertEqual(result['total_tokens'], 5)
                    self.assertIn('全部路由表', result['answer'])
                else:
                    self.assertEqual(planner.call_count, 2)
                    self.assertEqual(result['total_tokens'], 12)
                    self.assertIn(answer, result['answer'])

    def test_partial_bidirectional_measurement_keeps_the_measured_direction(self):
        from diagnostic_goal import assess_icmp
        scope = {'remote-backup': ['ping_local_primary'], 'local-primary': ['ping_remote_backup']}
        observations = [
            {'device': 'remote-backup', 'check': 'ping_local_primary', 'id': 'OBS-1', 'ok': True,
             'origin': 'device_read', 'freshness': 'current', 'output': 'ICMP_TX=3\nICMP_RX=2\n'},
            {'device': 'local-primary', 'check': 'ping_remote_backup', 'id': 'OBS-2', 'ok': False,
             'origin': 'device_read', 'freshness': 'current', 'output': ''},
        ]
        result = assess_icmp('icmp_reachability', observations, scope)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['refs'], ['OBS-1'])
        self.assertEqual([(item['source'], item['target'], item['received'], item['ref'])
                          for item in result['measurements']],
                         [('remote-backup', 'local-primary', 2, 'OBS-1'), ('local-primary', 'remote-backup', None, None)])

    def test_directed_ping_has_registered_source_target_and_bounds(self):
        source, check = 'local-primary', 'ping_remote_backup'
        steps=commands_for(source,check)[0].splitlines()
        self.assertIn('ip -4 route get 198.51.100.134',steps[0])
        self.assertIn('exit 0',steps[0])
        command=shlex.split(next(line for line in steps if line.startswith('LC_ALL=C ping ')))
        self.assertEqual(command, ['LC_ALL=C','ping','-4','-n','-q',
                                   '-c','3','-W','1','198.51.100.134'])
        self.assertNotIn('ping_local_primary', checks_for(source))
        with self.assertRaises(ValueError):
            build_command(source,'ping_local_primary')
        with self.assertRaises(ValueError):
            build_command(source,'ping_1_2_3_4')

    def test_worker_discovery_never_reads_devices_and_refuses_unregistered_fields(self):
        with patch('device_worker.probe') as probe_call:
            caps = dispatch({'operation':'capabilities'})
            self.assertEqual(caps['device_reads'],0)
            self.assertIn('ha_config',caps['devices']['remote-primary'])
            self.assertNotIn('ping_remote_primary',caps['devices']['remote-primary'])
            with self.assertRaises(ValueError):
                dispatch({'device':'remote-primary','check':'ha_config','path':'/etc/shadow'})
            probe_call.assert_not_called()

    def test_ping_stats_preserve_loss_without_promoting_command_errors(self):
        for stats, expected in ((b'3 packets transmitted, 0 received, 100% packet loss', 'ICMP_RX=0'),
                                (b'3 packets transmitted, 2 packets received, 33% packet loss', 'ICMP_RX=2'),
                                (b'ping: Operation not permitted', None),
                                (b'3 packets transmitted, 4 received', None)):
            executor = Mock(return_value=subprocess.CompletedProcess([],0,b'IDENTITY_OK\n'+stats+b'\n',b''))
            item = probe('remote-primary','ping_local_backup',executor)
            self.assertEqual(item['ok'],expected is not None,item)
            if expected:
                self.assertIn(expected,item['output'])
            else:
                self.assertEqual(item['output'],'')
                self.assertEqual(item['error'],'extended_check_unavailable')

    def test_probe_reports_kernel_route_and_does_not_count_no_route_as_loss(self):
        for raw,wanted in ((b'ICMP_ROUTE=192.0.2.13 via 192.0.2.1 dev tun16 src 192.0.2.2\n3 packets transmitted, 2 received',
                            'ICMP_TX=3\nICMP_RX=2\nICMP_MODE=kernel_route\nICMP_ROUTE_SRC=192.0.2.2\nICMP_ROUTE_DEV=tun16\nICMP_ROUTE_VIA=192.0.2.1\n'),
                           (b'ICMP_ROUTE_UNAVAILABLE=1','ICMP_ROUTE_UNAVAILABLE=1\n')):
            runner=Mock(return_value=subprocess.CompletedProcess([],0,b'IDENTITY_OK\n'+raw+b'\n',b''))
            result=probe('remote-primary','ping_local_primary',runner)
            self.assertTrue(result['ok'])
            self.assertEqual(wanted,result['output'])

    @unittest.skipIf(os.name == 'nt','awk projection requires Linux')
    def test_keepalived_projection_omits_secrets_and_include_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp)/'keepalived.conf'
            conf.write_text('''include /private/path.secret
vrrp_instance DEVICE_PRIVATE_NAME {
    interface eth4
    unicast_src_ip 192.0.2.2
    unicast_peer {
        192.0.2.3
    }
    priority 100
    authentication {
        auth_pass secret999
    }
}
''',encoding='utf-8')
            result = subprocess.run(['awk', KEEPALIVED_AWK, str(conf)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=3, check=True, universal_newlines=True)
        self.assertIn('interface=eth4',result.stdout)
        self.assertIn('peer=192.0.2.3',result.stdout)
        self.assertIn('INCLUDES=1',result.stdout)
        self.assertNotIn('secret',result.stdout)
        self.assertNotIn('DEVICE_PRIVATE_NAME',result.stdout)

    def test_approved_directed_probe_runs_before_premature_answer(self):
        scope = {'local-primary':['ping_remote_backup']}
        reads = []

        def planner(context):
            if 'diagnostic_options' in context:
                return {'action':'prepare_diagnostic','scope':scope,'brief':{'goal':'icmp_reachability'}},0
            return {'action':'answer','answer':'已确认业务完全正常。','refs':[],'basis':'limited'},0

        def fixture(device,check):
            reads.append((device,check))
            return dict(current_probe_fixture(device,check),output='ICMP_TX=3\nICMP_RX=2\n')

        with RunningApp(planner,fixture,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            code, pending=post_question(client,'从局端主机 ping 一下远端备机的管理地址。')
            self.assertEqual(code,200,pending)
            self.assertEqual(pending['status'],'approval_required',pending)
            self.assertEqual(pending['task']['scope'],scope)
            self.assertFalse(reads)
            result=run_task(client,pending['task'])
        self.assertEqual(reads,[('local-primary','ping_remote_backup')])
        self.assertEqual(result['goal_assessment']['status'],'observed',result)
        self.assertIn('收到 2 个应答',result['answer'])
        self.assertNotIn('业务完全正常',result['answer'])
        self.assertEqual(result['refs'],['OBS-1'])

    def test_approval_requires_a_direct_probe_for_icmp(self):
        action = {'action':'prepare_diagnostic','scope':{'local-primary':['interfaces']},
                  'brief':{'goal':'icmp_reachability'}}
        remote = Mock()
        with RunningApp(lambda _: (action,0),remote,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, result=post_question(client,'从局端主机检查到远端备机能不能 ping。')
            self.assertEqual(result['status'],'clarify',result)
            self.assertEqual(client.store.list(),[])
        remote.assert_not_called()

    def test_probe_evidence_can_propose_one_new_check_without_a_user_supplement(self):
        initial={'local-primary':['ping_remote_backup']}
        new_scope={'local-primary':['policy_routes']}
        reads=[]

        def planner(context):
            if 'diagnostic_options' in context:
                return {'action':'prepare_diagnostic','scope':initial,'goal':'icmp_reachability'},0
            observations=context['observations']
            if not observations:
                device=next(iter(context['tool_catalog']))
                check=next(iter(context['tool_catalog'][device]['checks']))
                return {'action':'read','device':device,'check':check},0
            if observations[-1]['check']=='policy_routes':
                self.assertEqual(context['workflow_policy']['diagnostic_goal'],'routing')
                return {'action':'answer','answer':'已读取路由表，完整转发待核实。','refs':['OBS-1'],'basis':'limited'},0
            self.assertEqual(context['task_thread'],[])
            self.assertEqual(context['scope_evidence_refs'],['OBS-1'])
            refs=['OBS-1'] if context['correction_feedback'] else ['OBS-99']
            return {'action':'request_scope','scope':new_scope,'goal':'routing','refs':refs},0

        def fixture(device,check):
            reads.append((device,check))
            output='ICMP_TX=3\nICMP_RX=0\n' if check.startswith('ping_') else 'default dev eth2 table 100'
            return dict(current_probe_fixture(device,check),output=output)

        with RunningApp(planner,fixture,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, pending=post_question(client,'从局端主机到远端备机，先测管理地址；有问题就定位。')
            task=pending['task']
            path='/api/tasks/'+task['id']
            self.assertEqual(client.call(path+'/decision',{'approved':True,'background':True,
                'data_use':DIAGNOSTIC_DATA_USE})[0],200)
            for _ in range(100):
                parent=client.store.get(task['id'])
                if parent['status'] not in ('approved','running'):
                    break
                time.sleep(.025)
            else:
                self.fail('background investigation did not finish')
            child=parent['result']['followup_task']
            self.assertEqual(child['scope'],new_scope)
            self.assertEqual(child['diagnostic_goal'],'routing')
            self.assertFalse(child['data_use_approved'])
            self.assertEqual(parent['task_inputs'],[])
            self.assertEqual(reads,[('local-primary','ping_remote_backup')])
            self.assertEqual(client.call('/api/tasks/'+child['id']+'/start',{})[0],409)
            result=run_task(client,child)
            self.assertEqual(result['status'],'answered',result)
        self.assertEqual(reads,[('local-primary','ping_remote_backup'),('local-primary','policy_routes')])
        self.assertIn('全部路由表',result['answer'])

    def test_read_only_profile_uses_one_grant_then_stops_after_relevant_checks(self):
        reads=[]
        def planner(context):
            if 'diagnostic_options' in context:
                self.assertEqual(context['diagnostic_options']['local-primary']['profiles']['read_only'],
                                 ['services','interfaces','policy_routes','logs','ha_config'])
                return {'action':'prepare_diagnostic','brief':{'goal':'fault'},
                        'scope':{'local-primary':['read_only']}},0
            pending=['services','logs','policy_routes']
            if len(context['observations'])<len(pending):
                return {'action':'read','device':'local-primary','check':pending[len(context['observations'])]},0
            self.assertIn('ha_config',context['tool_catalog']['local-primary']['checks'])
            return {'action':'answer','answer':'已取得服务、日志和策略路由线索，故障根因仍待核实。',
                    'refs':['OBS-1','OBS-3'],'basis':'limited'},0

        def fixture(device,check):
            reads.append((device,check))
            return dict(current_probe_fixture(device,check),output='partial diagnostic evidence')

        with RunningApp(planner,fixture,lookup=lambda _:[]) as client:
            client.enter_diagnostic('diagnosis')
            _, proposed=post_question(client,'请检查局端主机连接异常的原因。')
            task=proposed['task']
            self.assertEqual(task['scope'],{'local-primary':['services','interfaces','policy_routes','logs','ha_config']})
            self.assertEqual(task['execution_limits']['max_reads'],5)
            self.assertEqual(reads,[])
            result=run_task(client,task)
            self.assertEqual(result['status'],'answered',result)
            with closing(client.store.connect()) as db:
                count=db.execute("SELECT count(*) FROM workflow_events WHERE task=? AND kind='approved'",
                                 (task['id'],)).fetchone()[0]
            self.assertEqual(count,1)
        self.assertEqual(reads,[('local-primary','services'),('local-primary','logs'),('local-primary','policy_routes')])

    def test_policy_tool_reads_both_rules_and_non_main_tables(self):
        self.assertEqual(commands_for('local-backup','policy_routes'),
                         ('LC_ALL=C ip -4 rule show','LC_ALL=C ip -4 route show table all'))
        self.assertEqual(len(PING_TARGETS),6)
        checks=list(checks_for('local-backup'))
        limits=requested_limits({'local-backup':checks},staged=True)
        self.assertEqual(limits['max_reads'],min(12,len(checks)))
        self.assertEqual(limits['reads_per_stage'],4)


if __name__ == '__main__':
    unittest.main()
