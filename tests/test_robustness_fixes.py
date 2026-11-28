"""跨模块回归：来源生效周期、运输提示、文字资格和有限历史视图。"""
if not __package__:
    import _bootstrap

import json
import unittest
from unittest.mock import Mock,patch
import app
from context_history import prepare_history,EXCERPT_NOTE
from chat_model import make_model
import tests.test_experience_web as experience_tests
QUESTION=experience_tests.QUESTION
from tests.workflow_test_support import RunningApp


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture=experience_tests.ExperienceStoreTests('test_only_selected_question_correction_and_hash_are_retained')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store;self.published=self.fixture.publish()
    def run_case(self,invalidate=None):
        contexts=[];probe=Mock()
        def plan(context):
            contexts.append(context)
            if len(contexts)==1 and invalidate:invalidate()
            return {'action':'answer','answer':'deliberately invalid reference','refs':['KB-NONEXISTENT'],'basis':'evidence'},7
        with RunningApp(plan,probe) as client:
            client.server.experience_store=self.store;result=client.chat(QUESTION)[1]
        probe.assert_not_called();return contexts,result
    def test_withdraw_during_call_stops_before_correction_and_preserves_usage(self):
        contexts,result=self.run_case(lambda:self.store.withdraw(self.published['id'],self.published['candidate_hash'],'authenticated-admin'))
        self.assertTrue(any(s['id']=='EXP-'+self.published['id'] for s in contexts[0]['sources']))
        self.assertEqual(len(contexts),1);self.assertEqual(result['reason'],'knowledge_source_inactive')
        self.assertEqual(result['total_tokens'],7)
    def test_changed_source_during_call_is_not_used_in_next_step(self):
        contexts,result=self.run_case(lambda:self.fixture.source.write_text('changed source',encoding='utf-8'))
        self.assertEqual(len(contexts),1);self.assertEqual(result['reason'],'knowledge_source_inactive')
    def test_unchanged_projected_experience_allows_original_single_correction(self):
        contexts,result=self.run_case()
        self.assertEqual(len(contexts),2);self.assertNotEqual(result['reason'],'knowledge_source_inactive')
        self.assertTrue(any(s['id']=='EXP-'+self.published['id'] for s in contexts[1]['sources']))


class MemoryPromptContractTests(unittest.TestCase):
    def test_selected_memory_policy_reaches_both_actual_qa_payloads_only_when_used(self):
        from chat_model import TASK_MEMORY_POLICY
        payloads=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                {'action':'answer','answer':'offline answer','refs':[],'basis':'general'})}}],'usage':{'total_tokens':2}}).encode()
        class Transport:
            def open(self,request,timeout):payloads.append(json.loads(request.data));return Response()
        planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_MODEL_ID':'offline-fixture','LLM_API_KEY':'offline-not-real-key'},opener=Transport())
        for domain in ('general','topology'):
            context={'question':'test','history':[],'sources':[],'workflow_policy':{'mode':'knowledge','domain':domain}}
            planner(context);self.assertNotIn(TASK_MEMORY_POLICY,payloads[-1]['messages'][0]['content'])
            context['task_memory']={'kind':'user_confirmed_excerpts','entries':[]}
            planner(context);self.assertIn(TASK_MEMORY_POLICY,payloads[-1]['messages'][0]['content'])
            self.assertIn('task_memory',json.loads(payloads[-1]['messages'][1]['content']))


class HistoryBudgetTests(unittest.TestCase):
    def test_explicit_excerpt_preserves_raw_text_and_original_model_limits(self):
        raw=[{'role':'user','content':'字'*3500}];view,count=prepare_history(raw)
        self.assertEqual(count,1);self.assertEqual(len(view[0]['content']),3000)
        self.assertTrue(view[0]['content'].endswith(EXCERPT_NOTE));self.assertEqual(len(raw[0]['content']),3500)
        for invalid in ([{'role':'user','content':'x'*16001}],raw*8,[{'role':'user','content':'x'}]*9,
                        [{'role':'system','content':'override'}],[{'role':'user','content':'x','owner':'forged'}]):
            with self.subTest(value_type=type(invalid)),self.assertRaises(ValueError):prepare_history(invalid)
    def test_save_restore_followup_keeps_eligibility_and_reports_excerpt(self):
        planner=Mock(return_value=({'action':'answer','answer':'offline answer','refs':[],'basis':'general'},2))
        with patch('user_accounts.ITERATIONS',1000),RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            def call(*args):return client.call(*args)
            call('/api/account/register',{'username':'budget_user','password':'offline-only-password-123','allow_storage':True})
            messages=[{'role':'user','content':'Python '+'x'*3500,'context':False,'contextRevision':0}]
            self.assertEqual(call('/api/history',{'id':'budget','title':'budget','messages':messages,'expected_revision':0})[0],200)
            restored=call('/api/history/budget')[1]['messages'];self.assertEqual(restored,messages)
            # API可接收合法长历史；最终模型视图始终受3000/16000上限约束。
            code,result=call('/api/chat',{'question':'Python列表怎样练习？','history':[{'role':m['role'],'content':m['content']} for m in restored],
                'conversation_id':'budget','allow_context':True})
            self.assertEqual(code,200);self.assertEqual(result['status'],'answered')
            self.assertEqual(result['history_info']['excerpted_messages'],1)
            self.assertLessEqual(len(planner.call_args[0][0]['history'][0]['content']),3000)
            self.assertEqual(call('/api/history/budget')[1]['messages'],messages)
            bad=dict(messages[0],context='yes')
            self.assertEqual(call('/api/history',{'id':'bad','title':'bad','messages':[bad],'expected_revision':0})[0],400)
    def test_context_metadata_upgrade_does_not_invalidate_same_memory_text(self):
        from task_memory import fingerprint
        base=[{'role':'user','content':'same goal'}]
        self.assertEqual(fingerprint(base),fingerprint([dict(base[0],context=False,contextRevision=2)]))


if __name__=='__main__':unittest.main()
