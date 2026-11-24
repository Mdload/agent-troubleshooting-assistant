"""Execution record projection, lifecycle and ownership; no real model/device calls."""
import json,threading,unittest
from contextlib import closing
from unittest.mock import Mock
from execution_workflow import observation_step,check_request,observation_steps
from approval_store import TaskError
from workflow_test_support import RunningApp
from ha_case_test_support import case_probe
from device_tools import LOCAL_COMMANDS,REMOTE_CHECKS

class ExecutionProjectionTests(unittest.TestCase):
    def test_commands_come_from_actual_fixed_registry_not_model_text(self):
        for device,commands in [('local-backup',LOCAL_COMMANDS),('remote-primary',REMOTE_CHECKS)]:
            for check in commands:
                self.assertEqual(check_request(device,check)['command'],'\n'.join(commands[check]))
        for device,check in [('unregistered','services'),('local-backup','reboot')]:
            with self.assertRaises(ValueError):check_request(device,check)

    def test_sensitive_output_and_model_fields_are_not_projected(self):
        item=dict(case_probe('nonowner_stopped')('local-backup','services'),device='local-backup',check='services',
                  id='OBS-1',origin='device_read',freshness='current',reasoning='PRIVATE_THOUGHT',command='rm -rf /')
        item['output']+='\npassword=TOP_SECRET\n<img src=x onerror=alert(1)>'
        result=observation_step(item);text=json.dumps(result)
        self.assertNotIn('PRIVATE_THOUGHT',text);self.assertNotIn('TOP_SECRET',text)
        self.assertNotIn('rm -rf',text);self.assertIn('敏感信息已隐藏',result['output'])
        self.assertIn('Croc',result['summary']);self.assertNotIn('整机健康',result['summary'])
        self.assertIn('<img',result['output']) # UI must treat returned output as text, never HTML.

    def test_failed_check_and_stale_output_are_not_presented_as_live_success(self):
        item={'id':'OBS-1','device':'local-backup','check':'services','ok':False,
              'error':'timeout','output':'partial secret','origin':'device_read','freshness':'stale'}
        result=observation_step(item)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['output'],'')
        self.assertIn('超时',result['summary']);self.assertEqual(result['freshness'],'stale')
        self.assertEqual(observation_steps([item,item]),[result])


    def test_saved_technical_records_get_plain_words_without_rewriting_original_commands(self):
        from execution_workflow import readable_steps
        item=dict(case_probe('nonowner_stopped')('local-backup','services'),device='local-backup',check='services',
                  id='OBS-1',origin='device_read',freshness='historical')
        saved=observation_step(item);saved['command']='ORIGINAL_RECORDED_COMMAND';saved['summary']='OLD_TECHNICAL_SUMMARY'
        result=readable_steps([saved])[0]
        self.assertEqual(result['command'],'ORIGINAL_RECORDED_COMMAND')
        self.assertEqual(result['freshness'],'historical')
        self.assertIn('有没有运行',result['title']);self.assertIn('没有运行',result['summary'])
        self.assertNotIn('OLD_TECHNICAL',result['summary'])
        self.assertEqual(saved['summary'],'OLD_TECHNICAL_SUMMARY')

class ExecutionHttpTests(unittest.TestCase):
    def test_completed_steps_are_visible_while_model_waits_owned_and_historical(self):
        entered=threading.Event();release=threading.Event();results=[]
        def planner(context):
            if not context['observations']:return {'action':'read_overview','device':'local-backup'},0
            entered.set();release.wait(4)
            return {'action':'answer','answer':'模拟检查仅取得服务与接口状态，完整业务未知。',
                    'refs':['OBS-1','OBS-2'],'basis':'limited'},0
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('检查局端备机当前服务和VIP')[1]['task'];client.approve(task['id'])
            worker=threading.Thread(target=lambda:results.append(client.resume(task['id'])));worker.start()
            try:
                self.assertTrue(entered.wait(2))
                live=client.call('/api/tasks/'+task['id'])[1]
                self.assertEqual(live['status'],'running');self.assertEqual(len(live['execution_steps']),2)
                self.assertEqual(live['execution_steps'][0]['status'],'succeeded')
                self.assertIn('systemctl show',live['execution_steps'][0]['command'])
                self.assertNotIn('reasoning',json.dumps(live['execution_steps']))
                other=client.new_browser();client.call('/api/session',browser=other)
                self.assertEqual(client.call('/api/tasks/'+task['id'],browser=other)[0],404)
            finally:release.set();worker.join(5)
            final=results[0][1];self.assertEqual(final['status'],'completed')
            self.assertEqual(len(final['result']['execution_steps']),2)
            with closing(client.store.connect()) as db:owner=client.store.row(db,task['id'])['owner']
            import investigation_history
            old=investigation_history.get_record(client.store,owner,'default',task['id'])
            self.assertTrue(all(s['freshness']=='historical' for s in old['result']['execution_steps']))
            self.assertEqual(client.resume(task['id'])[0],409)

    def test_active_check_is_requested_not_success_and_cancel_preserves_actual_read_only(self):
        entered=threading.Event();release=threading.Event();reads=[];results=[]
        fixture=case_probe('nonowner_stopped')
        def probe(device,check):reads.append(check);entered.set();release.wait(4);return fixture(device,check)
        with RunningApp(lambda c:({'action':'read_overview','device':'local-backup'},0),probe,lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('检查局端备机当前服务和VIP')[1]['task'];client.approve(task['id'])
            worker=threading.Thread(target=lambda:results.append(client.resume(task['id'])));worker.start()
            try:
                self.assertTrue(entered.wait(2));live=client.call('/api/tasks/'+task['id'])[1]
                self.assertEqual(live['execution_steps'],[])
                self.assertEqual(live['active_check']['check'],'services')
                self.assertNotIn('status',live['active_check'])
                client.call('/api/tasks/'+task['id']+'/cancel',{})
            finally:release.set();worker.join(5)
            self.assertEqual(reads,['services']);self.assertEqual(results[0][1]['status'],'cancelled')
            self.assertEqual(len(results[0][1]['result']['execution_steps']),1)

    def test_unreserved_out_of_scope_or_arbitrary_observation_cannot_be_added(self):
        with RunningApp() as client:
            task=client.diagnostic_chat('检查局端备机当前服务和VIP')[1]['task'];client.approve(task['id'])
            with closing(client.store.connect()) as db:owner=client.store.row(db,task['id'])['owner']
            client.store.claim(task['id'],owner)
            item=dict(case_probe('nonowner_stopped')('local-backup','services'),id='OBS-1',device='local-backup',check='services')
            with self.assertRaises(TaskError):client.store.update_progress(task['id'],owner,'read_complete',device='local-backup',check='services',observation=item)
            self.assertEqual(client.store.get(task['id'],owner)['execution_steps'],[])

if __name__=='__main__':unittest.main()
