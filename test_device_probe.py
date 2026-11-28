"""Offline checks for the parameter and evidence boundaries."""
import copy
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_memory import AgentMemoryStore
from agent_v2 import AutonomousAgent
from device_registry import DEVICES, GENERAL_CHECK_LABELS, PROBE_KINDS, allows_probe, checks_for
from device_tools import build_command, build_probe_command, probe, probe_device
from device_worker import dispatch
from migration import bridge
import qa


ROUTER = 'router-1'
ARGUMENTS = dict(zip(PROBE_KINDS, ('sshd.service', 'mysql', 'sshd', '3306', 'sh')))


class DeviceProbeTests(unittest.TestCase):
    def test_all_checks_registered_and_dispatchable_on_all_six_devices(self):
        for device in DEVICES:
            for check in GENERAL_CHECK_LABELS:
                with self.subTest(device=device, check=check):
                    request = {'device': device, 'check': check}
                    self.assertIn(check, checks_for(device))
                    self.assertTrue(bridge.valid_request(request))
                    command = build_command(device, check)
                    self.assertEqual(command[:4], ['sudo', '-n', '-H', '/usr/bin/ssh'])
                    self.assertIn('IDENTITY_OK', command[-1])
                    with patch('device_worker.probe', return_value={'ok': True}) as read:
                        self.assertTrue(dispatch(request)['ok'])
                        read.assert_called_once_with(device, check)

    def test_five_kinds_follow_the_same_allowlist_and_preserve_not_found(self):
        for kind, target in ARGUMENTS.items():
            with self.subTest(kind=kind):
                request = {'device': ROUTER, 'kind': kind, 'target': target}
                self.assertTrue(bridge.valid_request(request))
                with patch('device_worker.probe_device', wraps=probe_device) as read, \
                        patch('device_tools.subprocess.run', return_value=Mock(
                            returncode=0, stdout=b'IDENTITY_OK\nNOT_FOUND\n')):
                    result = dispatch(request)
                    read.assert_called_once_with(**request)
                self.assertTrue(result['ok'])
                self.assertEqual(result['return_code'], 0)
                self.assertEqual(result['output'].strip(), 'NOT_FOUND')
                for k, v in request.items():
                    self.assertEqual(result[k], v)

    def test_injection_and_invalid_scopes_stop_before_transport_or_executor(self):
        cases = [dict(device=ROUTER, kind=kind, target=payload)
                 for kind in PROBE_KINDS for payload in ('; id', '$(whoami)', '../etc/passwd', 'sshd\n', 'a' * 65)]
        cases += [dict(device=ROUTER, kind='port', target=value)
                  for value in ('0', '65536', '-1', '３３０６', 3306)]
        cases += [dict(device='198.51.100.46', kind='service', target='sshd'),
                  dict(device=ROUTER, kind='restart', target='sshd')]
        fn = Mock()
        agent = AutonomousAgent(None, probe_device_fn=fn)
        with patch('migration.bridge.call_remote') as remote, patch('device_worker.probe_device') as worker:
            for request in cases:
                with self.subTest(request=request):
                    self.assertFalse(allows_probe(**request))
                    self.assertFalse(bridge.dispatch(request)['ok'])
                    with self.assertRaises(ValueError):
                        dispatch(request)
                    with self.assertRaises(ValueError):
                        build_probe_command(**request)
                    result = agent._inspect_call({'name': 'probe_device', 'arguments': request})
                    self.assertEqual(result['error'], 'invalid_probe_scope')
            remote.assert_not_called()
            worker.assert_not_called()
            fn.assert_not_called()
        for value in ('1', '65535'):
            self.assertTrue(allows_probe(ROUTER, 'port', value))

    def test_same_round_probe_and_inspect_share_first_device_grant_and_run_in_parallel(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        memory = AgentMemoryStore(Path(temp.name) / 'memory.sqlite3')
        calls = [
            {'id': 'ports', 'name': 'inspect_device', 'arguments': {'device': ROUTER, 'check': 'listening_ports'}},
            {'id': 'db', 'name': 'probe_device', 'arguments': {'device': ROUTER, 'kind': 'port', 'target': '3306'}},
        ]
        barrier = threading.Barrier(2, timeout=2)

        def read(*args):
            barrier.wait()
            return {'ok': True, 'output': 'NOT_FOUND\n'}

        def model(config, messages, **kwargs):
            if any(message['role'] == 'tool' for message in messages):
                return {'content': '检查结束。', 'usage': 1}
            return {'content': '', 'usage': 1, 'tool_calls': calls}

        agent = AutonomousAgent(memory, probe_fn=read, probe_device_fn=read, llm_client=model,
                                config_fn=lambda: {'LLM_MODEL_ID': 'test', 'LLM_API_KEY': 'test',
                                                   'LLM_BASE_URL': 'https://example.invalid'},
                                background_learning=False)
        pending = agent.chat('owner', 'thread', '核对 Router1 端口')
        self.assertEqual(pending['status'], 'device_permission_required')
        self.assertEqual([row['id'] for row in pending['requested_devices']], [ROUTER])
        self.assertIn('3306', ' '.join(pending['requested_devices'][0]['checks']))
        confirmed = agent.chat('owner', 'thread', pending_id=pending['pending_id'], grant_devices=[ROUTER])
        self.assertEqual(confirmed['status'], 'answered')
        self.assertEqual([s['ok'] for s in confirmed['agent_steps']], [True, True])
        self.assertEqual([s['tool'] for s in confirmed['agent_steps']], ['inspect_device', 'probe_device'])
        inspect, probe = confirmed['agent_steps']
        self.assertEqual(inspect['manual_commands'][0]['cmd'], 'ss -tulnp')
        self.assertEqual(probe['manual_commands'][0]['cmd'], "ss -tulnp 'sport = :3306'")
        self.assertIn('/usr/bin/ssh', probe['raw_command'])
        self.assertEqual(probe['output'], 'NOT_FOUND\n')
        self.assertEqual(agent.chat('owner', 'other', '再查端口')['status'], 'answered')
        self.assertEqual(memory.granted_devices('other-owner'), [])

    def test_search_excludes_pure_snapshots_and_projects_mixed_sources(self):
        mixed = next(row for row in qa.RECORDS if row['id'] == 'KB-008')
        original = copy.deepcopy(mixed)
        fake = {'id': 'snap', 'title': '设备运行快照', 'kind': 'historical_snapshot',
                'historical_finding': 'OLD_DB_STATE'}
        agent = AutonomousAgent(None)
        with patch('agent_v2.qa.retrieve', return_value=[{'case': fake}, {'case': mixed}]):
            result = agent._run_search_knowledge('Router1 现在安装了哪些软件')
            history = agent._run_search_knowledge('回顾 2026-09-28 当时的状态')
        self.assertEqual([item['id'] for item in result['hits']], ['KB-008'])
        self.assertTrue(result['hits'][0]['runtime_snapshot_excluded'])
        self.assertNotIn('本次资源状态', result['hits'][0]['content'])
        self.assertIn('Croc_sar', result['hits'][0]['content'])
        self.assertIn('不含当前实时状态', result['note'])
        self.assertEqual(history['hits'][0]['id'], 'snap')
        self.assertEqual(mixed, original)


def shell_path():
    shell = shutil.which('sh')
    git = shutil.which('git')
    if not shell and git:
        git_shell = Path(git).resolve().parents[1] / 'usr/bin/sh.exe'
        if git_shell.is_file():
            return str(git_shell)
    return shell


@unittest.skipUnless(shell_path(), 'POSIX shell unavailable')
class ProbeCommandTests(unittest.TestCase):
    def run_on_mock_router(self, fixtures, check=None, kind=None, target=None, cwd=None):
        def executor(command, **kwargs):
            words = shlex.split(command[-1])
            body = shlex.split(words[words.index('root@192.0.2.23') + 1])[2]
            identity = """hostname(){ printf 'example-router-1\\n'; }
                          id(){ printf 'root\\n'; }
                          ip(){ printf '2 eth5 inet 192.0.2.23/24\\n'; }\n"""
            shell = shell_path()
            env = os.environ.copy()
            if os.name == 'nt':
                env['PATH'] = str(Path(shell).parent) + os.pathsep + env['PATH']
            result = subprocess.run([shell, '-c', identity + fixtures + '\n' + body], cwd=cwd, env=env, **kwargs)
            self.assertIn(result.returncode, (0, 42, 48), result.stderr.decode(errors='replace'))
            return result

        if check:
            return probe(ROUTER, check, executor=executor)
        return probe_device(ROUTER, kind, target, executor=executor)

    def test_missing_unit_and_transport_error_are_distinct(self):
        missing = self.run_on_mock_router(
            'systemctl(){ printf "LoadState=not-found\\n"; return 1; }', kind='service', target='mysql')
        failed = self.run_on_mock_router('systemctl(){ return 1; }', kind='service', target='mysql')
        self.assertEqual(missing['output'].strip(), 'NOT_FOUND')
        self.assertTrue(missing['ok'])
        self.assertEqual(failed['error'], 'check_failed')

    def test_process_port_and_binary_apply_scope_and_support_empty_results(self):
        cases = [
            ('process', 'MySQL', 'ps(){ printf "11 mysqld\\n12 OTHER\\n"; }', '11 mysqld'),
            ('process', 'postgres', 'ps(){ printf "11 mysqld\\n"; }', 'NOT_FOUND'),
            ('port', '3306', """ss(){ [ "$2" = 'sport = :3306' ] || return 1; }""", 'NOT_FOUND'),
            ('binary', 'ata-binary-not-installed', '', 'NOT_FOUND'),
        ]
        for kind, target, fixtures, expected in cases:
            with self.subTest(kind=kind, target=target):
                result = self.run_on_mock_router(fixtures, kind=kind, target=target)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['output'].strip(), expected)

    def test_package_uses_installed_names_and_matches_literally(self):
        with tempfile.TemporaryDirectory() as temp:
            dpkg = Path(temp) / 'dpkg-query'
            dpkg.write_bytes(b"#!/bin/sh\nprintf 'mysql-server installed 8.0\\nmysql-old config-files 5.7\\n'\n")
            dpkg.chmod(0o700)
            for target, expected in [('MYSQL', 'mysql-server installed 8.0'), ('mysql.old', 'NOT_FOUND')]:
                result = self.run_on_mock_router('PATH=.:$PATH', kind='package', target=target, cwd=temp)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['output'].strip(), expected)

    def test_summaries_report_failed_units_and_skip_missing_docker(self):
        fixtures = """systemctl(){ printf 'ok.service loaded active running ok\\nfail.service loaded failed failed bad\\n'; }"""
        services = self.run_on_mock_router(fixtures, check='all_services')
        self.assertTrue(services['ok'], services)
        self.assertIn('TOTAL failed=1 running=1 other=0', services['output'])
        overview = self.run_on_mock_router("""uptime(){ printf 'load\\n'; }
                                             free(){ printf 'memory\\n'; }
                                             df(){ printf 'disk\\n'; }
                                             ps(){ printf 'process\\n'; }
                                             command(){ return 1; }""", check='system_overview')
        self.assertTrue(overview['ok'], overview)
        self.assertEqual(overview['output'].split(), ['load', 'memory', 'disk', 'process', 'DOCKER_UNAVAILABLE'])
        ports = self.run_on_mock_router('ss(){ printf "%s\\n" "$1"; }', check='listening_ports')
        self.assertEqual(ports['output'].strip(), '-tulnp')
