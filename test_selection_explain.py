"""单次短解释和正式流式HTTP；仅受控传输替身。"""
import io,json,unittest,threading
from unittest.mock import Mock,patch
from urllib.request import Request
from selection_explain import stream_explanation,PROMPT
from chat_model import ModelFailure
from model_availability import ModelAvailability
from workflow_test_support import RunningApp
import app
CONFIG={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_MODEL_ID':'test-model','LLM_API_KEY':'test-only'}
def frame(value):return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode('utf-8')
def response(finish='stop'):
    result=io.BytesIO(frame({'choices':[{'delta':{'content':'第一句。'}}]})+frame({'choices':[{'delta':{'content':'第二句。'},'finish_reason':finish}]})+frame({'choices':[],'usage':{'total_tokens':30}})+b'data: [DONE]\n\n')
    result.headers={'Content-Type':'text/event-stream'};return result
class SelectionExplainTests(unittest.TestCase):
    def test_single_short_stream_emits_before_upstream_complete_and_never_adds_tools(self):
        upstream=response();opener=Mock();opener.open.return_value=upstream;pieces=[];metrics={}
        def emit(kind,value):pieces.append((value['text'],upstream.tell()<len(upstream.getvalue())))
        result=stream_explanation(CONFIG,'示例',emit,lambda:None,metrics,opener=opener)
        self.assertEqual(result['answer'],'第一句。第二句。');self.assertTrue(pieces[0][1]);self.assertEqual(result['total_tokens'],30)
        payload=json.loads(opener.open.call_args[0][0].data.decode('utf-8'))
        self.assertTrue(payload['stream']);self.assertEqual(payload['max_tokens'],768);self.assertEqual(payload['thinking'],{'type':'disabled'})
        self.assertEqual(payload['messages'][1]['content'],'示例');self.assertNotIn('tools',payload);self.assertNotIn('response_format',payload)
        self.assertLess(metrics['prompt_bytes'],1000);opener.open.assert_called_once()
    def test_lab_selection_injects_projected_reference_but_omits_snapshot(self):
        for text, term in (('accept-watchdog', '接收队列'), ('route-envset', 'route-envset.service')):
            with self.subTest(text=text):
                opener = Mock()
                opener.open.return_value = response()
                stream_explanation(CONFIG, text, lambda *v: None, lambda: None, {}, opener=opener)
                payload = json.loads(opener.open.call_args.args[0].data)
                prompt = payload['messages'][0]['content']
                self.assertIn('实验室参考（历史机制与排查经验）', prompt)
                self.assertIn(term, prompt)
                self.assertNotIn('本轮发现', prompt)
                self.assertNotIn('安装但未运行', prompt)
                self.assertNotIn('未运行 |', prompt)
                self.assertLessEqual(prompt.count('\n- '), 2)
                self.assertNotIn('tools', payload)

    def test_generic_selection_keeps_general_prompt(self):
        for text in ('Keepalived', 'TCP 三次握手', 'Recv-Q', 'Python 装饰器'):
            with self.subTest(text=text):
                opener = Mock()
                opener.open.return_value = response()
                stream_explanation(CONFIG, text, lambda *v: None, lambda: None, {}, opener=opener)
                payload = json.loads(opener.open.call_args.args[0].data)
                self.assertEqual(payload['messages'][0]['content'], PROMPT)
                self.assertEqual(payload['messages'][1]['content'], text)

    def test_truncated_stream_keeps_partials_but_never_returns_complete_or_retries(self):
        opener=Mock();opener.open.return_value=response('length');pieces=[]
        with self.assertRaises(ModelFailure) as error:stream_explanation(CONFIG,'示例',lambda *v:pieces.append(v),lambda:None,{},opener=opener)
        self.assertEqual(error.exception.code,'model_output_truncated');self.assertTrue(pieces);opener.open.assert_called_once()
    def test_session_guard_closes_stream_and_no_late_text_is_emitted(self):
        opener=Mock();upstream=response();opener.open.return_value=upstream;calls=[0];pieces=[]
        def guard():
            calls[0]+=1
            if calls[0]>3:raise RuntimeError('session expired')
        with self.assertRaises(RuntimeError):stream_explanation(CONFIG,'示例',lambda *v:pieces.append(v),guard,{},opener=opener)
        self.assertTrue(upstream.closed);self.assertEqual(pieces,[])
    def test_provider_nonstream_json_is_accepted_without_second_model_call(self):
        upstream=io.BytesIO(json.dumps({'choices':[{'message':{'content':'完整解释'},'finish_reason':'stop'}]}).encode());upstream.headers={'Content-Type':'application/json'}
        opener=Mock();opener.open.return_value=upstream
        result=stream_explanation(CONFIG,'示例',lambda *v:None,lambda:None,{},opener=opener)
        self.assertEqual(result['answer'],'完整解释');self.assertFalse(result['total_tokens_complete']);opener.open.assert_called_once()
    def test_http_stream_retains_csrf_origin_size_and_model_quota_no_workflow_or_probe(self):
        with RunningApp(planner=Mock(),probe=Mock()) as client,patch('app.qa.model_config',return_value=CONFIG),patch('selection_explain.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=lambda *a,**k:response()
            self.assertEqual(client.call('/api/selection/explain',{'text':'示例','allow_context':True},token=False)[0],403)
            self.assertEqual(client.call('/api/selection/explain',{'text':'示例','allow_context':True},origin='https://evil.invalid')[0],403)
            self.assertEqual(client.call('/api/selection/explain',{'text':'x'*801,'allow_context':True})[0],400)
            request=Request(client.base+'/api/selection/explain',data=json.dumps({'text':'示例','allow_context':True}).encode(),headers={'Origin':client.base,'Content-Type':'application/json','X-ATA-Token':app.CSRF_TOKEN})
            with client.opener.open(request) as res:body=res.read().decode('utf-8');self.assertIn('text/event-stream',res.headers['Content-Type'])
            self.assertIn('event: delta',body);self.assertIn('event: done',body);self.assertIn('第一句',body)
            client.service.planner.assert_not_called();client.service.probe.assert_not_called()
            self.assertTrue(any(key[0]=='model' for key in client.server.request_limiter.buckets))
