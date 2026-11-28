import http.client
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import app
import devices.device_worker as device_worker
from agent.agent_memory import AgentMemoryStore
from agent.agent_v2 import AutonomousAgent, _verification_status
from server.approval_store import ApprovalStore, TaskError
from deploy import public_http
from devices.device_registry import DEVICES, HOPS, allows_repair
from devices.device_tools import build_repair_command, execute_repair
from legacy.lab_workflow import WorkflowService
from migration import bridge
from agent.repair_proposals import RepairProposals, normalized_steps
from server.user_accounts import AccountStore
from tests.workflow_test_support import RunningApp


RESTART = ('local-primary', 'restart_service', 'croc-sar.service')
FORWARD = ('router-1', 'enable_ip_forward', 'net.ipv4.ip_forward')
ROUTE = ('remote-primary', 'add_route', '172.27.10.0/24 via 203.0.113.1 dev tun16')


class RepairWhitelistTests(unittest.TestCase):
    def test_allowed_actions_for_all_registered_devices(self):
        units = {
            'local': ('keepalived.service', 'croc-sar.service'),
            'remote': ('keepalived.service', 'croc2.0smr.service', 'remote-ha-routes.service'),
            'router': ('keepalived.service', 'route-envset.service'),
        }
        for device in DEVICES:
            for action, verb in (('start_service', 'start'), ('restart_service', 'restart'),
                                 ('reset_failed_service', 'reset-failed')):
                for target in units[device.split('-')[0]]:
                    with self.subTest(device=device, action=action, target=target):
                        command = build_repair_command(device, action, target)[-1]
                        self.assertIn('systemctl '+verb+' '+target, command)
                        self.assertIn(DEVICES[device]['hostname'], command)
                        self.assertIn(DEVICES[device]['address'], command)
                        self.assertIn('id -un', command)
        for device in ('router-1', 'router-2'):
            self.assertIn('sysctl -w net.ipv4.ip_forward=1', build_repair_command(device, *FORWARD[1:])[-1])

    def test_invalid_scopes_rejected_at_server_bridge_and_worker(self):
        invalid = [
            ('router-1', 'restart_service', 'sshd.service'),
            ('router-1', 'restart_service', 'NetworkManager.service'),
            ('local-primary', 'start_service', 'systemd-networkd.service'),
            ('local-primary', 'restart_service', 'croc2.0smr.service'),
            ('remote-primary', 'restart_service', 'croc-sar.service'),
            ('router-2', 'start_service', 'remote-ha-routes.service'),
            ('remote-backup', 'reset_failed_service', 'route-envset.service'),
            ('router-1', 'restart_service', 'keepalived.service; id'),
            ('router-1', 'restart_service', 'keepalived.service\nwhoami'),
            ('router-1', 'restart_service', '$(id)'),
            ('router-1', 'start_service', '--all'),
            ('router-1', 'stop_service', 'dbus.service'),
            ('router-1', 'enable_ip_forward', 'net.ipv4.ip_forward=0'),
            ('router-1', 'disable_ip_forward', 'net.ipv4.ip_forward'),
            ('local-primary', 'enable_ip_forward', 'net.ipv4.ip_forward'),
            ('unknown', 'restart_service', 'keepalived.service'),
            ('router-1', ['start_service'], 'keepalived.service'),
            ('router-1', 'start_service', None),
        ]
        with patch.object(bridge, 'call_remote') as remote, patch.object(device_worker, 'execute_repair') as repair:
            for device, action, target in invalid:
                with self.subTest(device=device, action=action, target=target):
                    self.assertFalse(allows_repair(device, action, target))
                    request = dict(operation='approved_repair', device=device, action=action, target=target)
                    with self.assertRaises(ValueError):
                        build_repair_command(device, action, target)
                    self.assertEqual(bridge.dispatch(request)['error'], 'bridge_invalid_scope')
                    with self.assertRaises(ValueError):
                        device_worker.dispatch(request)
            remote.assert_not_called()
            repair.assert_not_called()

    def test_plain_shell_or_extra_payload_is_rejected(self):
        for request in ({'command': 'systemctl restart sshd.service'},
                        dict(operation='approved_repair', device=RESTART[0], action=RESTART[1],
                             target=RESTART[2], command='systemctl restart sshd.service')):
            self.assertFalse(bridge.valid_request(request))
            with self.assertRaises(ValueError):
                device_worker.dispatch(request)

    def test_route_policy_validates_networks_hops_and_interfaces_on_both_ends(self):
        invalid = [
            ('remote-primary', 'default via 203.0.113.1 dev tun16'),
            ('remote-primary', '0.0.0.0/0 via 203.0.113.1 dev tun16'),
            ('remote-primary', '198.51.100.133/32 via 203.0.113.1 dev tun16'),
            ('remote-primary', '10.0.0.0/8 via 203.0.113.1 dev tun16'),
            ('router-1', '192.0.2.0/24 via 172.27.10.1 dev eth1'),
            ('router-1', '10.250.251.0/24 via 172.27.10.1 dev eth1'),
            ('router-1', '192.168.50.0/24 via 172.27.10.1 dev eth5'),
            ('remote-primary', '172.27.10.0/24 via 203.0.113.1 dev tun7'),
            ('local-primary', '192.168.50.0/24 via 172.27.10.1 dev eth2'),
            ('local-primary', '192.168.50.0/24 via 172.27.10.1 dev eth99'),
            ('remote-primary', '172.27.10.0/24 via 198.51.100.134 dev tun16'),
            ('remote-primary', '172.27.10.0/24 via 999.0.0.1 dev tun16'),
            ('remote-primary', '172.27.10.1/24 via 203.0.113.1 dev tun16'),
            ('remote-primary', '172.27.10.0/24 via 203.0.113.1 dev tun16;id'),
            ('remote-primary', '172.27.10.0/24 via $(id) dev tun16'),
            ('remote-primary', '172.27.10.0/24 via 203.0.113.1 dev tun16 table 9'),
        ]
        with patch.object(device_worker, 'execute_repair', return_value={'ok': True}) as repair:
            for device, target in invalid:
                with self.subTest(target=target):
                    request = dict(operation='approved_repair', device=device, action='add_route', target=target)
                    self.assertFalse(bridge.valid_request(request))
                    with self.assertRaises(ValueError):
                        device_worker.dispatch(request)
            repair.assert_not_called()
            request = dict(zip(('device', 'action', 'target'), ROUTE), operation='approved_repair')
            self.assertTrue(bridge.valid_request(request))
            self.assertTrue(device_worker.dispatch(request)['ok'])
            repair.assert_called_once_with(*ROUTE)
        self.assertIn('ip -4 route add ' + ROUTE[2] + ' proto static', build_repair_command(*ROUTE)[-1])

    def test_route_and_manual_requests_pass_fixed_jump_protocol(self):
        route = dict(zip(('device', 'action', 'target'), ROUTE), operation='approved_repair')
        manual = dict(operation='manual_command', device='local-primary', action='run_command',
                      target='systemctl reload croc-sar.service\nip link set dev eth3 up')
        for request in (route, manual):
            with self.subTest(operation=request['operation']):
                self.assert_protocol_request(request)

    def assert_protocol_request(self, request):
        jump = HOPS['jump_host']
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()+b'\n'))
        stdout = SimpleNamespace(buffer=io.BytesIO())
        pwd = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name=jump['user']))
        with patch.dict(sys.modules, {'pwd': pwd}), patch('sys.stdin', stdin), patch('sys.stdout', stdout), \
                patch('socket.gethostname', return_value=jump['hostname']), \
                patch('os.getuid', return_value=jump['uid'], create=True), \
                patch('socket.AF_UNIX', 1, create=True), patch('socket.socket') as connect:
            connection = connect.return_value.__enter__.return_value
            connection.makefile.return_value.__enter__.return_value.readline.return_value = b'{"ok": true}\n'
            exec(bridge.REMOTE_CODE, {})
            self.assertEqual(json.loads(connection.sendall.call_args[0][0]), request)
        self.assertEqual(json.loads(stdout.buffer.getvalue()), {'ok': True})

    def test_wire_rejects_injections_before_opening_worker_socket(self):
        jump = HOPS['jump_host']
        for action, target in (('restart_service', 'keepalived.service; id'),
                               ('enable_ip_forward', 'net.ipv4.ip_forward=0')):
            request = dict(operation='approved_repair', device='router-1', action=action, target=target)
            frame = io.BytesIO(json.dumps(request).encode()+b'\n')
            pwd = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name=jump['user']))
            with patch.dict(sys.modules, {'pwd': pwd}), patch('sys.stdin', SimpleNamespace(buffer=frame)), \
                    patch('socket.gethostname', return_value=jump['hostname']), \
                    patch('os.getuid', return_value=jump['uid'], create=True), patch('socket.socket') as connection:
                with self.assertRaises(AssertionError):
                    exec(bridge.REMOTE_CODE, {})
                connection.assert_not_called()

    def test_worker_executes_a_fixed_command_and_requires_identity(self):
        request = dict(zip(('device', 'action', 'target'), RESTART), operation='approved_repair')
        with patch.object(device_worker, 'execute_repair', return_value={'ok': True}) as repair:
            self.assertTrue(device_worker.dispatch(request)['ok'])
            repair.assert_called_once_with(*RESTART)
        for exit_code, error in ((41, 'target_hostname_mismatch'), (46, 'target_user_mismatch'),
                                 (47, 'target_address_mismatch'), (0, 'identity_unconfirmed')):
            fake_run = Mock(return_value=SimpleNamespace(returncode=exit_code, stdout=b''))
            self.assertEqual(execute_repair(*RESTART, executor=fake_run)['error'], error)


class TieredExecutionTests(unittest.TestCase):
    risky_commands = (
        ('router-1', 'rm -rf /', 'disk'),
        ('router-1', '/sbin/mkfs.ext4 /dev/sda', 'disk'),
        ('router-1', 'dd if=/dev/zero of=/dev/sda', 'disk'),
        ('router-1', 'shred /dev/sda', 'disk'),
        ('router-1', 'echo 0 > /dev/sda', 'disk'),
        ('router-1', 'reboot', 'power'),
        ('router-1', 'shutdown -r now', 'power'),
        ('router-1', 'poweroff', 'power'),
        ('router-1', 'halt', 'power'),
        ('router-1', 'init 0', 'power'),
        ('router-1', 'init 6', 'power'),
        ('router-1', 'iptables -F', 'firewall'),
        ('router-1', 'iptables -t nat -X', 'firewall'),
        ('router-1', 'nft flush ruleset', 'firewall'),
        ('router-1', 'systemctl stop sshd', 'management'),
        ('router-1', 'echo x >> /root/.ssh/authorized_keys', 'management'),
        ('router-1', 'passwd root', 'management'),
        ('router-1', 'chpasswd', 'management'),
        ('router-1', 'userdel ubuntu', 'management'),
        ('router-1', 'systemctl restart NetworkManager', 'management'),
        ('router-1', 'systemctl stop systemd-networkd', 'management'),
        ('router-1', 'ip link set eth5 down', 'management'),
        ('local-primary', 'ip addr flush dev eth2', 'management'),
        ('remote-primary', 'ip link set dev tun7 down', 'management'),
        ('router-1', 'ip route del 192.0.2.0/24', 'management'),
        ('router-1', 'ip route flush 10.250.251.0/24', 'management'),
        ('router-1', 'ip route del 198.51.100.0/24', 'management'),
        ('router-1', 'ip route flush table 251', 'management'),
        ('router-1', 'ip link set dev eth1 up\nreboot', 'power'),
    )

    def test_high_risk_commands_blocked_before_card_and_at_both_dispatchers(self):
        agent = AutonomousAgent(None)
        with patch.object(bridge, 'call_remote') as transport, \
                patch.object(device_worker, 'execute_repair') as execute:
            for device, command, category in self.risky_commands:
                with self.subTest(command=command):
                    args = dict(device=device, action='run_command', target=command, commands=[command])
                    card = agent._propose('owner', 'chat', args, [], 'auto', True, None)
                    self.assertEqual(card['block_reason'], 'high_risk_' + category)
                    self.assertFalse(card['approval_button_rendered'])
                    self.assertNotIn('proposal_id', card)
                    self.assertEqual(card['commands'], [])
                    request = dict(operation='manual_command', device=device, action='run_command', target=command)
                    self.assertEqual(bridge.dispatch(request)['error'], 'bridge_invalid_scope')
                    with self.assertRaises(ValueError):
                        device_worker.dispatch(request)
            transport.assert_not_called()
            execute.assert_not_called()

    def test_command_grammar_denies_shell_and_out_of_scope_operations(self):
        for target in ('bash -c id', 'systemctl restart croc-sar.service; id',
                       'systemctl restart $(echo croc-sar.service)', 'systemctl start --all',
                       'systemctl restart /tmp/other.service', 'systemctl stop systemd-resolved',
                       'ip route del 0.0.0.0/0 via 172.27.10.1 dev eth3 proto static',
                       'ip link set dev eth2 up', 'ip link set dev eth3 down'):
            with self.subTest(target=target):
                self.assertFalse(allows_repair('local-primary', 'run_command', target))

    def test_manual_transport_accepts_multiple_fixed_commands_after_identity(self):
        target = 'systemctl reload croc-sar.service\nip link set dev eth3 up'
        args = ('local-primary', 'run_command', target)
        request = dict(zip(('device', 'action', 'target'), args), operation='manual_command')
        with patch.object(device_worker, 'execute_repair', return_value={'ok': True}) as execute:
            self.assertTrue(bridge.valid_request(request))
            self.assertTrue(device_worker.dispatch(request)['ok'])
            execute.assert_called_once_with(*args)
            request['operation'] = 'approved_repair'
            self.assertFalse(bridge.valid_request(request))
            with self.assertRaises(ValueError):
                device_worker.dispatch(request)
        command = build_repair_command(*args)[-1]
        for part in ('IDENTITY_OK', 'id -un', 'timeout -k 2s 20s', 'systemctl reload croc-sar.service',
                     'timeout -k 2s 20s ip link set dev eth3 up'):
            self.assertIn(part, command)
        executor = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'IDENTITY_OK\n'+b'x'*16000))
        result = execute_repair(*args, executor=executor)
        self.assertTrue(result['ok'])
        self.assertTrue(result['output'].endswith('\nTRUNCATED\n'))
        self.assertEqual(executor.call_args.kwargs['timeout'], 98)

    def test_router_manual_commands_enter_pending_approval(self):
        commands = ('systemctl restart mysql', 'systemctl restart mysqld',
                    'systemctl start mysqld', 'pkill -f "http.server 19999"',
                    'iptables -t nat -L -n')
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairProposals(Path(tmp)/'proposals.sqlite3')
            repair = Mock()
            agent = AutonomousAgent(None, proposal_store=store, repair_fn=repair)
            for command in commands:
                for mode in ('approval', 'auto'):
                    with self.subTest(command=command, mode=mode):
                        args = dict(device='router-1', action='run_command', target=command)
                        card = agent._propose('owner', 'chat', dict(args, rollback='Review service or port state'), [], mode, True, None)
                        self.assertEqual(card['status'], 'pending_approval')
                        self.assertEqual(card['execution_mode'], 'approval')
                        self.assertTrue(card['approval_button_rendered'])
                        with store.connect() as db:
                            row = db.execute('SELECT status FROM repair_proposals WHERE id=?',
                                             (card['proposal_id'],)).fetchone()
                        self.assertEqual(row['status'], 'pending_approval')
                        self.assertTrue(bridge.valid_request(dict(args, operation='manual_command')))
            repair.assert_not_called()

    def test_structured_database_proposals_persist_and_require_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairProposals(Path(tmp) / 'proposals.sqlite3')
            repair = Mock(return_value={'ok': True})
            probe_service = Mock(return_value={'ok': True, 'output':
                'Id=mysql.service\nLoadState=loaded\nActiveState=active\nSubState=running\n'})
            agent = AutonomousAgent(None, proposal_store=store, repair_fn=repair)
            for action, target in (('start_service', 'mysql'), ('restart_service', 'mysql.service')):
                for mode in ('approval', 'auto'):
                    with self.subTest(action=action, mode=mode):
                        args = dict(device='router-1', action=action, target=target)
                        card = agent._propose('owner', 'chat', args, [], mode, True, None)
                        self.assertRegex(card['proposal_id'], r'^[a-f0-9]{32}$')
                        self.assertTrue(card['approval_button_rendered'])
                        with store.connect() as db:
                            row = db.execute('SELECT status, execution_mode, target FROM repair_proposals WHERE id=?',
                                             (card['proposal_id'],)).fetchone()
                        self.assertEqual(tuple(row), ('pending_approval', 'approval', 'mysql.service'))
            repair.assert_not_called()
            result = store.execute(card['proposal_id'], 'owner', repair, Mock(), probe_target=probe_service)
            repair.assert_called_once_with('router-1', 'restart_service', 'mysql.service')
            probe_service.assert_called_once_with('router-1', 'service', 'mysql.service')
            self.assertTrue(result['review']['ok'])

    def test_structured_management_services_are_blocked_at_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairProposals(Path(tmp) / 'proposals.sqlite3')
            agent = AutonomousAgent(None, proposal_store=store, repair_fn=Mock())
            for unit in ('sshd', 'ssh', 'NetworkManager', 'systemd-networkd', 'systemd-resolved', 'dbus'):
                for action in ('start_service', 'restart_service', 'reset_failed_service', 'stop_service'):
                    with self.subTest(unit=unit, action=action):
                        args = dict(device='router-1', action=action, target=unit)
                        card = agent._propose('owner', 'chat', args, [], 'approval', True, None)
                        self.assertEqual(card['block_reason'], 'action_not_allowed')
                        self.assertFalse(card['approval_button_rendered'])
                        request = dict(args, target=unit+'.service', operation='approved_repair')
                        self.assertFalse(bridge.valid_request(request))
                        with self.assertRaises(ValueError):
                            device_worker.dispatch(request)
            self.assertEqual(store.for_conversation('owner', 'chat'), [])

    def test_auto_deletion_and_stop_require_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairProposals(Path(tmp) / 'proposals.sqlite3')
            repair = Mock()
            agent = AutonomousAgent(None, proposal_store=store, repair_fn=repair)
            for action, target in (('del_route', '192.168.50.0/24 via 192.168.20.1 dev eth0'),
                                   ('stop_service', 'keepalived')):
                with self.subTest(action=action):
                    args = dict(device='router-1', action=action, target=target)
                    card = agent._propose('owner', 'chat', args, [], 'auto', True, None)
                    self.assertEqual(card['status'], 'pending_approval')
                    self.assertEqual(card['execution_mode'], 'approval')
            repair.assert_not_called()

    def test_auto_plan_with_manual_command_waits_for_approval_and_can_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairProposals(Path(tmp)/'proposals.sqlite3')
            repair = Mock(return_value={'ok': True})
            device, command = 'local-primary', 'systemctl stop croc-sar'
            outputs = {'services': 'Id=croc-sar.service\nActiveState=inactive\n', 'interfaces': 'eth3 UP <UP,LOWER_UP>\n'}
            probe = Mock(side_effect=lambda device, check: {'ok': True, 'output': outputs[check]})
            agent = AutonomousAgent(None, proposal_store=store, repair_fn=repair, probe_fn=probe)
            steps = [dict(device=device, action='bring_up_interface', target='eth3'),
                     dict(device=device, action='run_command', target=command)]
            card = agent._propose('owner', 'chat', {'steps': steps, 'rollback': '启动 Croc'}, [],
                                  'auto', True, None, 'repair_verification_budget')
            self.assertEqual(card['status'], 'pending_approval')
            self.assertEqual(card['execution_mode'], 'approval')
            self.assertTrue(card['approval_button_rendered'])
            self.assertEqual(card['steps'][1]['command'], command)
            ident = card['proposal_id']
            self.assertEqual(store.execute(ident, 'owner', repair, probe, mode='auto')['status'],
                             'pending_approval')
            repair.assert_not_called()
            result = store.execute(ident, 'owner', repair, probe)
            self.assertEqual(result['status'], 'executed')
            self.assertTrue(result['review']['ok'])
            repair.assert_has_calls([call(device, 'bring_up_interface', 'eth3'),
                                     call(device, 'run_command', command)])
            receipt = store.claim_verification(ident, 'owner', 'chat')
            self.assertTrue(_verification_status(receipt, [])[0])
            observations = [dict(device=device, check=check, output=output, tool='inspect_device', ok=True)
                            for check, output in outputs.items()]
            self.assertEqual(_verification_status(receipt, observations), ([], []))
            with self.assertRaises(TaskError):
                store.claim_verification(ident, 'owner', 'chat')

    def test_auto_actions_are_limited_to_business_interface_up_and_static_delete(self):
        self.assertIn('ip link set dev eth1 up', build_repair_command('router-1', 'bring_up_interface', 'eth1')[-1])
        self.assertFalse(allows_repair('router-1', 'bring_up_interface', 'eth5'))
        target = '172.27.10.0/24 via 203.0.113.1 dev tun16'
        self.assertIn('ip -4 route del '+target+' proto static',
                      build_repair_command('remote-primary', 'del_route', target)[-1])
        self.assertFalse(allows_repair('remote-primary', 'del_route', '198.51.100.0/24 via 203.0.113.1 dev tun16'))


class RepairApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RepairProposals(self.root/'proposals.sqlite3')

    def test_model_only_creates_account_owned_cards(self):
        turns = [
            {'id': 'proposal', 'name': 'propose_config_change',
             'arguments': dict(zip(('device', 'action', 'target'), RESTART),
                               reason='现场故障', commands=['systemctl restart sshd.service'])},
            {'id': 'manual', 'name': 'propose_config_change',
             'arguments': {'device': 'router-1', 'action': 'restart_service', 'target': 'sshd.service'}},
        ]
        for signed_in in (True, False):
            llm = Mock(side_effect=[{'content': '', 'tool_calls': turns},
                                    {'content': '修复建议已生成。', 'tool_calls': []}])
            probe = Mock()
            agent = AutonomousAgent(AgentMemoryStore(self.root/'agent.sqlite3'), proposal_store=self.store,
                                    probe_fn=probe, llm_client=llm, background_learning=False,
                                    config_fn=lambda: {'LLM_BASE_URL': 'https://api.inferera.com/v1',
                                                       'LLM_MODEL_ID': 'fake', 'LLM_API_KEY': 'offline'})
            result = agent.chat('owner', 'chat', '给出建议', allow_repair_proposals=signed_in)
            allowed, manual = result['proposed_changes']
            self.assertFalse(allowed['executed'])
            self.assertNotIn('proposal_id', manual)
            self.assertEqual(manual['status'], 'manual')
            if signed_in:
                self.assertEqual(allowed['status'], 'pending_approval')
                self.assertEqual(allowed['commands'], ['systemctl restart croc-sar.service'])
                self.assertRegex(allowed['proposal_id'], r'^[a-f0-9]{32}$')
                self.assertTrue(allowed['approval_button_rendered'])
                self.assertIn(allowed['proposal_id'], str(llm.call_args_list[-1]))
            else:
                self.assertNotIn('proposal_id', allowed)
                self.assertFalse(allowed['approval_button_rendered'])
            self.assertFalse(manual['approval_button_rendered'])
            self.assertEqual(manual['block_reason'], 'action_not_allowed')
            probe.assert_not_called()

    def test_atomic_claim_prevents_replay_and_overlapping_same_device_write(self):
        ident = self.store.create('owner', 'chat', *RESTART)['proposal_id']
        other = self.store.create('owner', 'chat', *RESTART)['proposal_id']
        acquired, finish = threading.Event(), threading.Event()

        def simulate(*args):
            acquired.set()
            return {'ok': finish.wait(3)}

        repair = Mock(side_effect=simulate)
        probe = Mock(return_value={'ok': True, 'output': 'Id=croc-sar.service\nActiveState=active\n'})
        results = []
        execution = threading.Thread(target=lambda: results.append(self.store.execute(ident, 'owner', repair, probe)))
        execution.start()
        try:
            self.assertTrue(acquired.wait(3))
            self.assertEqual(self.store.execute(ident, 'owner', repair, probe)['status'], 'executing')
            with self.assertRaisesRegex(TaskError, 'proposal_device_busy'):
                self.store.execute(other, 'owner', repair, probe)
        finally:
            finish.set()
            execution.join(4)
        self.assertEqual(results[0]['status'], 'executed')
        self.assertEqual(self.store.execute(ident, 'owner', repair, probe), results[0])
        repair.assert_called_once_with(*RESTART)
        probe.assert_called_once_with(RESTART[0], 'services')
        self.assertIn('运行：active', results[0]['review']['summary'])
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM repair_proposals WHERE id=?', (ident,)).fetchone()
            self.assertLessEqual(row['created_at'], row['approved_at'])
            self.assertLessEqual(row['approved_at'], row['finished_at'])
            self.assertEqual(row['owner'], 'owner')

    def test_failed_execution_stays_consumed_and_rechecks(self):
        ident = self.store.create('owner', 'chat', *RESTART)['proposal_id']
        repair = Mock(side_effect=TimeoutError())
        probe = Mock(return_value={'ok': False})
        result = self.store.execute(ident, 'owner', repair, probe)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.execute(ident, 'owner', repair, probe), result)
        probe.assert_called_once_with(RESTART[0], 'services')
        repair.assert_called_once()

    def test_normalizes_labels_suffix_lists_and_command_only_plans(self):
        targets = ['croc-sar.service', 'keepalived.service']
        args = {'device': '局端主机', 'action': 'start_service', 'target': 'croc-sar, keepalived.service'}
        expected = [{'device': 'local-primary', 'action': 'start_service', 'target': unit} for unit in targets]
        self.assertEqual(normalized_steps(args), expected)
        self.assertEqual(normalized_steps({'device': 'local_primary',
            'commands': ['systemctl start croc-sar', 'systemctl start keepalived.service']}), expected)
        mixed = normalized_steps({'steps': [
            {'device': 'Router 1', 'commands': ['sysctl -w net.ipv4.ip_forward=1']},
            {'device': '远端主机', 'commands': ['ip route add '+ROUTE[2]]},
        ]})
        self.assertEqual([tuple(step.values()) for step in mixed], [FORWARD, ROUTE])
        for commands in (
            ['systemctl start croc-sar', 'systemctl start sshd.service'],
            ['systemctl start croc-sar; systemctl start keepalived'],
            ['bash -c "systemctl start croc-sar"'],
        ):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                normalized_steps({'device': '局端主机', 'commands': commands})

    def test_executes_steps_in_order_then_reviews_each_check(self):
        args = {'device': '局端主机', 'steps': [
            {'action': 'start_service', 'target': 'croc-sar,keepalived'},
            {'device': '远端主机', 'commands': ['ip route add '+ROUTE[2]]},
        ]}
        card = self.store.create('owner', 'chat', steps=normalized_steps(args))
        self.assertEqual(len(card['steps']), 3)
        transport = Mock()
        transport.repair.return_value = {'ok': True}
        transport.probe.side_effect = [
            {'ok': True, 'output': 'Id=croc-sar.service\nActiveState=active\n\nId=keepalived.service\nActiveState=active\n'},
            {'ok': True, 'output': ROUTE[2]+' proto static\n'},
        ]
        result = self.store.execute(card['proposal_id'], 'owner', transport.repair, transport.probe)
        self.assertEqual(transport.mock_calls, [
            call.repair('local-primary', 'start_service', 'croc-sar.service'),
            call.repair('local-primary', 'start_service', 'keepalived.service'),
            call.repair(*ROUTE), call.probe('local-primary', 'services'), call.probe('remote-primary', 'routes'),
        ])
        self.assertEqual([step['status'] for step in result['step_results']], ['executed']*3)
        self.assertTrue(result['review']['ok'])
        self.assertIn('已读到目标路由', result['step_results'][-1]['review']['summary'])

    def test_partial_failure_skips_follow_up_writes_and_reviews_involved_devices(self):
        steps = [dict(zip(('device', 'action', 'target'), item)) for item in (FORWARD, RESTART, ROUTE)]
        ident = self.store.create('owner', 'chat', steps=steps)['proposal_id']
        repair = Mock(side_effect=[{'ok': True}, {'ok': False, 'error': 'repair_command_failed'}])
        probe = Mock(return_value={'ok': False})
        result = self.store.execute(ident, 'owner', repair, probe)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(repair.call_args_list, [call(*FORWARD), call(*RESTART)])
        self.assertEqual(probe.call_count, 3)
        self.assertEqual([step['status'] for step in result['step_results']], ['executed', 'failed', 'skipped'])

    def test_chat_modes_rejection_audit_and_reload_with_server_saved_messages(self):
        config = lambda: {'LLM_BASE_URL': 'https://api.inferera.com/v1', 'LLM_MODEL_ID': 'fake', 'LLM_API_KEY': 'offline'}
        tool = {'id': 'a', 'name': 'propose_config_change',
                'arguments': {'device': '局端主机', 'commands': ['systemctl start croc-sar', 'systemctl start keepalived']}}
        probe = Mock(return_value={'ok': True,
            'output': 'Id=croc-sar.service\nActiveState=active\n\nId=keepalived.service\nActiveState=active\n'})
        repair = Mock(return_value={'ok': True})
        with RunningApp(probe=probe) as client, patch('server.user_accounts.ITERATIONS', 1000):
            engine = Mock()
            client.server.repair_proposals = self.store
            client.server.approved_repair = repair
            client.server.agent_v2 = AutonomousAgent(
                AgentMemoryStore(self.root/'agent.sqlite3'), proposal_store=self.store, probe_fn=probe,
                repair_fn=repair, proposal_audit=client.service.audit, llm_client=engine,
                background_learning=False, config_fn=config)
            payload = {'conversation_id': 'mode-tests', 'allow_context': True, 'question': '恢复服务'}
            self.assertEqual(client.call('/api/chat', dict(payload, execution_mode='auto'))[0], 401)
            code, account = client.call('/api/account/register',
                {'username': 'approval', 'password': 'test-only', 'allow_storage': True})
            self.assertEqual(code, 200)

            def chat(mode, proposal=tool, **kwargs):
                engine.side_effect = [{'content': '', 'tool_calls': [proposal]},
                                      {'content': '建议已生成。', 'tool_calls': []}]
                if mode == 'auto' and proposal == tool:
                    engine.side_effect = [
                        {'content': '', 'tool_calls': [proposal]},
                        {'content': '', 'tool_calls': [
                            {'id': str(index), 'name': 'inspect_device', 'arguments': dict(device=device, check=check)}
                            for index, (device, check) in enumerate([
                                ('local-primary', 'services'), ('local-primary', 'interfaces'),
                                ('local-backup', 'interfaces'), ('local-primary', 'ping_tunnel_peer')])]},
                        {'content': '检查已完成。', 'tool_calls': []},
                    ]
                data = dict(payload, **kwargs)
                if mode:
                    data['execution_mode'] = mode
                code, response = client.call('/api/chat', data)
                self.assertEqual(code, 200, response)
                step = next(step for step in response['agent_steps'] if step['tool'] == 'propose_config_change')
                output = json.loads(step['output'])
                self.assertIn('block_reason', output)
                self.assertIn('proposal_id', output)
                return response['proposed_changes'][0]

            pending = chat(None)
            self.assertEqual(pending['execution_mode'], 'approval')
            self.assertTrue(pending['approval_button_rendered'])
            ident = pending['proposal_id']
            repair.assert_not_called()
            history_path = '/api/history/'+payload['conversation_id']
            code, history = client.call(history_path)
            self.assertEqual(code, 200)
            self.assertEqual(history['messages'][-1]['proposed_changes'][0]['status'], 'pending_approval')
            foreign = {'id': 'different', 'title': 'Other', 'messages': [
                {'role': 'assistant', 'content': 'wrong chat', 'proposed_changes': [ident]}], 'expected_revision': 0}
            self.assertEqual(client.call('/api/history', foreign)[0], 400)
            self.assertEqual(client.call('/api/proposals/'+ident+'/reject', {})[1]['status'], 'rejected')
            self.assertEqual(client.call('/api/proposals/'+ident+'/execute', {})[1]['status'], 'rejected')
            repair.assert_not_called()
            code, history = client.call(history_path)
            self.assertEqual(code, 200)
            self.assertEqual(history['messages'][-1]['proposed_changes'][0]['status'], 'rejected')
            # A client that saves only text must not erase the server's stored card reference.
            saved = {'id': payload['conversation_id'], 'title': 'Test', 'expected_revision': history['revision'],
                     'messages': [{'role': 'assistant', 'content': history['messages'][-1]['content']}]}
            self.assertEqual(client.call('/api/history', saved)[0], 200)
            _, history = client.call(history_path)
            self.assertEqual(history['messages'][-1]['proposed_changes'][0]['proposal_id'], ident)
            client.server.agent_v2.memory.grant_devices(AccountStore.owner(account['account']),
                                                       ['local-primary', 'local-backup'])
            probe.side_effect = lambda device, check: {'ok': True, 'output': {
                'services': ('Id=croc-sar.service\nActiveState=active\nSubState=running\n\n'
                             'Id=keepalived.service\nActiveState=active\nSubState=running\n'),
                'interfaces': ('eth2 UP 192.0.2.15/24\neth3 UP 172.27.10.37/24' if device == 'local-primary'
                               else 'eth2 UP 192.0.2.14/24\neth3 UP'),
                'ping_tunnel_peer': 'ICMP_TX=3\nICMP_RX=3\n',
            }[check]}
            engine.reset_mock()
            auto = chat('auto')
            self.assertIn('rejected', engine.call_args_list[0][0][1][0]['content'])
            self.assertEqual(auto['status'], 'executed')
            self.assertFalse(auto['approval_button_rendered'])
            self.assertEqual(repair.call_count, 2)
            self.assertTrue(auto['review']['ok'])
            _, history = client.call(history_path)
            self.assertEqual(history['messages'][-1]['proposed_changes'][0]['step_results'], auto['step_results'])
            invalid = dict(tool, arguments={'device': '局端主机', 'commands': [
                'systemctl start croc-sar', 'systemctl start sshd']})
            blocked = chat('auto', invalid)
            self.assertEqual(blocked['block_reason'], 'action_not_allowed')
            self.assertEqual(repair.call_count, 2)

    def test_interrupted_write_is_not_replayed(self):
        ident = self.store.create('owner', 'chat', *RESTART)['proposal_id']
        with self.store.connect() as db:
            db.execute("UPDATE repair_proposals SET status='executing' WHERE id=?", (ident,))
        self.store.recover_interrupted()
        repair, probe = Mock(), Mock()
        result = self.store.execute(ident, 'owner', repair, probe)
        self.assertEqual(result['execution']['error'], 'execution_interrupted')
        repair.assert_not_called()
        probe.assert_not_called()

    def test_execution_revalidates_scope_and_the_exact_saved_command(self):
        ident = self.store.create('owner', 'chat', *RESTART)['proposal_id']
        repair, probe = Mock(), Mock()
        with self.store.connect() as db:
            db.execute('UPDATE repair_proposals SET command=? WHERE id=?', ('systemctl restart sshd.service', ident))
        with self.assertRaisesRegex(TaskError, 'proposal_scope_changed'):
            self.store.execute(ident, 'owner', repair, probe)
        repair.assert_not_called()

    def test_http_owner_csrf_forwarding_alias_and_recheck(self):
        probe = Mock(return_value={'ok': True, 'output': 'default dev eth1\nIP_FORWARD=1\n'})
        workflow = WorkflowService(ApprovalStore(self.root/'workflow.sqlite3'), probe=probe)
        server = app.Server(('127.0.0.1', 0), workflow=workflow)
        server.approved_repair = Mock(return_value={'ok': True, 'return_code': 0})
        server.repair_proposals = self.store
        account = {'id': 'owner', 'name': 'owner'}
        token, other_token = 'a'*43, 'b'*43
        accounts = {token: account, other_token: {'id': 'other', 'name': 'other'}}
        server.accounts = Mock(resolve=accounts.get)
        gateway = public_http.Server(('127.0.0.1', 0), '127.0.0.1:18890',
                                    server.server_address, require_proxy=False)
        for httpd in (server, gateway):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
        owner = AccountStore.owner(account)
        proposal = self.store.create(owner, 'chat', *FORWARD)['proposal_id']
        local_path = '/api/proposals/'+proposal+'/execute'
        public_path = '/api/repairs/'+proposal+'/execute'

        def post(httpd, path, token=None, data=None, csrf=app.CSRF_TOKEN):
            host = gateway.authority if httpd is gateway else '127.0.0.1:'+str(server.server_port)
            headers = {'Host': host, 'Origin': 'http://'+host, 'Content-Type': 'application/json', 'X-ATA-Token': csrf}
            if token:
                headers['Cookie'] = 'ata_account='+token
            conn = http.client.HTTPConnection(*httpd.server_address, timeout=4)
            try:
                conn.request('POST', path, json.dumps(data or {}), headers)
                response = conn.getresponse()
                return response.status, json.load(response)
            finally:
                conn.close()

        self.assertEqual(post(server, local_path)[0], 401)
        self.assertEqual(post(server, local_path, other_token)[0], 404)
        self.assertEqual(post(server, local_path, token, csrf='incorrect')[0], 403)
        self.assertEqual(post(server, local_path, token, {'target': 'sshd.service'})[0], 400)
        self.assertEqual(post(gateway, local_path, token)[0], 404)
        alias = {'approval_type': 'structured_proposal'}
        reject_id = self.store.create(owner, 'chat', *FORWARD)['proposal_id']
        decision_path = '/api/repairs/'+reject_id+'/decision'
        self.assertEqual(post(gateway, decision_path, other_token, dict(alias, decision='reject'))[0], 404)
        self.assertEqual(post(gateway, decision_path, token, dict(alias, decision='reject'))[1]['status'], 'rejected')
        self.assertEqual(post(gateway, public_path, other_token, alias)[0], 404)
        server.approved_repair.assert_not_called()
        status, result = post(gateway, public_path, token, alias)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'executed')
        self.assertEqual(result['review']['summary'], 'IPv4 转发：已开启（1）')
        self.assertEqual(post(server, local_path, token), (200, result))
        server.approved_repair.assert_called_once_with(*FORWARD)
        probe.assert_called_once_with(FORWARD[0], 'routes')
        server.agent_v2 = Mock()
        server.agent_v2.chat.return_value = {'status': 'answered', 'answer': '已完成只读核实。'}
        payload = {'verify_proposal_id': proposal, 'conversation_id': 'chat', 'allow_context': True,
                   'execution_mode': 'approval'}
        self.assertEqual(post(gateway, '/api/chat', other_token, payload)[0], 404)
        self.assertEqual(post(gateway, '/api/chat', token, dict(payload, conversation_id='other'))[0], 404)
        self.assertEqual(post(gateway, '/api/chat', token, dict(payload, verify_proposal_id=reject_id))[0], 409)
        self.assertEqual(post(gateway, '/api/chat', token, payload)[0], 200)
        self.assertEqual(post(gateway, '/api/chat', token, payload)[0], 409)
        server.agent_v2.chat.assert_called_once()
        receipt = server.agent_v2.chat.call_args.kwargs['verification_receipt']
        self.assertEqual(receipt['steps'][0]['action'], FORWARD[1])
        self.assertEqual(receipt['execution'], result['execution'])
        self.assertEqual(receipt['review'], result['review'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for inline UI check')
    def test_approval_updates_card_then_continues_once_in_originating_chat(self):
        page = (Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf-8')
        script = page[page.index('function renderRepairProposal('):page.index('function renderAgentDetails(')]
        harness = r"""
const assert=require('node:assert/strict');
let change, buttons, requests, routes, action;
const account={}, accountEpoch=1, executionMode='auto', c={id:'chat-a',messages:[]};
const state={mode:'qa',revision:2};
const interaction=()=>state, current=()=>c, chatBusy=()=>false, render=()=>{}, save=()=>{};
const spaceOf=()=> 'general', modelHistoryView=()=>({history:[]}), revealReply=()=>{};
const startChatRun=()=>({progress:{},controller:new AbortController(),finish(){}});
function add(parent,tag,label){
  const item={dataset:{},setAttribute(){}};
  if(tag==='button') buttons[label]=item;
  return item;
}
async function api(path,payload){
  routes.push([path,payload]);
  if(path.startsWith('/api/proposals/'))throw Object.assign(Error('接口不存在'),{status:404});
  return action==='拒绝'?{status:'rejected'}:
    {status:'executed',step_results:[{review:{summary:'初步复查'}}]};
}
async function requestChat(payload){
  assert.equal(change.status,'executed');
  assert.equal(change.step_results[0].review.summary,'初步复查');
  assert.equal(change.verifying,true);
  requests.push(payload);
  return {response:{ok:true},data:{answer:'验证结果'}};
}
(async()=>{
  for(action of ['批准执行','拒绝']){
    change={proposal_id:'a'.repeat(32),status:'pending_approval',approval_button_rendered:true};
    buttons={};requests=[];routes=[];
    renderRepairProposal({},change,c);
    await buttons[action].onclick();
    assert.equal(routes.length,2);
    assert.equal(routes[1][1].approval_type,'structured_proposal');
    if(action==='批准执行'){
      assert.equal(requests.length,1);
      assert.equal(requests[0].verify_proposal_id,change.proposal_id);
      assert.equal(requests[0].conversation_id,c.id);
      assert.equal(requests[0].execution_mode,executionMode);
      assert.equal(c.messages.at(-1).content,'验证结果');
    }else{
      assert.equal(change.status,'rejected');
      assert.equal(requests.length,0);
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-'], input=script+harness, capture_output=True,
                                encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
