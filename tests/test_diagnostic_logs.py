"""固定日志工具、审批与多轮取证。替身模型/执行器，不连接设备。"""
if not __package__:
    import _bootstrap

import json
import ast
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from diagnostic_logs import project_logs, LOG_LIMIT, SAFE_LIMIT, RAW_LIMIT
from device_tools import build_command, probe, DEVICES, CHECKS
from diagnostic_core import AuditStore
from question_routing import classify
from unified_chat import run, search
from tests.workflow_test_support import RunningApp, current_probe_fixture
from migration.protocol import REMOTE_CODE
from device_registry import checks_for


def rows(messages):
    return '\n'.join(json.dumps({'MESSAGE':message, '_SYSTEMD_UNIT':'croc-sar.service',
        '__REALTIME_TIMESTAMP':'1000000', 'PRIVATE_FIELD':'not-for-prompt'}, ensure_ascii=False)
        for message in messages).encode('utf-8')


def action(text, refs, basis='evidence'):
    return {'action':'answer','answer':text,'refs':refs,'basis':basis}, 0


class LogToolTests(unittest.TestCase):
    def test_fixed_commands_and_management_paths(self):
        # Linux桥接模块无法在Windows创建Unix服务类；只解析固定远端协议，不执行它。
        scopes=next(ast.literal_eval(n.value) for n in ast.parse(REMOTE_CODE).body if isinstance(n,ast.Assign)
                    and any(isinstance(t,ast.Name) and t.id=='SCOPES' for t in n.targets))
        self.assertEqual(set(scopes),set(DEVICES))
        self.assertTrue(all(value==checks_for(device) for device,value in scopes.items()))
        for device in DEVICES:
            command = build_command(device, 'logs')
            if device.startswith('local-'):
                outer = shlex.split(command[-1])
                nested = outer[outer.index('root@'+DEVICES[device][0])+1]
                body = shlex.split(nested)[2]
            else:
                body = shlex.split(command[-1])[2]
            self.assertIn('journalctl -b -n 40 --no-pager --quiet -o json', body)
            self.assertIn('keepalived.service', body)
            self.assertIn('croc2.0smr.service' if device.startswith('remote-') else 'croc-sar.service', body)
            self.assertNotIn('--all', body)
            self.assertNotIn('--follow', body)
            self.assertNotIn('restart', body)
            self.assertNotIn('health.sh', body)
            self.assertEqual(command[-2], 'root@'+(DEVICES[device][0] if device.startswith('remote-') else '203.0.113.251'))

    def test_sensitive_messages_private_fields_and_key_blocks_are_removed(self):
        output = project_logs(rows(['startup failed', 'password=PRIVATE_PASSWORD',
            'Authorization: Bearer PRIVATE_BEARER', 'https://user:PRIVATE_URL_SECRET@example.net',
            '-----BEGIN PRIVATE KEY-----', 'PRIVATE_PEM_BODY', '-----END PRIVATE KEY-----', 'retry stopped']))
        for secret in ('PRIVATE_PASSWORD','PRIVATE_BEARER','PRIVATE_URL_SECRET','PRIVATE_PEM_BODY','not-for-prompt'):
            self.assertNotIn(secret, output)
        self.assertIn('startup failed', output)
        self.assertIn('retry stopped', output)
        self.assertIn('LOG_MESSAGES_REDACTED=6', output)
        self.assertIn('不代表当前服务状态', output)
        self.assertIn('日志消息已隐藏',project_logs(rows(['MII'+('A'*70)])))

    def test_json_nontext_messages_and_untrusted_metadata_do_not_escape_projection(self):
        raw = json.dumps({'MESSAGE':[80,82,73,86,65,84,69], '__REALTIME_TIMESTAMP':'PRIVATE_TIME',
                          'UNIT':'PRIVATE_UNIT', '_CMDLINE':'PRIVATE_COMMAND'}).encode()
        output = project_logs(raw)
        for secret in ('PRIVATE_TIME','PRIVATE_UNIT','PRIVATE_COMMAND'):
            self.assertNotIn(secret, output)
        self.assertIn('非文本', output)
        self.assertIn('device_time_us=unknown', output)

    def test_projection_finds_values_before_keyed_record_and_before_truncation(self):
        for header in ('Authorization: Basic ', '"Authorization": "Bearer ', 'password='):
            with self.subTest(header=header):
                marker = 'synthetic-example-value'
                output = project_logs(rows(['previous record ' + marker, 'ordinary event',
                                            'a' * 260 + ' ' + header + marker]))
                self.assertNotIn(marker, output)
                self.assertIn('ordinary event', output)
                self.assertIn('LOG_MESSAGES_REDACTED=2', output)

    def test_malformed_and_oversized_output_is_rejected_without_raw_fallback(self):
        for raw in (b'not json', b'[]', rows(['x']*(LOG_LIMIT+1)), b'x'*(RAW_LIMIT+1)):
            with self.assertRaises((ValueError, UnicodeError)):
                project_logs(raw)
        executor = Mock(return_value=subprocess.CompletedProcess([], 0, b'IDENTITY_OK\nPRIVATE_INVALID_LOG', b''))
        result = probe('local-primary','logs',executor)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'],'log_format_invalid')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_large_chinese_messages_are_bounded_and_recent_entries_retained(self):
        output = project_logs(rows([str(i)+'日志'*1000 for i in range(LOG_LIMIT)]))
        self.assertLessEqual(len(output.encode('utf-8')), SAFE_LIMIT)
        self.assertIn('39日志', output)
        self.assertIn('消息已截短', output)
        self.assertIn('LOG_RECORDS_OMITTED=', output)

    def test_empty_log_is_a_successful_bounded_read_not_a_health_claim(self):
        executor = Mock(return_value=subprocess.CompletedProcess([], 0, b'IDENTITY_OK\n', b''))
        result = probe('remote-primary','logs',executor)
        self.assertTrue(result['ok'])
        self.assertIn('LOG_RECORDS_RETURNED=0', result['output'])
        self.assertEqual(result['evidence_role'],'event_history')

    def test_log_read_requires_same_identity_and_discards_failed_partial_output(self):
        executor = Mock(return_value=subprocess.CompletedProcess([], 41,
            b'IDENTITY_OK\nPRIVATE_PARTIAL_LOG', b'PRIVATE_STDERR'))
        result = probe('remote-primary','logs',executor)
        self.assertEqual(result['error'],'target_hostname_mismatch')
        self.assertEqual(result['output'],'')
        self.assertNotIn('PRIVATE',json.dumps(result))


class LogWorkflowTests(unittest.TestCase):
    def test_request_adds_logs_only_to_relevant_scopes_and_explanations_stay_read_free(self):
        route = classify('局端主机现在Croc没运行，帮我检查')
        self.assertIn('logs',route['scope']['local-primary'])
        self.assertNotIn('logs',classify('请检查远端主机路由')['scope']['remote-primary'])
        self.assertEqual(classify('局端备机为什么不运行Croc')['route'],'lab_knowledge')
        self.assertEqual(classify('journalctl日志怎么查看')['domain'],'basics')

    def test_one_approval_allows_state_then_logs_and_model_receives_feedback(self):
        contexts = []
        def planner(context):
            contexts.append(context)
            evidence = context['observations']
            if not evidence:
                return {'action':'read_overview','device':'local-primary'},0
            if len(evidence)==2 and 'ActiveState=failed' in evidence[0]['output']:
                return {'action':'read','device':'local-primary','check':'logs'},0
            return action('采集时Croc服务为failed，接口有业务VIP；日志记载启动失败，具体原因和业务质量还需核实。',
                          ['OBS-1','OBS-2','OBS-3'])
        def tool(device, check):
            item = current_probe_fixture(device, check)
            if check=='services': item['output']='ActiveState=failed\nMainPID=0'
            elif check=='interfaces': item['output']='eth2 UP 192.0.2.13/24 192.0.2.15/24'
            elif check=='logs': item['output']=project_logs(rows(['startup failed','password=PRIVATE_TEST_VALUE']))
            return item
        probe_mock=Mock(side_effect=tool)
        with RunningApp(planner,probe_mock,lookup=Mock(return_value=[])) as client:
            _, requested=client.diagnostic_chat('局端主机现在Croc没运行，帮我检查')
            self.assertEqual(requested['status'],'approval_required')
            self.assertEqual(contexts,[]);probe_mock.assert_not_called()
            task=requested['task']
            self.assertIn('logs',task['scope']['local-primary'])
            self.assertEqual(client.resume(task['id'])[0],409);probe_mock.assert_not_called()
            client.login();client.approve(task['id'])
            _, completed=client.resume(task['id'])
            self.assertEqual(completed['status'],'completed',completed)
            result=completed['result']
            self.assertEqual([e['check'] for e in result['evidence']],['services','interfaces','logs'])
            self.assertEqual(contexts[1]['observations'][0]['output'],'ActiveState=failed\nMainPID=0')
            self.assertEqual(result['evidence'][2]['evidence_role'],'event_history')
            self.assertNotIn('PRIVATE_TEST_VALUE',json.dumps(result))
            self.assertNotIn('PRIVATE_TEST_VALUE',json.dumps(client.service.audit.read(task['id'])))
            self.assertEqual(client.resume(task['id'])[0],409)
        self.assertEqual(probe_mock.call_count,3)

    def test_unapproved_log_scope_is_rejected_before_execution(self):
        planner=Mock(return_value=({'action':'read','device':'local-primary','check':'logs'},0))
        probe_mock=Mock()
        with tempfile.TemporaryDirectory() as directory:
            result=run('局端主机状态',[],planner,Mock(return_value=[]),probe_mock,
                AuditStore(Path(directory)/'audit.db'),permitted_checks={'local-primary':['services']},
                question_policy={'mode':'diagnostic','domain':'topology','require_current_evidence':True})
        self.assertTrue(any(e.get('reason')=='check_not_approved' for e in result['events']))
        probe_mock.assert_not_called()

    def test_log_events_alone_cannot_satisfy_current_status_evidence(self):
        planner=Mock(side_effect=[({'action':'read','device':'local-primary','check':'logs'},0),
            action('当前服务状态已确定。',['OBS-1']), action('当前服务状态已确定。',['OBS-1'])])
        tool=Mock(side_effect=current_probe_fixture)
        with tempfile.TemporaryDirectory() as directory:
            result=run('局端主机当前Croc状态',[],planner,Mock(return_value=[]),tool,
                AuditStore(Path(directory)/'audit.db'),permitted_checks={'local-primary':['logs']},
                question_policy={'mode':'diagnostic','domain':'topology','require_current_evidence':True})
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['reason'],'current_evidence_required')
        self.assertEqual(tool.call_count,1)

    def test_knowledge_question_cannot_call_logs_even_when_model_requests_it(self):
        planner=Mock(return_value=({'action':'read','device':'local-primary','check':'logs'},0))
        probe_mock=Mock()
        with RunningApp(planner,probe_mock) as client:
            _, result=client.chat('局端备机为什么不运行Croc')
            self.assertEqual(result['status'],'incomplete')
            self.assertEqual(client.store.list(),[])
        probe_mock.assert_not_called()


if __name__=='__main__':
    unittest.main()
