"""真实HTTP线程下检查只读进度、归属隔离、取消和重启恢复。"""
import json
import threading
import unittest
from contextlib import closing
from approval_store import ApprovalStore, TaskError
from workflow_test_support import RunningApp, current_probe_fixture
from ha_case_test_support import case_probe


class DiagnosticProgressTests(unittest.TestCase):
    def requested(self,client):
        task=client.diagnostic_chat('局端备机当前Croc服务和VIP')[1]['task']
        client.login();client.approve(task['id']);return task['id']

    def test_running_progress_is_owned_metadata_and_final_timing_is_retained(self):
        entered=threading.Event();release=threading.Event();results=[]
        def planner(context):
            if not context['observations']:
                entered.set();release.wait(3)
                return {'action':'read_overview','device':'local-backup'},1
            return {'action':'answer','answer':'按正常HA假设，无VIP且Croc停止符合局部预期；模式、对端与业务未核实。',
                    'refs':['OBS-1','OBS-2'],'basis':'evidence'},1
        with RunningApp(planner,case_probe('nonowner_stopped'),lookup=lambda q:[]) as client:
            task=self.requested(client)
            worker=threading.Thread(target=lambda:results.append(client.resume(task)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                running=client.call('/api/tasks/'+task)[1]
                self.assertEqual(running['progress']['stage'],'thinking')
                self.assertEqual(running['status'],'running');self.assertIsNone(running['result'])
                self.assertNotIn('Croc',json.dumps(running['progress']))
                self.assertEqual(client.call('/api/tasks/'+task)[1]['attempts'],0)
                other=client.new_browser();client.call('/api/session',browser=other)
                self.assertEqual(client.call('/api/tasks/'+task,browser=other)[0],404)
            finally:
                release.set();worker.join(4)
            final=results[0][1]
            self.assertEqual(final['status'],'completed');self.assertEqual(final['progress']['stage'],'completed')
            self.assertEqual(final['progress']['reads'],2)
            self.assertGreaterEqual(final['result']['timing']['total_seconds'],0)
            self.assertEqual(final['result']['ui_elapsed_seconds'],final['result']['timing']['total_seconds'])
            self.assertEqual(client.resume(task)[0],409)

    def test_cancel_during_read_freezes_progress_and_prevents_next_read(self):
        entered=threading.Event();release=threading.Event();reads=[];results=[]
        fixture=case_probe('nonowner_stopped')
        def probe(device,check):
            reads.append(check);entered.set();release.wait(3);return fixture(device,check)
        def planner(context): return {'action':'read_overview','device':'local-backup'},1
        with RunningApp(planner,probe,lookup=lambda q:[],prefetch_overview=True) as client:
            task=self.requested(client)
            worker=threading.Thread(target=lambda:results.append(client.resume(task)));worker.start()
            try:
                self.assertTrue(entered.wait(2))
                running=client.call('/api/tasks/'+task)[1]
                self.assertEqual(running['progress']['stage'],'reading')
                self.assertEqual(running['progress']['check'],'services')
                self.assertEqual(running['progress']['reads'],1)
                cancelled=client.call('/api/tasks/'+task+'/cancel',{})[1]
                self.assertEqual(cancelled['progress']['stage'],'cancelled')
                with closing(client.store.connect()) as db: owner=client.store.row(db,task)['owner']
                self.assertFalse(client.store.update_progress(task,owner,'analyzing',step=2))
            finally:
                release.set();worker.join(4)
            self.assertEqual(reads,['services'])
            self.assertEqual(results[0][1]['status'],'cancelled')
            self.assertEqual(results[0][1]['progress']['stage'],'cancelled')

    def test_progress_elapsed_freezes_and_interrupted_tasks_are_not_replayed(self):
        with RunningApp() as client:
            now=[1000];client.store.clock=lambda:now[0]
            task=self.requested(client)
            with closing(client.store.connect()) as db: owner=client.store.row(db,task)['owner']
            client.store.claim(task,owner)
            client.store.update_progress(task,owner,'reading',step=1,device='local-backup',check='services')
            now[0]+=5
            self.assertEqual(client.store.get(task,owner)['progress']['elapsed_seconds'],5)
            client.store.recover_interrupted()
            first=client.store.get(task,owner)
            self.assertEqual(first['status'],'incomplete');self.assertEqual(first['progress']['stage'],'interrupted')
            now[0]+=20
            self.assertEqual(client.store.get(task,owner)['progress']['elapsed_seconds'],5)
            with self.assertRaises(TaskError): client.store.claim(task,owner)

    def test_restart_recovers_saved_evidence_and_identifies_the_unrecorded_check(self):
        reads=[]

        def probe(device,check):
            reads.append(check)
            if check=='interfaces':
                raise KeyboardInterrupt()
            return current_probe_fixture(device,check)

        def planner(context):
            return {'action':'read_overview','device':'local-backup'},0

        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            task=self.requested(client)
            with closing(client.store.connect()) as db:
                owner=client.store.row(db,task)['owner']
            with self.assertRaises(KeyboardInterrupt):
                client.service.resume(owner,task)

            reopened=ApprovalStore(client.store.path)
            reopened.recover_interrupted()
            code,record=client.call('/api/conversations/default/investigations/'+task)
            self.assertEqual(code,200)
            self.assertEqual(record['status'],'incomplete')
            result=record['result']
            self.assertEqual([item['id'] for item in result['evidence']],['OBS-1'])
            item=result['evidence'][0]
            self.assertEqual(item['output'],'TEST FIXTURE: active')
            self.assertEqual(item['freshness'],'historical')
            self.assertEqual(result['evidence_scope']['current_reads'],0)
            self.assertIn('局端备机的接口、VIP与连接',result['answer'])
            self.assertIn('结果未知',result['answer'])

            task_view=client.call('/api/tasks/'+task)[1]
            self.assertEqual(task_view['execution_steps'],result['execution_steps'])
            self.assertEqual(task_view['attempts'],2)
            self.assertEqual(client.resume(task)[0],409)
            self.assertEqual(reads,['services','interfaces'])

    def test_progress_cannot_include_unapproved_targets_checks_or_arbitrary_text(self):
        with RunningApp() as client:
            task=self.requested(client)
            with closing(client.store.connect()) as db: owner=client.store.row(db,task)['owner']
            client.store.claim(task,owner)
            with self.assertRaises(TaskError): client.store.update_progress(task,owner,'reading',step=1,device='remote-primary',check='logs')
            with self.assertRaises(ValueError): client.store.update_progress(task,owner,'PRIVATE_LOG_CONTENT',step=1)
            with self.assertRaises(ValueError): client.store.update_progress(task,owner,'thinking',step=999)
            self.assertNotIn('PRIVATE',json.dumps(client.store.get(task,owner)['progress']))


if __name__=='__main__': unittest.main()
