"""受限修复授权/状态/HTTP全链路；仅临时库和合成设备，不连接现场。"""
if not __package__:
    import _bootstrap

import datetime,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock
from repair_control import RepairManager,READ_SCOPE,TARGET,PEER,ACTION,pair_state,check_recommendation
from approval_store import TaskError
from tests.workflow_test_support import RunningApp
from diagnostic_core import AuditStore
from unified_chat import run
from device_tools import DEVICES

def evidence(target_active='inactive',target_vip=False,peer_vip=True):
    now=datetime.datetime.utcnow().isoformat()+'Z';items=[]
    for device in (TARGET,PEER):
        state=target_active if device==TARGET else 'active'
        services='Id=keepalived.service\nActiveState='+state+'\n\nId=croc2.0smr.service\nActiveState=inactive\n\nId=remote-ha-routes.service\nActiveState=inactive\n'
        vip=target_vip if device==TARGET else peer_vip
        for check,output in [('services',services),('interfaces','eth0 UP 198.51.100.28/24'+(' 198.51.100.29/24' if vip else ''))]:
            items.append({'id':'OBS-'+str(len(items)+1),'device':device,'check':check,'output':output,
                          'address':DEVICES[device][0],
                          'ok':True,'collected_at':now,'clock_source':'collector_utc','freshness':'current'})
    return items

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.owner='owner-a';self.task={'id':'1'*32,'scope':READ_SCOPE}
        self.policy={'enabled':True,'owner':self.owner,'task':self.task['id'],'target':TARGET,'lease':'2'*32,'expires':time.time()+600}
        self.policy_path=self.root/'policy.json';self.policy_path.write_text(json.dumps(self.policy))
        self.items=evidence();self.started=False
        def probe(device,check):return next(i for i in evidence('active' if self.started else 'inactive') if i['device']==device and i['check']==check)
        self.probe=Mock(side_effect=probe)
        def execute(request):self.started=True;return {'ok':True,'status':'dispatched','id':request['id']}
        self.execute=Mock(side_effect=execute)
        self.manager=RepairManager(self.root/'repairs.sqlite3',self.policy_path,self.probe,self.execute,sleep=lambda n:None)
        self.result={'evidence':self.items,'repair_recommendation':{'device':TARGET,'action':ACTION,'refs':[i['id'] for i in self.items]}}
    def tearDown(self):self.temp.cleanup()
    def plan(self):return self.manager.propose(self.owner,self.task,self.result)
    def test_disabled_wrong_owner_task_and_normal_standby_never_offer_action(self):
        self.assertIsNone(self.manager.catalog('other',self.task));self.policy_path.unlink();self.assertIsNone(self.manager.catalog(self.owner,self.task))
        with self.assertRaises(TaskError):check_recommendation(self.result['repair_recommendation'],{TARGET:{}},evidence('active'))
        self.execute.assert_not_called()
    def test_plan_requires_all_four_current_state_refs_and_no_command_fields(self):
        invalid=dict(self.result['repair_recommendation'],command='reboot')
        with self.assertRaises(TaskError):check_recommendation(invalid,{TARGET:{}},self.items)
        invalid=dict(self.result['repair_recommendation'],refs=['OBS-1'])
        with self.assertRaises(TaskError):check_recommendation(invalid,{TARGET:{}},self.items)
        self.items[0]['collected_at']='2000-01-01T00:00:00Z'
        with self.assertRaises(TaskError):self.plan()
    def test_approval_execute_verify_once_and_duplicate_is_refused(self):
        plan=self.plan()
        with self.assertRaises(TaskError):self.manager.execute(plan['id'],self.owner)
        self.manager.decide(plan['id'],self.owner,True);result=self.manager.execute(plan['id'],self.owner)
        self.assertEqual(result['result']['status'],'service_restored');self.assertFalse(result['result']['business_verified'])
        self.execute.assert_called_once();self.assertEqual(self.execute.call_args[0][0]['operation'],ACTION)
        self.assertEqual(self.probe.call_count,16)
        with self.assertRaises(TaskError):self.manager.execute(plan['id'],self.owner)
        self.execute.assert_called_once()
    def test_approval_cannot_be_taken_by_other_owner_or_after_lease_change(self):
        plan=self.plan()
        with self.assertRaises(TaskError):self.manager.decide(plan['id'],'other',True)
        self.manager.decide(plan['id'],self.owner,True)
        self.policy['lease']='3'*32;self.policy_path.write_text(json.dumps(self.policy))
        with self.assertRaises(TaskError):self.manager.execute(plan['id'],self.owner)
        self.execute.assert_not_called()
    def test_changed_role_between_plan_and_execution_refuses_write(self):
        plan=self.plan();self.manager.decide(plan['id'],self.owner,True)
        changed=evidence(target_vip=True,peer_vip=False)
        self.probe.side_effect=lambda d,c:next(i for i in changed if i['device']==d and i['check']==c)
        result=self.manager.execute(plan['id'],self.owner)
        self.assertEqual(result['state'],'refused');self.execute.assert_not_called()
    def test_natural_recovery_is_noop_and_not_counted_as_agent_write(self):
        plan=self.plan();self.manager.decide(plan['id'],self.owner,True);self.started=True
        result=self.manager.execute(plan['id'],self.owner)
        self.assertEqual(result['result']['status'],'no_action_needed');self.execute.assert_not_called()
    def test_cancel_during_preflight_prevents_dispatch(self):
        plan=self.plan();self.manager.decide(plan['id'],self.owner,True)
        original=self.probe.side_effect
        def cancel(d,c):self.manager.cancel(plan['id'],self.owner);return original(d,c)
        self.probe.side_effect=cancel
        result=self.manager.execute(plan['id'],self.owner)
        self.assertEqual(result['state'],'refused');self.execute.assert_not_called()
    def test_transport_unknown_is_not_retried_or_claimed_recovered(self):
        plan=self.plan();self.manager.decide(plan['id'],self.owner,True);self.execute.side_effect=TimeoutError()
        result=self.manager.execute(plan['id'],self.owner)
        self.assertEqual(result['state'],'unknown');self.assertFalse(result['result']['service_verified'])
        with self.assertRaises(TaskError):self.manager.execute(plan['id'],self.owner)
        self.execute.assert_called_once()
    def test_http_owner_csrf_scope_and_approval_are_enforced(self):
        with RunningApp(planner=Mock(),probe=self.probe) as client:
            # Create a real task through the existing endpoint to obtain its trusted owner.
            client.enter_diagnostic('repair-http')
            task=client.diagnostic_chat('远端主备当前状态',conversation_id='repair-http')[1]['task']
            with client.store.connect() as db:owner=db.execute('SELECT owner FROM workflow_tasks WHERE id=?',(task['id'],)).fetchone()[0]
            db.close()
            self.policy.update(owner=owner,task=task['id']);self.policy_path.write_text(json.dumps(self.policy));self.manager.policy_path=self.policy_path
            client.service.repairs=self.manager
            plan=self.manager.propose(owner,dict(task,scope=READ_SCOPE),self.result)
            self.assertEqual(client.call('/api/repairs/'+plan['id']+'/decision',{'approved':True},token=False)[0],403)
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/repairs/'+plan['id']+'/decision',{'approved':True},browser=other)[0],404)
            self.assertEqual(client.call('/api/repairs/'+plan['id']+'/execute',{})[0],409)
            self.assertEqual(client.call('/api/repairs/'+plan['id']+'/decision',{'approved':True})[0],200)
            result=client.call('/api/repairs/'+plan['id']+'/execute',{})
            self.assertEqual(result[0],200);self.assertEqual(result[1]['result']['status'],'service_restored')

class ModelRecommendationTests(unittest.TestCase):
    def test_model_reads_actual_fixture_pair_then_can_propose_but_not_execute(self):
        items=evidence();calls=[]
        def probe(d,c):calls.append((d,c));return next(i for i in items if i['device']==d and i['check']==c)
        def planner(context):
            observed={(i['device'],i['check']) for i in context['observations']}
            for device in (TARGET,PEER):
                if (device,'services') not in observed:return {'action':'read_overview','device':device},0
            refs=[i['id'] for i in context['observations']]
            return {'action':'answer','answer':'备机热备服务未运行，主机仍持有业务VIP；建议启动备机热备服务，尚未执行。',
                    'refs':refs,'basis':'evidence','repair':{'device':TARGET,'action':ACTION,'refs':refs}},0
        with tempfile.TemporaryDirectory() as temp:
            result=run('检查远端主备热备服务和VIP',[],planner,lambda q:[],probe,AuditStore(Path(temp)/'audit.db'),
                permitted_checks=READ_SCOPE,question_policy={'mode':'diagnostic','domain':'topology','require_current_evidence':True},repair_catalog={TARGET:{'action':ACTION}})
        self.assertEqual(result['status'],'answered',result);self.assertEqual(result['repair_recommendation']['action'],ACTION);self.assertEqual(len(calls),4)


if __name__ == "__main__":
    import unittest

    unittest.main()
