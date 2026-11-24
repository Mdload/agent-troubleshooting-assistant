"""Router候选工具隔离和业务路径覆盖；无外部模型或真实设备调用。"""
import datetime
import json
import shlex
import unittest
from unittest.mock import patch
from business_path import assess_path, selected
from router_contract import read_commands, assess_replay
from device_registry import DEVICES, ROUTER_CONTRACT, PENDING_DEVICES, public_catalog
from device_tools import build_command
from diagnostic_logs import project_logs
from device_worker import dispatch
from ha_case_test_support import case_probe, case_planner
from test_investigation_stages import request, approve, start
from workflow_test_support import RunningApp

REPLAY_SCOPE={device:['services','interfaces','routes'] for device in
              ('remote-primary','router-1','router-2','local-primary')}


def path_rows(forward='1'):
    now=datetime.datetime.utcnow().isoformat()+'Z';rows=[]
    for device,checks in REPLAY_SCOPE.items():
        for check in checks:
            if device in DEVICES:output=case_probe('owner_running')(device,check)['output']
            elif check=='services':
                output='Id=keepalived.service\nLoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\n'
            elif check=='interfaces':
                output='\n'.join(iface+' UP'+(' '+vip+'/24' if device=='router-1' else '')
                                 for iface,vip in ROUTER_CONTRACT['ha_vips'].items())
            else:output='default via 192.0.2.15 dev eth5\nIP_FORWARD='+forward
            rows.append({'id':'OBS-'+str(len(rows)+1),'device':device,'check':check,
                'ok':True,'origin':'synthetic','freshness':'synthetic','collected_at':now,'output':output})
    return rows


class RouterContractTests(unittest.TestCase):
    def test_candidates_are_visible_but_cannot_be_approved_or_executed_as_runtime_devices(self):
        self.assertTrue(set(PENDING_DEVICES).isdisjoint(DEVICES))
        catalog=public_catalog()
        self.assertTrue(all(not row['execution_enabled'] and row['management_interface'] is None for row in catalog['pending_devices']))
        with patch('device_worker.probe') as probe:
            for device in PENDING_DEVICES:
                with self.assertRaises(ValueError):dispatch({'device':device,'check':'routes'})
                with self.assertRaises(ValueError):build_command(device,'routes')
            probe.assert_not_called()

    def test_fixed_commands_read_router_state_without_croc_or_side_effect_scripts(self):
        for check in ('services','interfaces','routes','logs'):
            text='\n'.join(read_commands(check))
            for forbidden in ('croc','restart','start ','route_envset.sh','notify.sh','health.sh','ready.sh','sysctl -w'):
                self.assertNotIn(forbidden,text.lower())
        self.assertIn('cat /proc/sys/net/ipv4/ip_forward',read_commands('routes')[-1])
        self.assertIn('route-envset.service',read_commands('services')[0])
        self.assertIn('NetworkManager.service',read_commands('logs')[0])
        for invalid in ('reboot','services; id',[],None):
            with self.assertRaises(ValueError):read_commands(invalid)

    def test_router_log_projection_keeps_only_safe_fields_and_filters_secrets(self):
        raw='\n'.join(json.dumps(row) for row in (
            {'_SYSTEMD_UNIT':'keepalived.service','MESSAGE':'Entering BACKUP STATE','SECRET_FIELD':'hidden'},
            {'_SYSTEMD_UNIT':'route-envset.service','MESSAGE':'password=fixture-secret'})).encode()
        view=project_logs(raw,group='router')
        self.assertIn('Entering BACKUP STATE',view)
        self.assertNotIn('fixture-secret',view);self.assertNotIn('SECRET_FIELD',view)
        self.assertIn('NetworkManager.service',view)
        for invalid in ('arbitrary.service',[]):
            with self.assertRaises(ValueError):project_logs(raw,group=invalid)

    def test_forwarding_zero_is_a_synthetic_field_and_never_confirms_root_cause(self):
        report=assess_replay(path_rows('0'),REPLAY_SCOPE)
        self.assertEqual(report['nodes'][0]['fields']['routes']['value'],'0')
        self.assertTrue(all(node['current_role']=='unknown' and not node['root_cause_confirmed'] for node in report['nodes']))
        self.assertFalse(report['grants_device_access'])

    def test_failed_duplicate_wrong_origin_time_and_scope_do_not_become_replay_fields(self):
        for kind in ('failed','duplicate','wrong_origin','bad_time','outside_scope','ambiguous_forwarding'):
            rows=path_rows();scope=dict(REPLAY_SCOPE)
            item=next(row for row in rows if row['device']=='router-1' and row['check']=='routes')
            if kind=='failed':item['ok']=False
            elif kind=='duplicate':rows.append(dict(item))
            elif kind=='wrong_origin':item.update(origin='device_read',freshness='current')
            elif kind=='bad_time':item['collected_at']='SIMULATION'
            elif kind=='outside_scope':scope['router-1']=['interfaces']
            else:item['output']+='\nIP_FORWARD=1'
            with self.subTest(kind=kind):self.assertNotIn('routes',assess_replay(rows,scope)['nodes'][0]['fields'])


class BusinessPathTests(unittest.TestCase):
    def test_path_uses_logs_for_a_stopped_owner_and_routes_for_a_running_owner(self):
        reads = []
        guidance = {}

        def probe(device, check):
            reads.append((device, check))
            variant = {'local-primary': 'owner_running', 'remote-primary': 'owner_stopped'}.get(device, 'nonowner_stopped')
            return case_probe(variant)(device, check)

        def planner(context):
            for item in context['investigation_plan']['next_checks']:
                if item['check'] in ('logs', 'routes'):
                    guidance[item['device'], item['check']] = item.get('because_refs')
            action, usage = case_planner(context)
            if action['action'] == 'answer':
                action.update(answer='按正常HA规则比较，远端主机有VIP但Croc停止，日志提供了启动线索。'
                              '局端主机的VIP和服务符合局部预期。停止原因和Router转发仍待核实。')
            return action, usage

        with RunningApp(planner, probe, lookup=lambda q: []) as client:
            task = request(client, '请检查局端主备和远端主备的完整业务路径，管理 ping 通但业务不通。')
            self.assertEqual(start(client, task)[0], 409)
            self.assertFalse(reads)
            self.assertEqual(approve(client, task)[0], 200)
            code, done = start(client, task)

        self.assertEqual(code, 200)
        result = done['result']
        self.assertEqual(result['status'], 'answered', result.get('reason'))
        self.assertCountEqual(reads[:8], [(device, check) for device in DEVICES for check in ('services', 'interfaces')])
        self.assertEqual(reads[8:], [('remote-primary', 'logs'), ('local-primary', 'routes')])
        for device, check in reads[8:]:
            self.assertCountEqual(guidance[device, check], [item['id'] for item in result['evidence']
                if item['device'] == device and item['check'] in ('services', 'interfaces')])
        self.assertTrue(any('未读取Router' in gap for gap in result['diagnostic_report']['unknowns']))
        self.assertFalse(result['business_path']['end_to_end_verified'])

    def test_path_context_only_applies_to_relevant_diagnostic_fault(self):
        policy={'mode':'diagnostic','scene':'ha_topology'}
        self.assertTrue(selected('远端能ping通但业务不通',policy))
        self.assertTrue(selected('局端和远端当前Croc异常',policy))
        self.assertFalse(selected('局端备机当前Croc服务和VIP',policy))
        self.assertFalse(selected('解释端到端业务路径',{'mode':'knowledge','scene':'ha_topology'}))

    def test_full_functional_replay_covers_three_segments_and_keeps_terminal_network_unknown(self):
        report=assess_path(path_rows(),REPLAY_SCOPE,synthetic_replay=True)
        self.assertEqual([segment['id'] for segment in report['segments']],['remote','router','local'])
        self.assertTrue(all(segment['nodes'] for segment in report['segments']))
        refs=[ref for segment in report['segments'] for node in segment['nodes'] for field in node['fields'].values() for ref in field['refs']]
        self.assertEqual(set(refs),{'OBS-'+str(i) for i in range(1,13)})
        self.assertFalse(report['end_to_end_verified']);self.assertFalse(report['root_cause_confirmed'])
        self.assertTrue(any('业务终端' in gap for gap in report['gaps']))

    def test_runtime_path_never_accepts_router_candidates_or_synthetic_endpoint_state(self):
        report=assess_path(path_rows(),REPLAY_SCOPE)
        self.assertEqual(report['segments'][1]['nodes'],[])
        self.assertTrue(any('未开放' in gap for gap in report['gaps']))
        self.assertTrue(all(not node['fields'] for segment in report['segments'] for node in segment['nodes']))

    def test_http_scope_and_model_context_preserve_router_gap_without_any_router_read(self):
        contexts=[];reads=[]
        def planner(context):contexts.append(context);return case_planner(context)
        def probe(device,check):reads.append((device,check));return case_probe('owner_running')(device,check)
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            task=request(client,'局端主机和远端主机当前Croc业务不通，请核对完整业务路径。')
            self.assertEqual(set(task['scope']),{'local-primary','remote-primary'})
            approve(client,task);done=start(client,task)[1]
        self.assertEqual(done['status'],'completed',done)
        self.assertTrue(all(device in DEVICES for device,check in reads))
        self.assertTrue(all(context['business_path']['grants_device_access'] is False for context in contexts))
        self.assertTrue(any('未读取Router' in gap for gap in done['result']['diagnostic_report']['unknowns']))
        self.assertFalse(done['result']['business_path']['end_to_end_verified'])


if __name__=='__main__':unittest.main()
