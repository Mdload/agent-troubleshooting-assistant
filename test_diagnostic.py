import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from diagnostic_core import AuditStore, run_diagnosis
from device_tools import build_command, probe

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = AuditStore(Path(self.temp.name)/'audit.sqlite3')

    def tearDown(self): self.temp.cleanup()

    def test_adaptive_loop_without_knowledge_hits(self):
        def planner(context):
            if not context['evidence']:
                return {'action':'check','device':'local-primary','check':'services'}
            if 'inactive' in context['evidence'][-1]['output']:
                return {'action':'check','device':'local-primary','check':'interfaces'}
            return {'action':'finish','answer':'服务未运行且没有VIP，需结合角色判断。','evidence_ids':['OBS-1','OBS-2']}
        tool = Mock(side_effect=[{'ok':True,'output':'ActiveState=inactive'}, {'ok':True,'output':'VIP absent'}])
        result=run_diagnosis('资料没有记录的异常',planner,tool,self.audit,['local-primary'])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(tool.call_count,2)
        self.assertEqual(len([r for r in self.audit.read(result['task']) if r[1]=='observation']),2)

    def test_unregistered_target_and_commands_rejected(self):
        for device,check in [('203.0.113.250','services'),('local-primary','reboot'),('local-primary; id','services')]:
            with self.assertRaises(ValueError): build_command(device,check)
        tool=Mock()
        result=run_diagnosis('检查',lambda c:{'action':'check','device':'local-backup','check':'services'},tool,self.audit,['local-primary'])
        self.assertEqual(result['status'],'incomplete');tool.assert_not_called()

    def test_failure_stops_without_claiming_success(self):
        tool=Mock(return_value={'ok':False,'error':'identity_mismatch','output':''})
        result=run_diagnosis('检查',lambda c:{'action':'check','device':'local-primary','check':'services'},tool,self.audit,['local-primary'])
        self.assertEqual(result['reason'],'identity_mismatch');self.assertEqual(tool.call_count,1)

    def test_duplicate_and_budget_stop(self):
        tool=Mock(return_value={'ok':True,'output':'active'})
        action={'action':'check','device':'local-primary','check':'services'}
        result=run_diagnosis('检查',lambda c:action,tool,self.audit,['local-primary'],1)
        self.assertEqual(result['reason'],'check_budget_reached');self.assertEqual(tool.call_count,1)

    def test_fabricated_observation_rejected(self):
        action={'action':'finish','answer':'已恢复','evidence_ids':['OBS-999']}
        result=run_diagnosis('检查',lambda c:action,Mock(),self.audit,['local-primary'])
        self.assertEqual(result['status'],'incomplete')

    def test_transport_failures_hide_stderr(self):
        for rc,error in [(41,'target_hostname_mismatch'),(42,'check_failed'),
                         (43,'ingress_hostname_mismatch'),(44,'ingress_user_mismatch'),
                         (45,'ingress_address_mismatch'),(46,'target_user_mismatch'),
                         (47,'target_address_mismatch'),(255,'ssh_failed')]:
            fake=Mock(return_value=subprocess.CompletedProcess([],rc,b'',b'PRIVATE-SENTINEL'))
            result=probe('local-primary','services',fake)
            self.assertEqual(result['error'],error);self.assertNotIn('PRIVATE-SENTINEL',str(result))
        result=probe('local-primary','services',Mock(side_effect=subprocess.TimeoutExpired('ssh',20)))
        self.assertEqual(result['error'],'timeout')

    def test_success_and_identity_prefix(self):
        fake=Mock(return_value=subprocess.CompletedProcess([],0,b'IDENTITY_OK\nActiveState=active\n',b''))
        result=probe('local-primary','services',fake)
        self.assertTrue(result['ok']);self.assertIn('collected_at',result)
        args=fake.call_args[0][0]
        self.assertEqual(args[:4],['sudo','-n','-H','/usr/bin/ssh'])
        self.assertNotIn('shell',fake.call_args[1])
