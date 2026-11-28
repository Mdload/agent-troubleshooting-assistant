"""长会话与异常时序；检查状态/轨迹，模型及设备均为可控替身。"""
if not __package__:
    import _bootstrap

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from approval_store import ApprovalStore
from diagnostic_core import AuditStore
from chat_model import ModelFailure
from lab_workflow import WorkflowService
from tests.workflow_test_support import RunningApp
from tests.ha_case_test_support import case_probe
from unified_chat import run


def general(context):
    return {'action':'answer','answer':'一般原理说明；本轮未核验设备状态。','refs':[],'basis':'general'},1


class LifecycleRobustnessTests(unittest.TestCase):
    def test_cancellation_during_model_prevents_new_search_and_model_calls(self):
        state={};queries=[];calls=[]
        def lookup(query):queries.append(query);return []
        def planner(context):
            calls.append(context)
            if len(calls)==1:
                client.store.cancel(state['task'],state['owner'])
                return {'action':'search','query':'PRIVATE_AFTER_CANCEL_QUERY'},7
            return {'action':'answer','answer':'取消后仍在调用模型。','refs':['OBS-1','OBS-2'],'basis':'evidence'},9
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lookup,prefetch_overview=True) as client:
            task=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task'];state['task']=task['id']
            with client.store.connect() as db:state['owner']=client.store.row(db,task['id'])['owner']
            del db
            client.login();client.approve(task['id']);finished=client.resume(task['id'])[1]
            self.assertEqual(finished['status'],'cancelled')
            self.assertEqual(len(calls),1)
            self.assertNotIn('PRIVATE_AFTER_CANCEL_QUERY',queries)
            self.assertEqual(len(finished['result']['evidence']),2)
            self.assertEqual(finished['result']['total_tokens'],7)
            self.assertEqual(finished['result']['status'],'incomplete')

    def test_expiry_during_model_keeps_usage_and_does_not_correct_or_read(self):
        now=[1000];calls=[]
        def planner(context):
            calls.append(context);now[0]+=301
            return {'action':'answer','answer':'引用不存在的观测。','refs':['OBS-99'],'basis':'evidence'},5
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[],prefetch_overview=True) as client:
            client.store.clock=lambda:now[0]
            task=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task']
            client.login();client.approve(task['id']);finished=client.resume(task['id'])[1]
            self.assertEqual(finished['status'],'expired')
            self.assertEqual(len(calls),1)
            self.assertEqual(finished['result']['total_tokens'],5)
            self.assertEqual(finished['attempts'],2)


class LongConversationRobustnessTests(unittest.TestCase):
    def test_basic_followup_keeps_retrieval_topic_without_growing_question(self):
        planner=Mock(side_effect=general)
        with tempfile.TemporaryDirectory() as directory:
            store=ApprovalStore(Path(directory)/'workflow.db')
            service=WorkflowService(store,planner,Mock(),lookup=lambda q:[])
            service.ask('owner','SSH是什么',[],True,'chat')
            for index in range(20):
                result=service.ask('owner','为什么呢',[],True,'chat')
                self.assertEqual(result['status'],'answered')
                context=planner.call_args[0][0]
                self.assertIn('SSH是什么',context['workflow_policy']['retrieval_query'])
                self.assertLessEqual(len(context['question']),1000)
                self.assertEqual(context['tool_catalog'],{})

    def test_general_followup_does_not_override_a_new_live_device_question(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            client.chat('Python列表是什么')
            planner.reset_mock()
            result=client.chat('那这台设备当前状态怎么样')[1]
            self.assertEqual(result['status'],'current_unverified')
            planner.assert_not_called()
            self.assertEqual(client.store.list(),[])

    def test_oversized_history_is_rejected_before_any_model_or_device_call(self):
        planner=Mock(side_effect=general);probe=Mock()
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            for history in ([{'role':'user','content':'Python'}]*9,
                            [{'role':'user','content':'x'*3000}]*6):
                self.assertEqual(client.chat('Python列表是什么',history=history)[0],400)
            planner.assert_not_called();probe.assert_not_called()
            self.assertEqual(client.store.list(),[])

    def test_generic_followup_without_repeating_python_is_still_a_conversation(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            first=client.chat('Python列表是什么')[1]
            history=[{'role':'user','content':'Python列表是什么'},
                     {'role':'assistant','content':first['answer']}]
            second=client.chat('那最后一个元素呢',history=history)[1]
            self.assertEqual(second['status'],'answered')
            self.assertEqual(planner.call_args[0][0]['history'],history)
            self.assertEqual(planner.call_args[0][0]['tool_catalog'],{})
            self.assertEqual(client.store.list(),[])
    def test_many_followups_do_not_exceed_input_limit_or_reuse_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ApprovalStore(Path(directory)/'workflow.db')
            planner=Mock(side_effect=general);probe=Mock()
            service=WorkflowService(store,planner,probe,lookup=lambda q:[])
            owner='long-conversation';conversation='chat';history=[]
            # 同一对话超过前端窗口：换主题后仅带最近四轮，服务端仍保留最近用户话题。
            for index in range(100):
                question='Python列表是什么' if index%10==0 else '再解释一下列表'
                result=service.ask(owner,question,history,True,conversation,'qa',0)
                self.assertEqual(result['status'],'answered')
                history=(history+[{'role':'user','content':question},{'role':'assistant','content':result['answer']}])[-8:]
                self.assertLessEqual(len(planner.call_args[0][0]['history']),8)
            state=store.set_interaction(owner,conversation,'diagnostic','ha_topology')
            request=service.ask(owner,'局端备机当前状态',history,True,conversation,'diagnostic',state['revision'])
            first=request['task'];store.decide(first['id'],True,'local-admin')
            next_task=service.ask(owner,'那主机呢',[],True,conversation,'diagnostic',state['revision'])['task']
            self.assertEqual(next_task['status'],'pending');self.assertEqual(set(next_task['scope']),{'local-primary'})
            self.assertNotEqual(first['id'],next_task['id']);probe.assert_not_called()

    def test_new_generic_topic_does_not_send_old_device_findings_to_model(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            old=[{'role':'user','content':'局端备机当前Croc业务不通'},
                 {'role':'assistant','content':'PRIVATE_OLD_DEVICE_LOG_MARKER；设备当前正常。'}]
            response=client.chat('Python列表是什么',history=old)[1]
            self.assertEqual(response['status'],'answered')
            context=planner.call_args[0][0]
            self.assertNotIn('PRIVATE_OLD_DEVICE_LOG_MARKER',json.dumps(context))
            self.assertEqual(context['tool_catalog'],{})

    def test_explicit_excluding_devices_does_not_make_a_python_question_a_lab_topic(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            old=[{'role':'user','content':'局端备机当前Croc业务不通'},
                 {'role':'assistant','content':'SYNTHETIC_PRIVATE_FIELD_MARKER'}]
            response=client.chat('Python列表和元组有什么区别？只回答原理，不讨论设备。',history=old)[1]
            self.assertEqual(response['route'],'general')
            self.assertNotIn('SYNTHETIC_PRIVATE_FIELD_MARKER',json.dumps(planner.call_args[0][0]))

    def test_general_followup_cannot_reintroduce_old_field_history(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            history=[{'role':'user','content':'局端备机当前Croc业务不通'},
                     {'role':'assistant','content':'PRIVATE_OLD_FIELD_STATE'},
                     {'role':'user','content':'Python列表是什么'},
                     {'role':'assistant','content':'列表可以增加删除元素。'}]
            client.chat('那列表怎么删除元素',history=history)
            clean=planner.call_args[0][0]['history']
            self.assertEqual(clean,history[-2:])

    def test_valid_long_or_empty_history_messages_do_not_break_domain_filter(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            for text in ('','Linux '+('x'*2994)):
                code,response=client.chat('Python列表是什么',history=[{'role':'user','content':text}])
                self.assertEqual(code,200);self.assertEqual(response['status'],'answered')


class ToolSequenceRobustnessTests(unittest.TestCase):
    def test_seeded_fault_and_adversarial_sequences_keep_scope_budget_and_evidence(self):
        for seed in range(40):
            with self.subTest(seed=seed),tempfile.TemporaryDirectory() as directory:
                rng=random.Random(seed);actual=[];known=[];unknown=[]
                variant='read_failure' if seed%7==0 else ('owner_stopped' if seed%2 else 'nonowner_stopped')
                fixture=case_probe(variant)
                def probe(device,check):
                    actual.append((device,check));return fixture(device,check)
                def planner(context):
                    choice=rng.randrange(9)
                    if choice==0:
                        unknown.append(1);raise ModelFailure('model_timeout')
                    known.append(3)
                    if choice==1:return {'action':'read','device':'PRIVATE_UNREGISTERED','check':'services'},3
                    if choice==2:return {'action':'read','device':'local-backup','check':'services','command':'PRIVATE_UNREGISTERED'},3
                    if choice==3:return {'action':'read','device':'local-backup','check':'logs'},3
                    if choice==4:return {'action':'read','device':'local-backup','check':'routes'},3
                    if choice==5:return {'action':'search','query':'Croc服务状态'},3
                    return {'action':'answer','answer':'本轮只取得局部状态，模式和业务根因仍未核实。',
                            'refs':['OBS-99'] if choice==6 else ['OBS-1','OBS-2'],'basis':'evidence'},3
                audit=AuditStore(Path(directory)/'events.db')
                result=run('局端备机当前Croc业务不通',[],planner,lambda query:[],probe,audit,
                           permitted_checks={'local-backup':['services','interfaces','logs','routes']},
                           question_policy={'domain':'topology','mode':'diagnostic','scene':'ha_topology'},initial_overview=True)
                self.assertLessEqual(len(actual),4);self.assertEqual(len(actual),len(set(actual)))
                self.assertTrue(all(d=='local-backup' and c in ('services','interfaces','logs','routes') for d,c in actual))
                self.assertLessEqual(len(result['model_trace']),6)
                self.assertLessEqual(sum(e['action']=='correction' for e in result['events']),1)
                self.assertEqual(result['total_tokens'],sum(known));self.assertEqual(result['total_tokens_complete'],not unknown)
                self.assertNotIn('PRIVATE_UNREGISTERED',json.dumps(audit.read(result['task'])))
                valid={o['id'] for o in result['evidence'] if o['ok']}
                self.assertTrue(set(result['refs'])<=valid)
                if variant=='read_failure':
                    self.assertEqual(result['reason'],'required_state_unavailable');self.assertEqual(known,[])
                if result['status']=='answered':
                    self.assertEqual(result['basis'],'evidence');self.assertTrue({'OBS-1','OBS-2'}<=set(result['refs']))


if __name__=='__main__':unittest.main()
