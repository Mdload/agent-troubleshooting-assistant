"""调查只读恢复的HTTP归属、来源和生命周期边界；替身，无外网/设备。"""
import json
import time
import unittest
from unittest.mock import Mock,patch
import app
from server.approval_store import DIAGNOSTIC_DATA_USE, SINGLE_APPROVAL
from tests.workflow_test_support import RunningApp, current_probe_fixture
from legacy.investigation_history import NOTE
from server.user_accounts import AccountStore

class InvestigationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch('server.user_accounts.ITERATIONS',1000);self.patch.start()
        self.planner=Mock();self.probe=Mock()
        self.client=RunningApp(self.planner,self.probe,lookup=Mock()).__enter__()
    def tearDown(self):self.client.__exit__(None,None,None);self.patch.stop()
    def call(self,path,data=None,**kwargs):
        return self.client.call(path,data,**kwargs)
    def register(self,name='alice',browser=None):
        return self.call('/api/account/register',{'username':name,'password':'offline-password-only-123','allow_storage':True},browser=browser)[1]['account']
    def task(self,conversation='archive',ended=True):
        self.client.enter_diagnostic(conversation)
        task=self.client.chat('局端备机当前Croc服务与VIP',conversation_id=conversation)[1]['task']
        if ended:
            with self.client.store.connect() as db:
                result={'status':'answered','answer':'原调查回答[OBS-1]，完整根因未核实。','task':{'approved_scope':{'forged':'no'}},
                    'sources':[{'id':'KB-1','title':'original reference'}],'refs':['OBS-1'],
                    'diagnostic_report':{'schema_version':1,'note':'original current note','facts':[]},
                    'evidence':[{'id':'OBS-1','ok':True,'device':'local-backup','check':'services','freshness':'current',
                        'origin':'synthetic','collected_at':'2026-10-01T00:00:00Z','output':'SYNTHETIC ORIGINAL'}]}
                db.execute("UPDATE workflow_tasks SET status='completed',result=? WHERE id=?",(json.dumps(result),task['id']))
        return task
    def base(self,c='archive'):return '/api/conversations/'+c+'/investigations'

    def test_ended_records_and_raw_sources_are_server_owned_historical_projection(self):
        task=self.task();code,listing=self.call(self.base())
        self.assertEqual(code,200);self.assertEqual(listing['records'][0]['id'],task['id'])
        self.assertNotIn('result',listing['records'][0]);self.assertTrue(listing['read_only'])
        code,record=self.call(self.base()+'/'+task['id']);self.assertEqual(code,200)
        self.assertEqual(record['note'],NOTE);self.assertNotIn('task',record['result'])
        evidence=record['result']['evidence'][0]
        self.assertEqual(evidence['freshness'],'historical');self.assertEqual(evidence['output'],'SYNTHETIC ORIGINAL')
        self.assertEqual(evidence['collected_at'],'2026-10-01T00:00:00Z')
        self.assertEqual(record['result']['evidence_scope']['current_reads'],0)
        self.assertEqual(self.client.store.get(task['id'])['result']['evidence'][0]['freshness'],'current')
        self.planner.assert_not_called();self.probe.assert_not_called()

    def test_foreign_owner_and_different_conversation_cannot_fetch_record(self):
        task=self.task();other=self.client.new_browser();self.call('/api/session',browser=other)
        self.assertEqual(self.call(self.base(),browser=other)[1]['records'],[])
        self.assertEqual(self.call(self.base()+'/'+task['id'],browser=other)[0],404)
        self.assertEqual(self.call(self.base('another')+'/'+task['id'])[0],404)

    def test_pending_records_hidden_and_cannot_be_restored_or_posted(self):
        task=self.task(ended=False)
        self.assertEqual(self.call(self.base())[1]['records'],[])
        self.assertEqual(self.call(self.base()+'/'+task['id'])[0],409)
        self.assertEqual(self.call(self.base()+'/'+task['id'],{'owner':'forged','resume':True})[0],404)
        self.assertEqual(self.client.store.get(task['id'])['status'],'pending')

    def test_account_second_browser_restores_same_record_and_logout_invalidates_access(self):
        self.register();task=self.task();other=self.client.new_browser()
        self.call('/api/account/login',{'username':'alice','password':'offline-password-only-123'},browser=other)
        self.assertEqual(self.call(self.base()+'/'+task['id'],browser=other)[0],200)
        self.call('/api/account/logout',{},browser=other)
        self.assertEqual(self.call(self.base()+'/'+task['id'],browser=other)[0],401)
        bob=self.client.new_browser();self.register('bob',bob)
        self.assertEqual(self.call(self.base()+'/'+task['id'],browser=bob)[0],404)

    def test_listing_bounded_and_contexts_do_not_mix(self):
        task=self.task()
        with self.client.store.connect() as db:
            owner=db.execute('SELECT owner FROM workflow_tasks WHERE id=?',(task['id'],)).fetchone()['owner']
            for i in range(24):db.execute('INSERT INTO workflow_tasks '
                '(id,owner,question,history,scope,status,expires,conversation,scene) VALUES (?,?,?,?,?,?,?,?,?)',
                (format(i,'032x'),owner,'archived '+str(i),'[]','{}','cancelled',0,'archive','ha_topology'))
        record=self.call(self.base())[1]
        self.assertEqual(len(record['records']),20);self.assertTrue(record['has_more'])
        self.assertEqual(self.call(self.base('another'))[1]['records'],[])

    def test_empty_results_remain_empty_and_restore_never_renews_approval(self):
        task=self.task(ended=False);self.client.login();self.client.approve(task['id'])
        self.assertEqual(self.call(self.base()+'/'+task['id'])[0],409)
        self.call('/api/tasks/'+task['id']+'/cancel',{})
        record=self.call(self.base()+'/'+task['id'])[1]
        self.assertIsNone(record['result']);self.assertEqual(record['reads'],0)
        self.assertEqual(self.call('/api/tasks/'+task['id']+'/resume',{'allow_context':True,'allow_live_data':True})[0],409)
        self.planner.assert_not_called();self.probe.assert_not_called()

    def test_reopening_chat_finds_pending_and_waiting_task_without_restarting_it(self):
        self.register()
        mode_url = '/api/conversations/live/mode'
        _, mode = self.client.enter_diagnostic('live')
        question = '局端备机的服务现在是什么状态？'
        payload = {'question':question, 'allow_context':True, 'history':[], 'chat_space':'ha',
            'conversation_id':'live', 'interaction_mode':mode['mode'],
            'interaction_revision':mode['revision'], 'approval_flow':SINGLE_APPROVAL}

        def planner(context):
            if 'diagnostic_options' in context:
                return {'action':'prepare_diagnostic','goal':'device_state',
                        'scope':{'local-backup':['services']}}, 0
            if not context['task_thread']:
                return {'action':'clarify','question':'你说的是业务进程还是 Keepalived？'}, 0
            self.assertEqual(context['task_thread'][-1]['content'], '只查 Keepalived。')
            if not context['observations']:
                return {'action':'read','device':'local-backup','check':'services'}, 0
            return {'action':'answer','answer':'已读取局端备机的服务快照，完整主备情况仍待核实。',
                    'basis':'limited','refs':['OBS-1']}, 0

        self.planner.side_effect = planner
        self.probe.side_effect = current_probe_fixture
        self.client.service.lookup = lambda _: []
        status, proposed = self.call('/api/chat', payload)
        self.assertEqual(status, 200, proposed)
        task = proposed['task']
        path = '/api/tasks/' + task['id']

        reopened = self.client.new_browser()
        self.assertEqual(self.call('/api/account/login', {'username':'alice',
            'password':'offline-password-only-123'}, browser=reopened)[0], 200)
        _, loaded = self.call(mode_url, browser=reopened)
        self.assertEqual([item['id'] for item in loaded['active_tasks']], [task['id']])
        self.assertFalse(loaded['active_tasks'][0]['data_use_approved'])
        self.assertEqual(self.call(path+'/start', {}, browser=reopened)[0], 409)
        self.assertNotIn('active_tasks', self.call('/api/conversations/other/mode')[1])
        guest = self.client.new_browser()
        self.call('/api/session', browser=guest)
        self.assertNotIn('active_tasks', self.call(mode_url, browser=guest)[1])
        self.probe.assert_not_called()

        self.assertEqual(self.call(path+'/decision', {'approved':True, 'data_use':DIAGNOSTIC_DATA_USE,
            'background':True})[0], 200)
        try:
            deadline = time.monotonic()+4
            while time.monotonic()<deadline:
                active = self.call(mode_url, browser=reopened)[1]['active_tasks'][0]
                if active.get('progress', {}).get('stage')=='awaiting_input':
                    break
                time.sleep(.02)
            else:
                self.fail('original worker did not ask for clarification')
            self.assertEqual(active['id'], task['id'])
            self.assertTrue(active['server_owned'])
            self.assertIn('Keepalived', active['progress']['question'])
            expiry = active['expires']
            self.assertEqual(self.call(mode_url, browser=reopened)[1]['active_tasks'][0]['expires'], expiry)
            self.assertEqual(self.call(path+'/start', {}, browser=reopened)[0], 409)
            self.assertEqual(self.planner.call_count, 2)
            self.probe.assert_not_called()
            self.assertEqual(self.call(path+'/input', {'message_id':'reopened-input','text':'只查 Keepalived。'},
                                       browser=reopened)[0], 200)
            deadline = time.monotonic()+4
            while time.monotonic()<deadline:
                done = self.call(path, browser=reopened)[1]
                if done['status'] in ('completed','incomplete'):
                    break
                time.sleep(.02)
            else:
                self.fail('restored worker did not continue')
            self.assertEqual(done['status'], 'completed', done['result'])
            self.assertEqual(done['expires'], expiry)
            self.assertEqual(done['attempts'], 1)
            self.probe.assert_called_once_with('local-backup','services')
        finally:
            if self.client.store.get(task['id'])['status'] in ('approved','running'):
                self.call(path+'/cancel', {}, browser=reopened)
        self.assertNotIn('active_tasks', self.call(mode_url, browser=reopened)[1])
        self.assertEqual(self.call(self.base('live')+'/'+task['id'], browser=reopened)[0], 200)

if __name__=='__main__':unittest.main()
