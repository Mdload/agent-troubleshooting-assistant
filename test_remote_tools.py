"""远端登记、命令/身份边界、失败和证据范围，不连接实际设备或模型。"""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import device_worker
from device_tools import build_command, probe, tool_catalog
from diagnostic_core import AuditStore, run_diagnosis
from evidence_scope import mark_observation, build_scope
from unified_chat import run


class RemoteToolsTests(unittest.TestCase):
    def test_remote_uses_140_root_direct_path_and_guard(self):
        for device, address, hostname in (('remote-primary', '198.51.100.133', 'example-remote-primary'),
                                           ('remote-backup', '198.51.100.134', 'example-backup')):
            command = build_command(device, 'services')
            self.assertEqual(command[:4], ['sudo', '-n', '-H', '/usr/bin/ssh'])
            self.assertEqual(command[-2], 'root@' + address)
            self.assertNotIn('-J', command)
            self.assertNotIn('root@203.0.113.251', command)
            body = shlex.split(command[-1])[2]
            self.assertIn('hostname', body)
            self.assertIn(hostname, body)
            self.assertIn(address + '/16', body)
            self.assertIn('addr show dev tun7', body)
            self.assertIn('croc2.0smr.service', body)
            self.assertNotIn('diald-ppp', body)
            self.assertNotIn('croc-sar.service', body)

    def test_local_ingress_retained_and_route_failure_not_masked(self):
        command = build_command('local-primary', 'services')
        self.assertEqual(command[-2], 'root@203.0.113.251')
        for device in ('local-primary', 'remote-primary'):
            self.assertNotIn('| head', build_command(device, 'routes')[-1])

    def test_absent_remote_interface_is_explicit_and_connections_are_read(self):
        body = shlex.split(build_command('remote-backup', 'interfaces')[-1])[2]
        self.assertIn('INTERFACE_ABSENT=tun16', body)
        self.assertIn('ppp6', body)
        self.assertIn('ss -ntH', body)
        self.assertNotIn('ss -lntH', body)
        self.assertNotIn('remote-ha-health.sh', body)
        self.assertNotIn('check_croc_links.sh', body)

    def test_worker_rejects_unregistered_commands_and_malformed_values(self):
        requests = [{'device': 'router-1', 'check': 'services'},
                    {'device': 'remote-primary', 'check': 'reboot'},
                    {'device': 'remote-primary; id', 'check': 'services'},
                    {'device': 'remote-primary', 'check': ['services']},
                    {'device': [], 'check': 'services'},
                    {'device': 'remote-primary', 'check': 'services', 'command': 'id'}]
        with patch('device_worker.probe') as executor:
            for request in requests:
                with self.assertRaises(ValueError):
                    device_worker.dispatch(request)
            executor.assert_not_called()

    def test_worker_accepts_only_registered_remote_enum(self):
        with patch('device_worker.probe', return_value={'ok': True}) as executor:
            device_worker.dispatch({'device': 'remote-backup', 'check': 'interfaces'})
            executor.assert_called_once_with('remote-backup', 'interfaces')

    def test_remote_failures_discard_partial_output_and_stderr(self):
        for rc, error in ((41, 'target_hostname_mismatch'), (46, 'target_user_mismatch'),
                          (47, 'target_address_mismatch'), (42, 'check_failed'), (255, 'ssh_failed')):
            executor = Mock(return_value=subprocess.CompletedProcess([], rc,
                b'IDENTITY_OK\nPRIVATE_PARTIAL', b'PRIVATE_STDERR'))
            result = probe('remote-primary', 'services', executor)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], error)
            self.assertEqual(result['return_code'], rc)
            self.assertEqual(result['output'], '')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_remote_timeout_oserror_output_limit_and_missing_identity(self):
        cases = ((subprocess.TimeoutExpired('ssh', 20), 'timeout'),
                 (OSError('private detail'), 'transport_unavailable'),
                 (subprocess.CompletedProcess([], 0, b'IDENTITY_OK\n' + b'x'*16000, b''), 'output_limit'),
                 (subprocess.CompletedProcess([], 0, b'active', b''), 'identity_unconfirmed'))
        for value, error in cases:
            executor = Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
            result = probe('remote-primary', 'services', executor)
            self.assertEqual(result['error'], error)
            self.assertEqual(result['output'], '')
            self.assertNotIn('private detail', json.dumps(result))
            self.assertGreaterEqual(result['duration_ms'], 0)

    def test_remote_success_has_collection_metadata_and_scope(self):
        executor = Mock(return_value=subprocess.CompletedProcess([], 0, b'IDENTITY_OK\nActiveState=inactive\n', b''))
        item = probe('remote-backup', 'services', executor)
        self.assertTrue(item['ok'])
        self.assertEqual(item['clock_source'], 'collector_utc')
        self.assertEqual(item['return_code'], 0)
        item = mark_observation(item, 'remote-backup', 'services')
        item.update(id='OBS-1')
        scope = build_scope([item])
        self.assertEqual(scope['kind'], 'device_read')
        self.assertIn('远端备机', scope['note'])
        self.assertIn('端到端业务可用', scope['unverified'])

    def test_catalog_matches_remote_scope_and_does_not_include_router(self):
        catalog = tool_catalog()
        self.assertEqual(set(catalog), {'local-primary', 'local-backup', 'remote-primary', 'remote-backup'})
        self.assertIn('不检查拨号进程', catalog['remote-primary']['checks']['services'])
        self.assertIn('策略表内容', catalog['remote-primary']['checks']['routes'])

    def test_unified_remote_read_reaches_planner_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditStore(Path(temp) / 'audit.db')
            planner = Mock(side_effect=[({'action': 'read', 'device': 'remote-backup', 'check': 'services'}, 0),
                ({'action': 'answer', 'answer': '只确认服务未运行，当前VIP角色仍需读取。',
                  'refs': ['OBS-1'], 'basis': 'limited'}, 0)])
            tool = Mock(return_value={'ok': True, 'collected_at': 'SIMULATION', 'output': 'inactive'})
            result = run('远端备机状态', [], planner, Mock(return_value=[]), tool, audit)
            self.assertEqual(result['status'], 'answered')
            self.assertEqual(result['evidence_scope']['kind'], 'synthetic')
            self.assertIn('合成状态回放', result['answer'])
            self.assertIn('remote-primary', planner.call_args_list[1][0][0]['tool_catalog'])
            self.assertEqual(len([row for row in audit.read(result['task']) if row[1] == 'observation']), 1)

    def test_legacy_diagnosis_scope_still_rejects_other_registered_node(self):
        with tempfile.TemporaryDirectory() as temp:
            tool = Mock()
            result = run_diagnosis('远端', lambda c: {'action': 'check', 'device': 'remote-backup', 'check': 'services'},
                tool, AuditStore(Path(temp) / 'audit.db'), ['remote-primary'])
            self.assertEqual(result['status'], 'incomplete')
            tool.assert_not_called()


@unittest.skipIf(os.name == 'nt', 'Shell execution guard requires Linux')
class RemoteShellGuardTests(unittest.TestCase):
    def execute(self, hostname='example-remote-primary', user='root', address='198.51.100.133/16', route_code=0):
        # 在临时PATH执行实际生成的sh正文；假程序不访问设备，不使用sudo。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = {'hostname': 'printf "%s\\n" ' + shlex.quote(hostname),
                       'id': 'printf "%s\\n" ' + shlex.quote(user),
                       'ip': ('case "$1" in -o) printf "1: tun7 inet %s scope global\\n" ' +
                              shlex.quote(address) + ';; *) exit ' + str(route_code) + ';; esac')}
            for name, script in scripts.items():
                path = root / name
                path.write_text('#!/bin/sh\n' + script + '\n', encoding='utf-8')
                path.chmod(0o700)
            body = shlex.split(build_command('remote-primary', 'routes')[-1])[2]
            env = dict(os.environ, PATH=str(root) + ':/usr/bin:/bin')
            return subprocess.run(['/bin/sh', '-c', body], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=3, check=False)

    def test_hostname_mismatch_stops_before_read(self):
        self.assertEqual(self.execute(hostname='wrong').returncode, 41)

    def test_user_mismatch_stops_before_read(self):
        self.assertEqual(self.execute(user='ubuntu').returncode, 46)

    def test_management_address_mismatch_stops_before_read(self):
        self.assertEqual(self.execute(address='198.51.100.134/16').returncode, 47)

    def test_command_failure_is_not_masked_by_pipeline(self):
        self.assertEqual(self.execute(route_code=3).returncode, 42)

    def test_matching_identity_allows_bounded_read(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith(b'IDENTITY_OK\n'))


if __name__ == '__main__':
    unittest.main()
