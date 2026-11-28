"""模型隔离状态、并发恢复和正式HTTP接线；全部使用替身，零外发/设备读取。"""
import json
import io
import socket
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from agent.chat_model import make_model, ModelFailure, retry_delay, rate_limit_kind
from devices.diagnostic_core import AuditStore
from agent.model_availability import ModelAvailability, CircuitOpen
from agent.model_trace import safe_metrics
from legacy.unified_chat import run
from tests.workflow_test_support import RunningApp


CONFIG={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_MODEL_ID':'offline-model',
        'LLM_API_KEY':'offline-only-private-marker'}


def response(content=None):
    action={'action':'answer','answer':'一般原理：列表可修改。','refs':[],'basis':'general'}
    data={'usage':{'total_tokens':17},'choices':[{'message':{'content':
        json.dumps(action) if content is None else content},'finish_reason':'stop'}]}
    item=Mock()
    item.__enter__=Mock(return_value=item)
    item.__exit__=Mock(return_value=False)
    item.read.return_value=json.dumps(data).encode('utf-8')
    return item


class CircuitStateTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.0]
        self.circuit=ModelAvailability(threshold=2,cooldown=10,window=20,clock=lambda:self.now[0])

    def fail(self):
        self.circuit.finish(self.circuit.reserve('backend'),failed=True)

    def test_threshold_then_cooldown_and_successful_probe(self):
        self.fail();self.fail()
        with self.assertRaises(CircuitOpen) as error:self.circuit.reserve('backend')
        self.assertEqual(error.exception.retry_after,10)
        self.now[0]=10
        ticket=self.circuit.reserve('backend')
        with self.assertRaises(CircuitOpen):self.circuit.reserve('backend')
        self.circuit.finish(ticket,failed=False)
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)

    def test_failed_probe_reopens_for_full_cooldown(self):
        self.fail();self.fail();self.now[0]=10
        self.fail()
        with self.assertRaises(CircuitOpen) as error:self.circuit.reserve('backend')
        self.assertEqual(error.exception.retry_after,10)

    def test_success_resets_consecutive_failures(self):
        self.fail()
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)
        self.fail()
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)

    def test_old_failures_expire(self):
        self.fail();self.now[0]=21;self.fail()
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)

    def test_old_inflight_success_cannot_undo_open_state(self):
        old=self.circuit.reserve('backend')
        self.fail();self.fail()
        self.circuit.finish(old,failed=False)
        with self.assertRaises(CircuitOpen):self.circuit.reserve('backend')

    def test_new_configuration_ignores_old_inflight_failure(self):
        old=self.circuit.reserve('old')
        new=self.circuit.reserve('new')
        self.circuit.finish(old,failed=True)
        self.circuit.finish(new,failed=False)
        self.circuit.finish(self.circuit.reserve('new'),failed=False)

    def test_only_one_concurrent_half_open_request_is_admitted(self):
        self.fail();self.fail();self.now[0]=10
        def attempt(_):
            try:return self.circuit.reserve('backend')
            except CircuitOpen:return None
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(attempt,range(20)))
        admitted=[ticket for ticket in results if ticket is not None]
        self.assertEqual(len(admitted),1)
        self.circuit.finish(admitted[0],failed=False)

    def test_repeated_finish_cannot_count_one_failure_twice(self):
        ticket=self.circuit.reserve('backend')
        self.circuit.finish(ticket,failed=True);self.circuit.finish(ticket,failed=True)
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)

    def test_provider_wait_opens_immediately_and_old_success_cannot_clear_it(self):
        old=self.circuit.reserve('backend')
        ticket=self.circuit.reserve('backend')
        self.circuit.finish(ticket,failed=True,retry_after=45)
        self.circuit.finish(old,failed=False)
        self.now[0]=44
        with self.assertRaises(CircuitOpen) as blocked:self.circuit.reserve('backend')
        self.assertEqual(blocked.exception.retry_after,1)
        self.now[0]=45
        probe=self.circuit.reserve('backend')
        with self.assertRaises(CircuitOpen):self.circuit.reserve('backend')
        self.circuit.finish(probe,failed=False)
        self.circuit.finish(self.circuit.reserve('backend'),failed=False)


class FallbackTests(unittest.TestCase):
    alternate_url = 'https://alternate.example/v1'
    alternate = dict(CONFIG, LLM_BASE_URL=alternate_url, LLM_MODEL_ID='alternate-model',
                     LLM_API_KEY='alternate-test-key', LLM_JSON_MODE=True)

    def test_explicit_endpoints_are_validated_before_any_request(self):
        opener = Mock()
        for endpoint in ('https://other.example/v1', 'http://alternate.example/v1',
                         'https://user@alternate.example/v1', self.alternate_url + '?unsafe'):
            with self.subTest(endpoint=endpoint):
                config = dict(CONFIG, LLM_FALLBACKS=[dict(self.alternate, LLM_BASE_URL=endpoint)])
                with self.assertRaisesRegex(ValueError, '^Unapproved destination$'):
                    make_model(config, opener, approved_urls=[self.alternate_url])
        with self.assertRaisesRegex(ValueError, '^Model request option must be boolean$'):
            make_model(dict(CONFIG, LLM_FALLBACKS=[dict(self.alternate, LLM_JSON_MODE=1)]),
                       opener, approved_urls=[self.alternate_url])
        opener.open.assert_not_called()

        opener.open.return_value = response()
        planner = make_model(dict(self.alternate, LLM_BASE_URL=self.alternate_url + '/',
                                  LLM_APPROVED_BASE_URLS=[self.alternate_url]), opener)
        self.assertEqual(planner({})[1], 17)
        self.assertEqual(opener.open.call_args[0][0].full_url, self.alternate_url + '/chat/completions')

    def test_timeout_switches_transport_within_budget_and_keeps_unknown_usage(self):
        now = [0.0]
        opener = Mock()

        def transport(request, timeout):
            if request.full_url.startswith(CONFIG['LLM_BASE_URL']):
                now[0] += timeout
                raise socket.timeout('upstream details')
            self.assertLessEqual(now[0] + timeout, 10.0)
            body = json.loads(request.data.decode('utf-8'))
            self.assertEqual(body['model'], 'alternate-model')
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            return response()

        opener.open.side_effect = transport
        planner = make_model(dict(CONFIG, LLM_FALLBACKS=[self.alternate]), opener,
                             approved_urls=[self.alternate_url])
        with patch('agent.chat_model.time.monotonic', side_effect=lambda: now[0]):
            action, usage = planner({'remaining_seconds': 10.0})
        self.assertEqual(action['action'], 'answer')
        self.assertIsNone(usage)
        self.assertEqual(opener.open.call_count, 2)
        metrics = safe_metrics(planner.last_metrics)
        self.assertEqual(metrics['transport_attempts'], 2)
        for private in (self.alternate_url, self.alternate['LLM_API_KEY'], CONFIG['LLM_API_KEY']):
            self.assertNotIn(private, str(metrics))

    def test_circuits_stay_independent_across_configuration_loads(self):
        circuit = ModelAvailability(threshold=1)
        config = dict(CONFIG, LLM_FALLBACKS=[self.alternate])
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('private', 503, '', {}, io.BytesIO()),
                                   response(), response()]
        make = lambda: make_model(config, opener, circuit, [self.alternate_url])
        self.assertIsNone(make()({})[1])  # 主模型 503 的用量未知。
        planner = make()
        self.assertEqual(planner({})[1], 17)  # 主熔断没有发请求，不增加用量。
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(planner.last_metrics['backend_attempts'], 2)
        self.assertEqual(planner.last_metrics['transport_attempts'], 1)

    def test_non_quota_rate_limit_uses_approved_backup(self):
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.HTTPError('private', 429, '', {'Retry-After': '30'},
                                   io.BytesIO(b'{"error":{"code":"concurrency"}}')),
            response()]
        planner = make_model(dict(CONFIG, LLM_FALLBACKS=[self.alternate]), opener,
                             approved_urls=[self.alternate_url])
        action, usage = planner({})
        self.assertEqual(action['action'], 'answer')
        self.assertIsNone(usage)
        self.assertEqual(opener.open.call_count, 2)

    def test_quota_client_error_and_exhausted_budget_do_not_fallback(self):
        for code, body in ((429, b'{"error":{"code":"insufficient_quota"}}'),
                           (400, b'{}')):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError('private', code, '', {}, io.BytesIO(body))
                planner = make_model(dict(CONFIG, LLM_FALLBACKS=[self.alternate]), opener,
                                     approved_urls=[self.alternate_url])
                with self.assertRaises(ModelFailure):
                    planner({})
                opener.open.assert_called_once()
        opener = Mock()
        opener.open.side_effect = socket.timeout()
        planner = make_model(dict(CONFIG, LLM_FALLBACKS=[self.alternate]), opener,
                             approved_urls=[self.alternate_url])
        with self.assertRaisesRegex(ModelFailure, '^model_timeout$'):
            planner({'remaining_seconds': 0.5})
        opener.open.assert_called_once()


class CircuitAdapterTests(unittest.TestCase):
    def test_429_reason_is_bounded_and_raw_private_text_is_never_returned(self):
        messages=[('reached the limit of the free model quota PRIVATE_TOKEN','quota'),
                  ('Model PRIVATE_MODEL rate limited by provider (tid: PRIVATE_ID)','provider_capacity'),
                  ('too many concurrent requests PRIVATE_TOKEN','concurrency'),
                  ('tokens per minute exceeded PRIVATE_TOKEN','token_rate'),
                  ('requests per minute exceeded PRIVATE_TOKEN','request_rate'),
                  ('unexplained PRIVATE_TOKEN','unknown')]
        for message,kind in messages:
            raw=json.dumps({'error':{'message':message}}).encode('utf-8')
            error=urllib.error.HTTPError('offline',429,'PRIVATE',{},io.BytesIO(raw))
            self.assertEqual(rate_limit_kind(error),kind)
        error=Mock();error.read.return_value=b'PRIVATE_NON_JSON'
        self.assertEqual(rate_limit_kind(error),'unknown')
        error.read.assert_called_once_with(16384)

    def test_quota_failure_surfaces_category_and_cooldown_but_not_payload(self):
        opener=Mock()
        raw=json.dumps({'error':{'code':'insufficient_quota','message':'PRIVATE_TOKEN'}}).encode('utf-8')
        opener.open.side_effect=urllib.error.HTTPError('offline',429,'PRIVATE',{},io.BytesIO(raw))
        circuit=ModelAvailability()
        planner=make_model(CONFIG,opener,availability=circuit)
        with tempfile.TemporaryDirectory() as directory:
            result=run('Python列表是什么',[],planner,lambda q:[],Mock(),AuditStore(Path(directory)/'audit.db'),permitted_checks={})
        self.assertEqual(result['rate_limit_kind'],'quota')
        self.assertIn('额度',result['answer'])
        self.assertNotIn('retry_after_seconds',result)
        self.assertNotIn('PRIVATE',json.dumps(result,ensure_ascii=False))
        self.assertFalse(result['total_tokens_complete'])
        with self.assertRaises(ModelFailure) as blocked:planner({})
        self.assertEqual(blocked.exception.code,'model_circuit_open')
        other=make_model(CONFIG,opener,availability=circuit)
        with tempfile.TemporaryDirectory() as directory:
            next_result=run('Python列表是什么',[],other,lambda q:[],Mock(),AuditStore(Path(directory)/'audit.db'),permitted_checks={})
        self.assertEqual(next_result['reason'],'model_circuit_open')
        self.assertEqual(next_result['rate_limit_kind'],'quota')
        self.assertIn('额度',next_result['answer'])
        self.assertNotIn('retry_after_seconds',next_result)
        self.assertTrue(next_result['total_tokens_complete'])
        self.assertEqual(opener.open.call_count,1)

    def test_retry_after_delta_date_invalid_and_extreme_headers_are_bounded(self):
        self.assertEqual(retry_delay({'Retry-After':'90'}),90)
        self.assertEqual(retry_delay({'Retry-After':'Thu, 01 Jan 1970 00:02:00 GMT'},now=0),120)
        for value in ('PRIVATE_MESSAGE','-1','NaN','inf','1.5','x'*129):
            self.assertEqual(retry_delay({'Retry-After':value},fallback=30),30)
        self.assertEqual(retry_delay({'Retry-After':'99999999'}),86400)
        self.assertEqual(retry_delay({'Retry-After':'0'}),1)
        self.assertEqual(retry_delay({'Retry-After':'Thu, 01 Jan 1970 00:00:00 GMT'},now=5),30)

    def test_first_429_cools_all_planners_and_surfaces_wait_without_transport(self):
        now=[0.0]
        circuit=ModelAvailability(threshold=3,cooldown=10,clock=lambda:now[0])
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError('offline',429,'PRIVATE',{'Retry-After':'45'},None)
        planner=make_model(CONFIG,opener,availability=circuit)
        with tempfile.TemporaryDirectory() as directory:
            result=run('Python列表是什么',[],planner,lambda q:[],Mock(),AuditStore(Path(directory)/'audit.db'),permitted_checks={})
        self.assertEqual(result['reason'],'model_rate_limited')
        self.assertEqual(result['retry_after_seconds'],45)
        self.assertFalse(result['total_tokens_complete'])
        self.assertNotIn('PRIVATE',json.dumps(result,ensure_ascii=False))
        other=make_model(CONFIG,opener,availability=circuit)
        with self.assertRaises(ModelFailure) as blocked:other({})
        self.assertEqual((blocked.exception.code,blocked.exception.usage),('model_circuit_open',0))
        self.assertEqual(opener.open.call_count,1)
        now[0]=45;opener.open.side_effect=None;opener.open.return_value=response()
        other({})
        self.assertEqual(opener.open.call_count,2)

    def test_open_circuit_skips_transport_and_reports_known_zero_usage(self):
        opener=Mock();opener.open.side_effect=socket.timeout()
        circuit=ModelAvailability(threshold=1)
        planner=make_model(CONFIG,opener,availability=circuit)
        with self.assertRaises(ModelFailure) as first:planner({})
        self.assertIsNone(first.exception.usage)
        with self.assertRaises(ModelFailure) as blocked:planner({})
        self.assertEqual((blocked.exception.code,blocked.exception.usage),('model_circuit_open',0))
        self.assertEqual(opener.open.call_count,1)
        metrics=safe_metrics(planner.last_metrics)
        self.assertFalse(metrics['transport_attempted'])
        self.assertEqual(metrics['stage'],'capacity_guard')
        self.assertNotIn('private-marker',json.dumps(metrics))

    def test_only_availability_errors_trip_the_circuit(self):
        for status in (429,500,503,401,403,404):
            with self.subTest(status=status):
                opener=Mock();opener.open.side_effect=urllib.error.HTTPError('offline',status,'',{},None)
                planner=make_model(CONFIG,opener,availability=ModelAvailability(threshold=1))
                for _ in range(2):
                    with self.assertRaises(ModelFailure):planner({})
                self.assertEqual(opener.open.call_count,1 if status==429 or status>=500 else 2)

    def test_invalid_model_json_keeps_existing_correction_path(self):
        opener=Mock();opener.open.return_value=response('NOT JSON')
        planner=make_model(CONFIG,opener,availability=ModelAvailability(threshold=1))
        for _ in range(3):
            with self.assertRaises(ModelFailure) as error:planner({})
            self.assertEqual((error.exception.code,error.exception.usage),('model_invalid_json',17))
        self.assertEqual(opener.open.call_count,3)

    def test_configuration_change_and_host_budget_do_not_carry_old_denial(self):
        opener=Mock();opener.open.side_effect=socket.timeout()
        circuit=ModelAvailability(threshold=1)
        planner=make_model(CONFIG,opener,availability=circuit)
        with self.assertRaises(ModelFailure):planner({})
        with self.assertRaises(ModelFailure) as budget:planner({'remaining_seconds':0})
        self.assertEqual((budget.exception.code,budget.exception.usage),('time_budget_reached',0))
        changed=make_model(dict(CONFIG,LLM_API_KEY='another-offline-key'),opener,availability=circuit)
        with self.assertRaises(ModelFailure) as current:changed({})
        self.assertEqual(current.exception.code,'model_timeout')
        self.assertEqual(opener.open.call_count,2)

    def test_run_retains_sources_and_accounts_blocked_call_without_unknown_usage(self):
        opener=Mock();opener.open.side_effect=socket.timeout()
        planner=make_model(CONFIG,opener,availability=ModelAvailability(threshold=1))
        with self.assertRaises(ModelFailure):planner({})
        with tempfile.TemporaryDirectory() as directory:
            result=run('Python列表是什么',[],planner,lambda q:[],Mock(),
                       AuditStore(Path(directory)/'audit.db'),permitted_checks={})
        self.assertEqual(result['reason'],'model_circuit_open')
        self.assertEqual(result['total_tokens'],0)
        self.assertTrue(result['total_tokens_complete'])
        self.assertEqual(result['evidence'],[])
        self.assertEqual(result['model_trace'][0]['outcome'],'model_circuit_open')
        self.assertFalse(result['model_trace'][0]['transport']['transport_attempted'])
        self.assertEqual(opener.open.call_count,1)

    def test_formal_http_shares_breaker_across_planners_and_no_consent_sends_nothing(self):
        opener=Mock();opener.open.side_effect=socket.timeout()
        probe=Mock(side_effect=AssertionError('No device read permitted'))
        with RunningApp(probe=probe,lookup=lambda q:[]) as client:
            client.service.model_availability=ModelAvailability(threshold=2)
            with patch('legacy.unified_chat.qa.model_config',return_value=CONFIG), \
                 patch('agent.chat_model.urllib.request.build_opener',return_value=opener):
                self.assertEqual(client.chat('Python列表是什么',consent=False)[1]['status'],'consent_required')
                opener.open.assert_not_called()
                results=[client.chat('Python列表是什么')[1] for _ in range(3)]
            self.assertEqual([r.get('reason') for r in results],['model_timeout','model_timeout','model_circuit_open'])
            self.assertTrue(results[-1]['total_tokens_complete'])
            self.assertEqual(opener.open.call_count,2)
            self.assertEqual(client.store.list(),[])
        probe.assert_not_called()


if __name__=='__main__':unittest.main()
