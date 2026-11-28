"""固定登记的一致性和工具边界；无模型或设备调用。"""
import ast
import shlex
import unittest
from unittest.mock import Mock,patch
import device_tools
from device_registry import DEVICES,PROFILES,HOPS,public_catalog,allows_check,checks_for
from diagnostic_logs import UNITS
from interaction_mode import SCENES
from question_routing import target_devices
from migration.protocol import REMOTE_CODE
from workflow_test_support import RunningApp


class RegistryTests(unittest.TestCase):
    def test_all_runtime_consumers_share_registered_targets_and_checks(self):
        self.assertEqual(len(DEVICES),6)
        self.assertEqual(device_tools.DEVICES,{key:(d['address'],d['hostname']) for key,d in DEVICES.items()})
        self.assertEqual(device_tools.DEVICE_LABELS,{key:d['label'] for key,d in DEVICES.items()})
        self.assertEqual(set(SCENES['ha_topology']['devices']),set(DEVICES))
        self.assertEqual(set(SCENES['croc_local']['devices']),{'local-primary','local-backup'})
        self.assertEqual(SCENES['remote_device']['devices'],())
        for key,d in DEVICES.items():
            self.assertEqual(set(device_tools.tool_catalog()[key]['checks']),set(checks_for(key)))
        self.assertEqual(UNITS['local'],PROFILES['local_ha']['units'])
        self.assertEqual(UNITS['remote'],PROFILES['remote_ha']['units'])

    def test_target_routing_is_derived_from_inventory_roles_not_live_roles(self):
        self.assertEqual(target_devices('远端主机'),['remote-primary'])
        self.assertEqual(set(target_devices('局端主备')),{'local-primary','local-backup'})
        self.assertFalse(allows_check('198.51.100.46','services'))
        self.assertFalse(allows_check('local-primary','restart'))
        self.assertTrue(all(d['current_role']=='unknown' for d in public_catalog()['devices']))

    def test_bridge_protocol_uses_generated_per_device_whitelist(self):
        tree=ast.parse(REMOTE_CODE)
        scope=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and
                   any(isinstance(t,ast.Name) and t.id=='SCOPES' for t in n.targets))
        self.assertEqual(scope,{key:checks_for(key) for key in DEVICES})
        self.assertIn(repr(HOPS['jump_host']['hostname']),REMOTE_CODE)
        self.assertNotIn('root@',REMOTE_CODE)

    def test_local_absent_tunnel_is_explicit_and_commands_keep_identity_guards(self):
        cmd=device_tools.build_command('local-backup','interfaces')
        words=shlex.split(cmd[-1]);nested=words[words.index('root@192.0.2.14')+1]
        body=shlex.split(nested)[2]
        self.assertIn('INTERFACE_ABSENT=tun16',body)
        self.assertIn('192.0.2.14/24',body);self.assertIn('|| exit 42',body)
        self.assertNotIn('health.sh',body);self.assertNotIn('restart',body)

    def test_http_registry_has_reviewable_addresses_without_execute_capability(self):
        probe=Mock()
        with RunningApp(Mock(),probe) as client:
            code,data=client.call('/api/devices')
            self.assertEqual(code,200);self.assertEqual(data,public_catalog())
            self.assertFalse(data['current_state_verified']);self.assertEqual(client.store.list(),[])
        probe.assert_not_called()


if __name__=='__main__': unittest.main()
