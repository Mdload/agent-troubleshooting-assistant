"""修复申请、管理员许可与执行后核查；临时库和固定合成证据。"""
import json,tempfile,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from approval_store import TaskError
from repair_control import RepairManager,READ_SCOPE,TARGET,ACTION,service_assessment
from test_repair_control import evidence
from workflow_test_support import RunningApp,TEST_PASSWORD

class PermissionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.now=time.time()
  self.tasks={'1'*32:{'id':'1'*32,'question':'远端主备当前状态','scope':READ_SCOPE,'status':'pending','expires':self.now+900}}
  self.inspect=Mock(return_value={'ok':True,'id':'0'*32,'operation':'inspect_keepalived','permit':{'ready':True,'unused':True,'lease':'a'*32,'remaining_seconds':300}})
  self.manager=RepairManager(self.root/'repair.db',self.root/'absent.json',Mock(),Mock(),clock=lambda:self.now,inspector=self.inspect,
     task_loader=lambda ident,owner:self.tasks[ident] if owner=='alice' else (_ for _ in ()).throw(TaskError('not_found')))
  self.permissions=self.manager.permissions
 def tearDown(self):self.temp.cleanup()
 def request(self,task='1'*32):return self.permissions.request('alice',task)
 def test_request_binds_owner_task_scope_and_never_enables_model_write_catalog(self):
  item=self.request();self.assertEqual(item,self.request());self.assertEqual(item['max_writes'],1)
  self.assertIsNone(self.manager.catalog('alice',self.tasks['1'*32]));self.inspect.assert_not_called()
  self.assertNotIn('lease',item)
  with self.assertRaises(TaskError):self.permissions.get(item['id'],'bob')
 def test_started_task_or_incomplete_scope_cannot_request_permission(self):
  self.tasks['1'*32]['status']='running'
  with self.assertRaisesRegex(TaskError,'repair_task_not_pending'):self.request()
  self.tasks['1'*32]['status']='pending';self.tasks['1'*32]['scope']={TARGET:['services']}
  with self.assertRaises(TaskError):self.request()
 def test_grant_uses_device_expiry_and_revocation_blocks_catalog_even_with_legacy_file(self):
  item=self.request();allowed=self.permissions.grant(item['id'],'admin',15)
  self.assertLessEqual(allowed['expires'],self.now+295);self.assertIsNotNone(self.manager.catalog('alice',self.tasks['1'*32]))
  self.assertNotIn('lease',allowed);self.permissions.revoke(item['id'],'admin')
  self.manager.policy_path.write_text(json.dumps({'enabled':True,'owner':'alice','task':'1'*32,'target':TARGET,'lease':'b'*32,'expires':self.now+100}))
  self.assertIsNone(self.manager.policy('alice','1'*32))
 def test_closed_device_permission_and_nonstandard_duration_are_refused(self):
  item=self.request();self.inspect.return_value={'ok':True,'id':'0'*32,'operation':'inspect_keepalived','permit':{'ready':False}}
  with self.assertRaisesRegex(TaskError,'repair_device_not_armed'):self.permissions.grant(item['id'],'admin',10)
  for minutes in (True,0,1,30):
   with self.assertRaises(ValueError):self.permissions.grant(item['id'],'admin',minutes)
  self.assertEqual(self.permissions.get(item['id'])['state'],'pending')
 def test_admin_session_recheck_after_inspection_prevents_grant(self):
  item=self.request();checks=[0]
  def guard():
   checks[0]+=1
   if checks[0]>1:raise TaskError('admin_required')
  with self.assertRaisesRegex(TaskError,'admin_required'):self.permissions.grant(item['id'],'admin',10,guard)
  self.assertEqual(self.permissions.get(item['id'])['state'],'pending')
 def test_one_device_lease_cannot_authorize_two_concurrent_windows(self):
  self.tasks['2'*32]=dict(self.tasks['1'*32],id='2'*32);items=[self.request(),self.request('2'*32)];barrier=threading.Barrier(2)
  original=self.inspect.return_value
  self.inspect.side_effect=lambda request:(barrier.wait(timeout=3),original)[1]
  def approve(item):
   try:self.permissions.grant(item['id'],'admin',10);return 'active'
   except TaskError:return 'refused'
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(approve,items))
  self.assertCountEqual(results,['active','refused'])
 def test_expiry_and_owner_logout_remove_permission(self):
  item=self.request();self.permissions.grant(item['id'],'admin',5);self.now+=300
  self.assertIsNone(self.manager.policy('alice','1'*32));self.assertEqual(self.permissions.list('alice')[0]['state'],'expired')
  self.now-=300;self.permissions.revoke_owner('alice');self.assertIsNone(self.manager.policy('alice','1'*32))
 def test_cancelled_task_and_wrong_inspection_identity_cannot_leave_active_permission(self):
  item=self.request();self.inspect.return_value={'ok':True,'id':'wrong','operation':'inspect_keepalived','permit':{'ready':True,'unused':True,'lease':'a'*32,'remaining_seconds':300}}
  with self.assertRaises(TaskError):self.permissions.grant(item['id'],'admin',5)
  self.inspect.return_value.update(id='0'*32);self.permissions.grant(item['id'],'admin',5)
  self.tasks['1'*32]['status']='cancelled';self.assertIsNone(self.manager.policy('alice','1'*32))

class ReconciliationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.healthy=False
  self.policy={'enabled':True,'owner':'alice','task':'1'*32,'target':TARGET,'lease':'a'*32,'expires':time.time()+500}
  (self.root/'policy.json').write_text(json.dumps(self.policy))
  self.executor=Mock(side_effect=TimeoutError());self.inspector=Mock()
  self.manager=RepairManager(self.root/'repair.db',self.root/'policy.json',lambda d,c:next(i for i in evidence('active' if self.healthy else 'inactive') if i['device']==d and i['check']==c),self.executor,sleep=lambda _:None,inspector=self.inspector)
  observed=evidence();self.plan=self.manager.propose('alice',{'id':'1'*32,'scope':READ_SCOPE},{'evidence':observed,'repair_recommendation':{'device':TARGET,'action':ACTION,'refs':[i['id'] for i in observed]}})
  self.manager.decide(self.plan['id'],'alice',True);self.manager.execute(self.plan['id'],'alice')
 def tearDown(self):self.temp.cleanup()
 def inspect(self,record):self.inspector.return_value={'ok':True,'id':self.plan['id'],'operation':'inspect_keepalived','record':record,'record_current_boot':True}
 def test_unknown_after_timeout_can_be_confirmed_without_another_start_or_active_window(self):
  self.healthy=True;self.inspect({'ok':True,'id':self.plan['id'],'status':'dispatched'});(self.root/'policy.json').unlink()
  result=self.manager.reconcile(self.plan['id'],'alice')
  self.assertEqual(result['result']['status'],'service_restored_confirmed');self.assertTrue(result['result']['execution_confirmed'])
  self.assertEqual(result['result']['verification_samples'],3);self.executor.assert_called_once()
 def test_healthy_state_with_missing_or_inflight_receipt_is_not_claimed_as_agent_success(self):
  self.healthy=True
  for receipt in (None,{'ok':False,'id':self.plan['id'],'status':'dispatching'}):
   self.inspect(receipt);result=self.manager.reconcile(self.plan['id'],'alice')
   self.assertEqual(result['result']['status'],'state_verified_execution_unconfirmed');self.assertFalse(result['result']['execution_confirmed'])
  self.executor.assert_called_once()
 def test_failed_inspection_never_reexecutes_and_other_owner_cannot_read(self):
  self.inspector.return_value={'ok':False}
  with self.assertRaises(TaskError):self.manager.reconcile(self.plan['id'],'bob')
  self.assertEqual(self.manager.reconcile(self.plan['id'],'alice')['state'],'unknown');self.executor.assert_called_once()
 def test_crashed_running_claim_is_unknown_and_can_be_checked_not_replayed(self):
  with self.manager.db() as db:db.execute("UPDATE repair_plans SET state='running' WHERE id=?",(self.plan['id'],))
  self.manager.recover_interrupted();self.assertEqual(self.manager.get(self.plan['id'],'alice')['state'],'unknown')
  with self.assertRaises(TaskError):self.manager.execute(self.plan['id'],'alice')
  self.executor.assert_called_once()
 def test_stopped_service_remains_unverified_even_with_successful_receipt(self):
  self.inspect({'ok':True,'id':self.plan['id'],'status':'dispatched'})
  result=self.manager.reconcile(self.plan['id'],'alice');self.assertFalse(result['result']['service_verified']);self.assertFalse(result['result']['business_verified'])
 def test_old_boot_receipt_does_not_prove_current_recovery_was_this_action(self):
  self.healthy=True;self.inspect({'ok':True,'id':self.plan['id'],'status':'dispatched'});self.inspector.return_value['record_current_boot']=False
  self.assertEqual(self.manager.reconcile(self.plan['id'],'alice')['result']['status'],'state_verified_execution_unconfirmed')

class PermissionHttpTests(unittest.TestCase):
 def test_real_http_requires_account_owner_admin_and_csrf_for_distinct_steps(self):
  with RunningApp(planner=Mock(),probe=Mock()) as client:
   client.call('/api/account/register',{'username':'alice','password':'test-password','allow_storage':True})
   task=client.diagnostic_chat('远端主备当前状态')[1]['task'];manager=client.service.repairs
   manager.inspector=Mock(return_value={'ok':True,'id':'0'*32,'operation':'inspect_keepalived','permit':{'ready':True,'unused':True,'lease':'a'*32,'remaining_seconds':500}})
   self.assertEqual(client.call('/api/repair-windows',{'task':task['id'],'command':'reboot'})[0],400)
   code,request=client.call('/api/repair-windows',{'task':task['id']});self.assertEqual(code,200,request)
   route='/api/admin/repair-windows/'+request['id']+'/decision';payload={'approved':True,'duration_minutes':10}
   self.assertEqual(client.call(route,payload)[0],401);manager.inspector.assert_not_called()
   self.assertEqual(client.call('/api/admin/login',{'password':TEST_PASSWORD})[0],200)
   self.assertEqual(client.call(route,payload,token=False)[0],403)
   self.assertEqual(client.call(route,payload)[0],200)
   self.assertEqual(client.call('/api/repair-windows')[1]['windows'][0]['state'],'active')
   other=client.new_browser();client.call('/api/session',browser=other)
   client.call('/api/account/register',{'username':'bob','password':'test-password','allow_storage':True},browser=other)
   self.assertEqual(client.call('/api/repair-windows/'+request['id']+'/revoke',{},browser=other)[0],404)
   self.assertEqual(client.call('/api/repair-windows/'+request['id']+'/revoke',{})[0],200)
   client.service.planner.assert_not_called();client.service.probe.assert_not_called()

class AssessmentTests(unittest.TestCase):
 def test_standby_business_stop_and_keepalived_stop_are_distinct_findings(self):
  self.assertEqual(service_assessment(evidence('active'))['finding'],'normal_standby')
  self.assertEqual(service_assessment(evidence())['finding'],'standby_keepalived_stopped')
  self.assertEqual(service_assessment(evidence(target_vip=True,peer_vip=False))['finding'],'conditions_not_met')
  self.assertIsNone(service_assessment([]))

if __name__=='__main__':unittest.main()
