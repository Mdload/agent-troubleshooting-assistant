"""完整JSON边界、脱敏定位和可选供应商参数；无真实模型调用。"""
if not __package__:
    import _bootstrap

import copy
import json
import urllib.error
import unittest
from unittest.mock import Mock
from action_json import decode_action,ActionJSONError
from chat_model import make_model,ModelFailure
from diagnostic_context import model_view
from model_trace import safe_metrics
from evaluation.ha_model import FixtureResponse

CONFIG={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'PRIVATE_TEST_KEY','LLM_MODEL_ID':'offline-fixture'}
ACTION={'action':'answer','answer':'段落一。\n\n`x = "quoted"` 保留反斜杠 \\。','refs':[],'basis':'general'}


def adapter(content=None,finish='stop',**options):
    response=FixtureResponse({'usage':{'total_tokens':73},'choices':[
        {'finish_reason':finish,'message':{'content':content if content is not None else json.dumps(ACTION,ensure_ascii=False)}}]})
    opener=Mock();opener.open.return_value=response
    return make_model(dict(CONFIG,**options),opener),opener


class ModelProtocolTests(unittest.TestCase):
    def test_intake_options_are_available_to_the_model_separately_from_tools(self):
        action={'action':'prepare_diagnostic','scope':{'local-backup':['interfaces']}}
        planner,opener=adapter(json.dumps(action))
        options={'local-backup':{'label':'局端备机','checks':{'interfaces':'接口与VIP'}}}
        actual,_=planner({'workflow_policy':{'mode':'knowledge','domain':'general'},
                          'tool_catalog':{},'diagnostic_options':options})
        self.assertEqual(actual,action)
        messages=json.loads(opener.open.call_args[0][0].data)['messages']
        self.assertIn('prepare_diagnostic',messages[0]['content'])
        view=json.loads(messages[1]['content'])
        self.assertEqual(view['diagnostic_options'],options)
        self.assertEqual(view['tool_catalog'],{})

    def test_complete_json_and_single_fence_preserve_quotes_newlines_and_slashes(self):
        raw=json.dumps(ACTION,ensure_ascii=False)
        for content in (raw,'  '+raw+'\n','```json\r\n'+raw+'\r\n```'):
            self.assertEqual(decode_action(content),ACTION)

    def test_learning_can_read_one_complete_answer_object_with_surrounding_prose(self):
        raw='先解释这件事。\n\n'+json.dumps(ACTION,ensure_ascii=False)+'\n以上是说明。'
        planner,opener=adapter(raw)
        action,usage=planner({'workflow_policy':{'mode':'knowledge'}})
        self.assertEqual(action,ACTION)
        self.assertEqual(usage,73)
        self.assertTrue(safe_metrics(planner.last_metrics)['answer_wrapper_removed'])
        self.assertEqual(opener.open.call_count,1)
        planner,_=adapter(raw)
        with self.assertRaises(ModelFailure):planner({'workflow_policy':{'mode':'diagnostic'}})

    def test_learning_wrapper_does_not_repair_or_choose_between_objects_or_tools(self):
        for raw in ('说明 '+json.dumps(ACTION)+' '+json.dumps(ACTION),
                    '说明 {"action":"answer","action":"read"}',
                    '说明 {"action":"answer","refs":[NaN]}',
                    '说明 {"action":"read","device":"local-primary","check":"services"}',
                    '说明 {"action":"search","query":"croc"}',
                    '说明 {"action":"answer","answer":"truncated'):
            planner,opener=adapter(raw)
            with self.assertRaises(ModelFailure):planner({'workflow_policy':{'mode':'knowledge'}})
            self.assertEqual(opener.open.call_count,1)

    def test_plain_learning_reply_is_answer_only_and_preserves_reference_checks(self):
        text='依据KB-129，角色回调在转换后执行，失败不能证明业务已接管。'
        planner,_=adapter(text)
        action,_=planner({'workflow_policy':{'mode':'knowledge','domain':'basics'}})
        self.assertEqual(action,{'action':'answer','answer':text,'refs':['KB-129'],'basis':'evidence'})
        self.assertTrue(safe_metrics(planner.last_metrics)['knowledge_text_answer'])
        for mode in ('diagnostic','material_analysis'):
            planner,_=adapter(text)
            with self.assertRaises(ModelFailure):planner({'workflow_policy':{'mode':mode}})
        planner,_=adapter('没有编号的实验室断言。')
        with self.assertRaises(ModelFailure):planner({'workflow_policy':{'mode':'knowledge','domain':'topology'},'sources':[{'id':'KB-005'}]})

    def test_duplicate_keys_nonfinite_values_and_mixed_text_are_not_repaired(self):
        for content,kind in [('{"action":"read","action":"answer"}','duplicate_key'),
            ('{"answer":"x","refs":[NaN]}','nonfinite_number'),
            ('[]','action_not_object'),('请执行 '+json.dumps(ACTION),'action_syntax'),
            (json.dumps(ACTION)+json.dumps(ACTION),'action_syntax'),
            ('{"action":"read","device":"local-backup"','action_syntax')]:
            with self.subTest(kind=kind),self.assertRaises(ActionJSONError) as error:
                decode_action(content)
            self.assertEqual(error.exception.kind,kind)

    def test_json_error_records_position_and_known_usage_without_private_content(self):
        planner,opener=adapter('{"answer":"PRIVATE_OUTPUT\nraw newline"}')
        with self.assertRaises(ModelFailure) as error:planner({})
        self.assertEqual(error.exception.code,'model_invalid_json')
        self.assertEqual(error.exception.usage,73)
        metrics=safe_metrics(planner.last_metrics)
        self.assertEqual(metrics['json_failure_kind'],'action_syntax')
        self.assertGreaterEqual(metrics['json_error_line'],1)
        self.assertNotIn('PRIVATE',json.dumps(metrics)+str(error.exception))
        self.assertEqual(opener.open.call_count,1)

    def test_outer_response_syntax_and_action_syntax_are_distinguishable(self):
        planner,opener=adapter()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'PRIVATE_BROKEN_RESPONSE'
        opener.open.return_value=response
        with self.assertRaises(ModelFailure) as error:planner({})
        self.assertIsNone(error.exception.usage)
        self.assertEqual(safe_metrics(planner.last_metrics)['json_failure_kind'],'response_syntax')
        self.assertNotIn('PRIVATE',json.dumps(safe_metrics(planner.last_metrics)))

    def test_missing_content_preserves_existing_invalid_response_category(self):
        planner,opener=adapter()
        opener.open.return_value=FixtureResponse({'usage':{'total_tokens':73},'choices':[{'message':{'content':None},'finish_reason':'stop'}]})
        with self.assertRaises(ModelFailure) as error:planner({})
        self.assertEqual(error.exception.code,'model_invalid_response')
        self.assertEqual(error.exception.usage,73)
        self.assertEqual(planner.last_metrics['json_failure_kind'],'content_not_text')

    def test_request_options_are_absent_by_default_and_cannot_override_limits(self):
        for options,expected in [({},{}),({'LLM_JSON_MODE':True},{'response_format':{'type':'json_object'}}),
            ({'LLM_JSON_MODE':True,'LLM_DISABLE_THINKING':True},{'response_format':{'type':'json_object'},'thinking':{'type':'disabled'}})]:
            planner,opener=adapter(**options);planner({'remaining_seconds':12})
            payload=json.loads(opener.open.call_args[0][0].data)
            self.assertEqual(payload['max_tokens'],4096)
            self.assertEqual(opener.open.call_args[1]['timeout'],12)
            for key in ('response_format','thinking'):
                self.assertEqual(payload.get(key),expected.get(key))
        for invalid in ('true',1,None):
            with self.assertRaises(ValueError):make_model(dict(CONFIG,LLM_JSON_MODE=invalid),Mock())

    def test_unsupported_option_does_not_retry_or_silently_remove_requested_controls(self):
        planner,opener=adapter(LLM_JSON_MODE=True)
        opener.open.side_effect=urllib.error.HTTPError('https://api.inferera.com/v1/chat/completions',400,'PRIVATE',None,None)
        with self.assertRaises(ModelFailure) as error:planner({})
        self.assertEqual(error.exception.code,'model_http_error')
        self.assertIsNone(error.exception.usage)
        self.assertEqual(opener.open.call_count,1)
        self.assertTrue(safe_metrics(planner.last_metrics)['request_json_mode'])

    def test_learning_thinking_policy_is_scoped_and_explicit_settings_win(self):
        cases=[({},'knowledge',True),({},'diagnostic',False),({},'material_analysis',False),
               ({'LLM_FAST_LEARNING':False},'knowledge',False),
               ({'LLM_DISABLE_THINKING':False},'knowledge',False),
               ({'LLM_DISABLE_THINKING':True},'diagnostic',True)]
        for options,mode,disabled in cases:
            with self.subTest(options=options,mode=mode):
                planner,opener=adapter(**options)
                planner({'workflow_policy':{'mode':mode,'domain':'general'}})
                payload=json.loads(opener.open.call_args[0][0].data)
                self.assertEqual(payload.get('thinking'),{'type':'disabled'} if disabled else None)
                self.assertEqual(payload.get('response_format'),{'type':'json_object'} if mode=='knowledge' else None)
                self.assertEqual(planner.last_metrics['request_thinking_disabled'],disabled)
                self.assertEqual(opener.open.call_count,1)
        for field in ('LLM_FAST_LEARNING','LLM_DISABLE_THINKING'):
            with self.assertRaises(ValueError):make_model(dict(CONFIG,**{field:1}),Mock())

    def test_answer_review_uses_previous_text_without_fast_learning_override(self):
        context={'workflow_policy':{'mode':'knowledge','domain':'general'},
                 'question':'你刚才那个例子是不是写反了？',
                 'history':[{'role':'assistant','content':'这段话需要核对，不能作为事实。'}]}
        for options,disabled in (({},False),({'LLM_DISABLE_THINKING':True},True)):
            planner,opener=adapter(**options);planner(context)
            payload=json.loads(opener.open.call_args[0][0].data)
            view=json.loads(payload['messages'][1]['content'])
            self.assertEqual(view['review_context']['previous_assistant_answer'],context['history'][0]['content'])
            self.assertEqual(payload.get('thinking'),{'type':'disabled'} if disabled else None)
            self.assertEqual(opener.open.call_count,1)

    def test_explicit_json_mode_false_keeps_learning_override_and_no_silent_retry(self):
        planner,opener=adapter(LLM_JSON_MODE=False)
        planner({'workflow_policy':{'mode':'knowledge'}})
        self.assertNotIn('response_format',json.loads(opener.open.call_args[0][0].data))
        self.assertEqual(opener.open.call_count,1)

    def test_finish_length_cannot_be_accepted_even_if_json_body_looks_complete(self):
        planner,opener=adapter(finish='length',LLM_JSON_MODE=True)
        with self.assertRaises(ModelFailure) as error:planner({})
        self.assertEqual(error.exception.code,'model_output_truncated')
        self.assertEqual(error.exception.usage,73)
        self.assertEqual(opener.open.call_count,1)

    def test_projection_removes_only_display_duplicates_and_preserves_authority_and_raw_state(self):
        context={'workflow_policy':{'mode':'diagnostic'},'sources':[],
            'observations':[{'id':'OBS-1','output':'PRIVATE_ORIGINAL_STATE','check':'interfaces','freshness':'current'}],
            'evidence_scope':{'kind':'device_read','checked':[{'id':'OBS-1'}],'note':'repeated narrative','unverified':['业务']},
            'tool_events':[{'action':'read','device':'local-backup','check':'interfaces','ok':True,'purpose':'duplicate description'}],
            'investigation_plan':{'next_checks':[{'device':'local-backup','check':'logs','purpose':'历史事件','reason_source':'host_check_contract'}],
                'remaining_reads':2,'attempted_failures':[],'stop_explanation':'有批准项'},
            'tool_catalog':{'local-backup':{'checks':{'logs':'固定日志'}}},'remaining_reads':2,
            'attempted_checks':[{'device':'local-backup','check':'interfaces','ok':True}],
            'correction_feedback':{'valid_refs':['OBS-1']},'observation_constraints':[{'id':'constraint'}]}
        original=copy.deepcopy(context);view=model_view(context)
        self.assertEqual(context,original)
        for key in ('observations','tool_catalog','remaining_reads','attempted_checks','correction_feedback','observation_constraints'):
            self.assertEqual(view[key],original[key])
        self.assertNotIn('checked',view['evidence_scope'])
        self.assertNotIn('purpose',view['tool_events'][0])
        self.assertEqual(view['investigation_plan']['next_checks'][0]['purpose'],'历史事件')
        self.assertLess(len(json.dumps(view)),len(json.dumps(original)))



class GroundedDiagnosticRequestTests(unittest.TestCase):
    def test_only_current_successful_status_enables_direct_inference_and_keeps_limits(self):
        from unittest.mock import Mock
        import json,socket
        from chat_model import make_model,ModelFailure
        cfg={'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test-placeholder','LLM_MODEL_ID':'test'}
        opener=Mock();opener.open.side_effect=socket.timeout()
        good={'id':'OBS-1','ok':True,'freshness':'current','check':'services'}
        cases=[([],{},False,65),([good],{},True,65),
               ([dict(good,ok=False)],{},False,65),([dict(good,freshness='stale')],{},False,65),
               ([dict(good,check='logs')],{},False,65),([good],{'LLM_DISABLE_THINKING':False},False,65)]
        for observations,options,disabled,expected in cases:
            planner=make_model(dict(cfg,**options),opener);before=opener.open.call_count
            with self.assertRaises(ModelFailure):
                planner({'workflow_policy':{'mode':'diagnostic'},'observations':observations,'remaining_seconds':170})
            self.assertEqual(opener.open.call_count,before+1)
            self.assertEqual(opener.open.call_args[1]['timeout'],expected)
            payload=json.loads(opener.open.call_args[0][0].data)
            self.assertEqual(payload.get('thinking'),{'type':'disabled'} if disabled else None)
            self.assertEqual(payload['max_tokens'],4096)
        with self.assertRaises(ModelFailure):
            make_model(cfg,opener)({'workflow_policy':{'mode':'diagnostic'},'observations':[good],'remaining_seconds':37})
        self.assertEqual(opener.open.call_args[1]['timeout'],37)

if __name__=='__main__':unittest.main()
