"""评估入口的隔离、审批、失败保留及记账；响应替身不代表模型准确率。"""
if not __package__:
    import _bootstrap

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from chat_model import ModelFailure, make_model
from evaluation.ha_model import (DEVICE, EXPECTED, VARIANTS, FixtureOpener,
    FixtureResponse, isolated_io, run_batch, main, SIMULATION_NOTICE)

FAKE_CONFIG = {'LLM_API_KEY': 'ONLY-TEST-NOT-A-REAL-SECRET',
    'LLM_MODEL_ID': 'test-model', 'LLM_BASE_URL': 'https://api.inferera.com/v1'}


class SequencedOpener(FixtureOpener):
    def __init__(self, failure=None, known_usage=False, bad_target=False, recover=False):
        self.calls = 0
        self.failure = failure
        self.known_usage = known_usage
        self.bad_target = bad_target
        self.recover = recover

    def open(self, request, timeout):
        self.calls += 1
        if self.failure and (not self.known_usage or self.calls >= 2 and (not self.recover or self.calls == 2)):
            if self.known_usage:
                return FixtureResponse({'usage': {'total_tokens': 23},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': 'invalid JSON'}}]})
            raise ModelFailure(self.failure)
        if self.bad_target:
            return FixtureResponse({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps({'action': 'read',
                    'device': 'remote-primary', 'check': 'services'})}}]})
        response = super().open(request, timeout)
        if self.known_usage:
            body = json.loads(response.data.decode('utf-8'))
            body['usage'] = {'total_tokens': 11}
            return FixtureResponse(body)
        return response


class ModelEvaluationTests(unittest.TestCase):
    def test_export_marks_simulation_even_when_model_omits_it_and_keeps_raw_action(self):
        class UnlabelledOpener(FixtureOpener):
            def open(self,request,timeout):
                response=super().open(request,timeout)
                body=json.loads(response.data.decode('utf-8'))
                action=json.loads(body['choices'][0]['message']['content'])
                if action.get('action')=='answer':
                    action['answer']=action['answer'].replace('针对模拟样本：','')
                    body['choices'][0]['message']['content']=json.dumps(action,ensure_ascii=False)
                return FixtureResponse(body)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'unlabelled'
            run_batch(output,['owner_stopped'],'offline',opener=UnlabelledOpener())
            row=json.loads((output/'owner_stopped.json').read_text(encoding='utf-8'))
            raw=row['adapter_calls'][-1]['action']['answer']
            self.assertFalse(raw.startswith(SIMULATION_NOTICE))
            from response_contract import plain_language_answer
            self.assertIn('按已有正常HA资料比较', raw)
            self.assertEqual(row['result']['answer'],SIMULATION_NOTICE+'\n\n'+plain_language_answer(raw))
            self.assertNotEqual(row['result']['answer'],SIMULATION_NOTICE+'\n\n'+raw)
            self.assertEqual(row['result']['evaluation_provenance']['live_device_reads'],0)
            self.assertIsNone(row['human_review']['reviewer'])

    def test_prepare_lists_reviewable_inputs_without_reading_config_or_calling_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'prepared'
            loader = Mock(side_effect=AssertionError('Must not read secrets'))
            with patch('evaluation.ha_model.chat_model.make_model') as factory:
                report = run_batch(output, VARIANTS, config_loader=loader)
            loader.assert_not_called(); factory.assert_not_called()
            self.assertEqual(report['model_api_calls'], 0)
            self.assertEqual(report['cases'], [])
            scope = json.loads((output / 'scope.json').read_text(encoding='utf-8'))
            self.assertEqual(scope['device'], DEVICE)
            self.assertEqual(scope['knowledge_sources'], [])
            self.assertEqual(set(scope['fixtures']), set(VARIANTS))
            self.assertEqual(scope['destination'], 'https://api.inferera.com/v1/chat/completions')
            self.assertIn('不读取模型配置', scope['notes'])
            self.assertTrue((output / 'system-prompt.txt').is_file())

    def test_seven_offline_adapter_cases_preserve_inputs_audit_refs_and_blank_review(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'offline'
            loader = Mock(side_effect=AssertionError('Must not read secrets'))
            report = run_batch(output, VARIANTS, 'offline', config_loader=loader)
            loader.assert_not_called()
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(len(report['cases']), 7)
            self.assertEqual(report['model_api_calls'], 0)
            self.assertEqual(report['live_device_reads'], 0)
            self.assertIsNone(report['total_tokens'])
            for variant in VARIANTS:
                row = json.loads((output / (variant + '.json')).read_text(encoding='utf-8'))
                self.assertEqual(row['result']['status'], 'incomplete' if variant=='read_failure' else 'answered', variant)
                if variant=='read_failure':
                    self.assertEqual(row['result']['reason'],'required_state_unavailable')
                    self.assertEqual(row['adapter_calls'],[])
                self.assertEqual(row['expected_host_finding'], EXPECTED[variant])
                self.assertTrue(row['automatic_checks']['expected_host_finding_observed'], variant)
                self.assertTrue(row['automatic_checks']['core_checks_collected'])
                self.assertTrue(row['automatic_checks']['read_budget_respected'])
                self.assertTrue(row['automatic_checks']['no_duplicate_reads'])
                self.assertIn('合成场景推演', row['result']['answer'])
                self.assertFalse(row['result']['diagnostic_case']['repair_executed'])
                self.assertTrue(row['audit'])
                if row['adapter_calls']:
                    self.assertTrue(row['adapter_calls'][0]['transport']['request_bytes'] > 0)
                for context in row['model_inputs']:
                    self.assertEqual(context['sources'], [])
                    self.assertEqual(context['history'], [])
                    self.assertEqual(context['evaluation_context']['live_device_reads'], 0)
                    self.assertTrue(set(context['tool_catalog']) <= {DEVICE})
                    attempted={(item['device'],item['check']) for item in context['attempted_checks']}
                    for device,entry in context['tool_catalog'].items():
                        self.assertTrue(all((device,check) not in attempted for check in entry['checks']))
                    if context['remaining_reads']==0:
                        self.assertEqual(context['tool_catalog'],{})
                self.assertIsNone(row['human_review']['reviewer'])
                self.assertTrue(all(q['score'] is None for q in row['human_review']['items']))

    def test_offline_guard_blocks_dns_external_socket_worker_and_all_processes_then_restores(self):
        old = (socket.getaddrinfo, socket.socket.connect, subprocess.Popen)
        with isolated_io() as state:
            state.model_call = True  # offline仍不允许模型网络。
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo('api.inferera.com', 443)
            for address in [('198.51.100.1', 443), '/tmp/ata-worker.sock']:
                with socket.socket() as sock:
                    with self.assertRaises(RuntimeError):
                        sock.connect(address)
            with self.assertRaises(RuntimeError):
                subprocess.Popen(['ssh', 'unregistered'])
        self.assertEqual(old, (socket.getaddrinfo, socket.socket.connect, subprocess.Popen))

    def test_model_guard_requires_active_call_fixed_dns_resolved_address_and_https_port(self):
        dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.51.100.25', 443))])
        connect = Mock()
        with patch('socket.getaddrinfo', dns), patch('socket.socket.connect', connect):
            with isolated_io(True) as state:
                with self.assertRaises(RuntimeError):
                    socket.getaddrinfo('api.inferera.com', 443)
                state.model_call = True
                with self.assertRaises(RuntimeError):
                    socket.getaddrinfo('other.example', 443)
                socket.getaddrinfo('api.inferera.com', 443)
                with socket.socket() as sock:
                    for address in [('198.51.100.26', 443), ('198.51.100.25', 22), '/tmp/worker.sock']:
                        with self.assertRaises(RuntimeError):
                            sock.connect(address)
                    sock.connect(('198.51.100.25', 443))
                    state.model_call = False
                    with self.assertRaises(RuntimeError):
                        sock.connect(('198.51.100.25', 443))
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(connect.call_count, 1)

    def test_injected_transport_still_rejects_unapproved_destination_before_call(self):
        opener = Mock()
        with self.assertRaises(ValueError):
            make_model(dict(FAKE_CONFIG, LLM_BASE_URL='https://other.example/v1'), opener)
        opener.open.assert_not_called()

    def test_model_unapproved_target_rejected_after_only_approved_initial_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'rejected'
            report = run_batch(output, ['owner_stopped'], 'offline', opener=SequencedOpener(bad_target=True))
            row = json.loads((output / 'owner_stopped.json').read_text(encoding='utf-8'))
            self.assertEqual(row['result']['reason'], 'invalid_action_or_citation')
            self.assertEqual(row['synthetic_reads'], [
                {'device':DEVICE,'check':'services'},{'device':DEVICE,'check':'interfaces'}])
            self.assertFalse(any(r['device']=='remote-primary' for r in row['synthetic_reads']))
            self.assertEqual(report['model_api_calls'], 0)
            self.assertFalse(row['automatic_checks']['response_completed'])

    def test_usage_returned_with_invalid_json_is_kept_and_failure_stops_next_case(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'bad-json'
            transport = SequencedOpener('model_invalid_json', known_usage=True)
            report = run_batch(output, ['owner_stopped', 'owner_running'], 'model',
                config_loader=lambda: dict(FAKE_CONFIG), opener=transport)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(report['known_total_tokens'], 57)
            self.assertEqual(report['total_tokens'], 57)
            self.assertEqual(report['unknown_usage_calls'], 0)
            self.assertEqual(report['model_api_calls'], 3)
            self.assertEqual(report['unexecuted_cases'], ['owner_running'])
            row = json.loads((output / 'owner_stopped.json').read_text(encoding='utf-8'))
            self.assertEqual(row['result']['reason'], 'model_invalid_json')
            self.assertEqual([r['check'] for r in row['synthetic_reads']], ['services','interfaces','logs'])
            self.assertEqual(row['adapter_calls'][-1]['usage'], 23)
            self.assertTrue(row['audit'])
            self.assertEqual(transport.calls, 3)

    def test_one_successful_json_correction_keeps_failed_usage_and_allows_next_case(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'corrected'
            transport = SequencedOpener('model_invalid_json', known_usage=True, recover=True)
            report = run_batch(output, ['owner_stopped', 'partial_vip'], 'model',
                config_loader=lambda: dict(FAKE_CONFIG), opener=transport)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['unexecuted_cases'], [])
            self.assertEqual(report['known_total_tokens'], 56)
            self.assertEqual(report['model_api_calls'], 4)
            row = json.loads((output / 'owner_stopped.json').read_text(encoding='utf-8'))
            self.assertEqual(row['result']['status'], 'answered')
            corrections = [e for e in row['result']['events'] if e['action'] == 'correction']
            self.assertEqual(len(corrections), 1)
            self.assertEqual(row['adapter_calls'][1]['usage'], 23)

    def test_timeout_usage_stays_unknown_and_old_failed_directory_cannot_be_rerun(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'timeout'
            loader = Mock(return_value=dict(FAKE_CONFIG))
            transport = SequencedOpener('model_timeout')
            report = run_batch(output, ['owner_stopped', 'read_failure'], 'model', loader, transport)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(report['known_total_tokens'], 0)
            self.assertEqual(report['unknown_usage_calls'], 1)
            self.assertIsNone(report['total_tokens'])
            self.assertEqual(report['model_api_calls'], 1)
            self.assertEqual(report['unexecuted_cases'], ['read_failure'])
            row=json.loads((output/'owner_stopped.json').read_text(encoding='utf-8'))
            self.assertTrue(row['result']['answer'].startswith(SIMULATION_NOTICE))
            before = (output / 'run.json').read_bytes()
            loader.reset_mock()
            with self.assertRaises(FileExistsError):
                run_batch(output, ['owner_stopped'], 'model', loader, transport)
            loader.assert_not_called()
            self.assertEqual((output / 'run.json').read_bytes(), before)
            self.assertEqual(transport.calls, 1)

    def test_successful_responses_without_usage_are_not_reported_as_zero_token_total(self):
        with tempfile.TemporaryDirectory() as temp:
            report = run_batch(Path(temp) / 'unknown-usage', ['owner_stopped'], 'model',
                config_loader=lambda: dict(FAKE_CONFIG), opener=FixtureOpener())
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['model_api_calls'], 2)
            self.assertEqual(report['unknown_usage_calls'], 2)
            self.assertEqual(report['known_total_tokens'], 0)
            self.assertIsNone(report['total_tokens'])

    def test_setup_error_does_not_print_or_store_credentials_or_exception_text(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'bad-config'
            loader = Mock(side_effect=ValueError(FAKE_CONFIG['LLM_API_KEY']))
            report = run_batch(output, ['owner_stopped'], 'model', loader, Mock())
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(report['model_api_calls'], 0)
            for path in output.iterdir():
                self.assertNotIn(FAKE_CONFIG['LLM_API_KEY'], path.read_text(encoding='utf-8'))

    def test_duplicate_or_unknown_cases_are_refused_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'invalid'
            for cases in [[], ['not-registered'], ['owner_stopped', 'owner_stopped']]:
                with self.assertRaises(ValueError):
                    run_batch(output, cases)
                self.assertFalse(output.exists())

    def test_config_path_is_refused_in_prepare_and_offline_before_read_or_run(self):
        with tempfile.TemporaryDirectory() as temp:
            for mode in ('prepare', 'offline'):
                with patch.object(sys, 'argv', ['ha_model.py', '--mode', mode,
                    '--config', 'must-not-be-read.json', '--output', str(Path(temp) / mode)]):
                    with patch('evaluation.ha_model.run_batch') as runner:
                        with self.assertRaises(SystemExit) as error:
                            main()
                        self.assertEqual(error.exception.code, 2)
                        runner.assert_not_called()
                self.assertFalse((Path(temp) / mode).exists())


if __name__ == '__main__':
    unittest.main()
