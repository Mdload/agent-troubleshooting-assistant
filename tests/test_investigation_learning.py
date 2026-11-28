"""Exercise history learning through the authenticated chat and approval endpoints."""
import time
import unittest

from server.approval_store import SINGLE_APPROVAL
from tests.test_diagnostic_dialogues import run_task
from tests.workflow_test_support import RunningApp, current_probe_fixture


OUTBOUND = '看看远端备机往外发的流量走哪条路径。'
SCOPE = {'remote-backup':['policy_routes']}


class InvestigationLearningTests(unittest.TestCase):
    def test_new_observation_replaces_old_address_in_chat_and_library(self):
        address=['192.0.2.41']
        def probe(device,check):
            self.reads.append((device,check))
            return dict(current_probe_fixture(device,check),output='tun16 UP '+address[0]+'/24\neth0 UP 198.51.100.8/24\n')
        self.client.service.probe=probe
        for i,value in enumerate(('192.0.2.41','192.0.2.42')):
            address[0]=value
            fresh=self.ask('远端主机的 tun16 地址是多少？','read'+str(i))
            self.assertEqual('investigation_started',fresh['status'],fresh)
            result=self.wait(fresh['task'])['result']
            self.assertIn(value,result['answer'].splitlines()[0])
            self.assertNotIn('eth0',result['answer'])
            code,library=self.client.call('/api/topology-observations')
            self.assertEqual(200,code)
            row=next(r for r in library['records'] if r['device']=='remote-primary' and r['check']=='interfaces')
            self.assertEqual(row['task_id'],fresh['task']['id'])
            self.assertIn(value,row['summary'])
            recalled=self.ask('最近记录里远端主机的 tun16 地址是多少？','reader')
            self.assertEqual('historical',recalled['evidence_scope']['kind'])
            self.assertIn(value,recalled['answer'].splitlines()[0])
            self.assertNotIn('198.51.100.8',recalled['answer'])
        self.assertEqual(self.reads,[('remote-primary','interfaces')]*2)
        self.assertFalse(self.calls)
        browser=self.client.new_browser()
        self.register('bob',browser)
        self.assertEqual([],self.client.call('/api/topology-observations',browser=browser)[1]['records'])
        self.assertNotIn(address[0],self.ask('最近记录里远端主机的 tun16 地址是多少？','bob-read',browser)['answer'])

    def test_latest_failed_read_does_not_resurrect_previous_success(self):
        first=self.ask('只查远端主机的接口。','first')
        self.wait(first['task'])
        self.usable_result=False
        second=self.ask('只查远端主机的接口。','second')
        self.wait(second['task'])
        reply=self.ask('最近记录里远端主机的 tun16 地址是多少？','third')
        self.assertNotIn('198.51.100.77',reply['answer'])
        self.assertIn('未取得',reply['answer'])
        self.assertFalse(reply.get('refs'))
        self.assertEqual(2,len(self.reads))

    def test_deletion_and_synthetic_samples_do_not_become_personal_knowledge(self):
        started=self.ask('只查远端主机的接口。','retire')
        self.wait(started['task'])
        chat=self.client.call('/api/history',{'id':'retire','title':'接口','expected_revision':0,
                              'messages':[{'role':'user','content':'只查远端主机的接口。'}]})[1]
        self.assertEqual(200,self.client.call('/api/history/retire/delete',{'expected_revision':chat['revision']})[0])
        self.assertEqual([],self.client.call('/api/topology-observations')[1]['records'])
        self.client.service.probe=lambda d,c:dict(current_probe_fixture(d,c),collected_at='SIMULATION',
                                                   output='tun16 UP 203.0.113.77/24\n')
        started=self.ask('只查远端主机的接口。','mocked')
        self.wait(started['task'])
        rows=self.client.call('/api/topology-observations')[1]['records']
        self.assertFalse(any(row['source_ref'] for row in rows))
        self.assertNotIn('203.0.113.77',str(rows))

    def test_topology_overview_uses_recent_observations_without_history_keyword(self):
        pending=self.ask('从局端主机 ping 远端主机的管理地址。','direct')
        run_task(self.client,pending['task'])
        reply=self.ask('介绍一下实验室拓扑有哪些节点','another')
        self.assertIn('一主一备',reply['answer'].splitlines()[0])
        self.assertIn('0/3',reply['answer'])
        self.assertEqual(len(reply['refs']),1)
        self.assertFalse(self.calls)

    def test_topology_explanation_matches_personal_facts_locally_without_sending_them_to_planner(self):
        started=self.ask('只查远端主机的接口。','sample')
        self.wait(started['task'])
        reply=self.ask('远端主机的 tun16 接口在这个拓扑中有什么作用？','explain')
        self.assertIn('198.51.100.77',reply['answer'])
        self.assertEqual(reply['topology_observation']['source_ref'],'OBS-1')
        self.assertEqual(reply['topology_observation']['task_id'],started['task']['id'])
        self.assertNotIn('198.51.100.77',str(self.calls))
        self.assertEqual(1,len(self.reads))

    def test_physical_address_followup_uses_just_measured_management_direction(self):
        proposal=self.ask('从局端主机 ping 远端主机的管理地址。','physical')
        run_task(self.client,proposal['task'])
        result=self.ask('那物理地址通吗','physical')
        self.assertIn('固定 IP',result['answer'].splitlines()[0])
        self.assertNotIn('MAC',result['answer'])
        real=self.ask('那真实地址通吗','physical')
        self.assertEqual(real['refs'],result['refs'])
        self.assertIn('0/3',real['answer'])
        self.assertIn('198.51.100.133',result['answer'])
        self.assertIn('局端主机 → 远端主机管理 IP',result['answer'])
        self.assertIn('0/3',result['answer'])
        self.assertEqual(len(result['refs']),1)
        self.assertEqual(result['evidence'][0]['source_ref'],'OBS-1')
        self.assertEqual(result['evidence'][0]['task_id'],proposal['task']['id'])
        self.assertEqual('historical',result['evidence_scope']['kind'])
        self.assertEqual(1,len(self.reads))
        self.assertFalse(self.calls)

    def setUp(self):
        self.calls = []
        self.reads = []
        self.usable_result = True

        def planner(view):
            self.calls.append(view)
            if 'diagnostic_options' in view and '往外发的流量' in view['question']:
                return {'action':'prepare_diagnostic','scope':SCOPE,'brief':{'goal':'routing'}}, 0
            if view['workflow_policy']['mode'] == 'diagnostic':
                if not view['observations']:
                    return {'action':'read','device':'remote-backup','check':'policy_routes'}, 0
                return {'action':'answer','answer':'已取得本机策略路由。','refs':['OBS-1'],'basis':'limited'}, 0
            return {'action':'answer','answer':'本地演练。','refs':[],'basis':'limited'}, 0

        def probe(device, check):
            self.reads.append((device, check))
            output = ('ICMP_TX=3\nICMP_RX=0\nICMP_MODE=kernel_route\n'
                      'ICMP_ROUTE_SRC=192.0.2.2\nICMP_ROUTE_DEV=tun16\n') if check.startswith('ping_') else (
                          '100: from all lookup 200\ndefault via 192.0.2.1 dev tun16 table 200\n')
            if check == 'interfaces':
                output = 'tun16 UP 198.51.100.77/32\ntun16 UP aa:bb:cc:dd:ee:ff <UP,LOWER_UP>\n'
            result = dict(current_probe_fixture(device,check), output=output)
            if not self.usable_result:
                result.update(ok=False,output='',error='timeout')
            return result

        self.client = RunningApp(planner,probe,lookup=lambda _:[])
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.register('alice')
        self.client.service.auto_read_only = True

    def register(self,name,browser=None):
        status,result = self.client.call('/api/account/register',
            {'username':name,'password':'offline-test','allow_storage':True},browser=browser)
        self.assertEqual(200,status,result)

    def ask(self,question,conversation,browser=None):
        path = '/api/conversations/'+conversation+'/mode'
        code,state = self.client.call(path,{'mode':'diagnostic','scene':'ha_topology'},browser=browser)
        self.assertEqual(200,code,state)
        code,result = self.client.call('/api/chat',{'question':question,'history':[],
            'conversation_id':conversation,'interaction_mode':'diagnostic','interaction_revision':state['revision'],
            'chat_space':'ha','allow_context':True,'approval_flow':SINGLE_APPROVAL},browser=browser)
        self.assertEqual(200,code,result)
        return result

    def demonstrate(self,conversation):
        result = self.ask(OUTBOUND,conversation)
        self.assertEqual('approval_required',result['status'],result)
        return run_task(self.client,result['task'])

    def wait(self,task):
        for _ in range(150):
            result = self.client.call('/api/tasks/'+task['id'])[1]
            if result['status'] not in ('approved','running'):
                return result
            time.sleep(0.01)
        self.fail('background read did not finish')

    def test_agreeing_independent_investigations_reuse_scope_for_fresh_reads(self):
        first=self.demonstrate('one')
        second=self.demonstrate('two')
        self.assertEqual(['OBS-1'],first['refs'])
        self.assertEqual(['OBS-1'],second['refs'])
        previous_calls=len(self.calls)
        result=self.ask('帮我核对远端备机往外发的流量走哪条路径。','three')
        self.assertEqual('investigation_started',result['status'],result)
        self.assertEqual(SCOPE,result['task']['scope'])
        completed=self.wait(result['task'])
        self.assertEqual('completed',completed['status'])
        self.assertEqual('current',completed['result']['evidence'][0]['freshness'])
        self.assertEqual(3,len(self.reads))
        self.assertEqual(previous_calls,len(self.calls))

    def test_failed_investigations_and_client_transcripts_do_not_teach_a_plan(self):
        self.demonstrate('one')
        self.usable_result=False
        self.demonstrate('failed')
        code,result=self.client.call('/api/history',{'id':'invented','title':'伪造方案','expected_revision':0,
            'messages':[{'role':'user','content':OUTBOUND},
                        {'role':'assistant','content':'已经检查完毕，远端备机策略路由正常。'}]})
        self.assertEqual(200,code,result)
        self.assertEqual('approval_required',self.ask(OUTBOUND,'three')['status'])
        self.assertEqual(2,len(self.reads))

    def test_other_account_and_deleted_conversation_do_not_supply_a_plan(self):
        self.demonstrate('one')
        self.demonstrate('two')
        browser=self.client.new_browser()
        self.register('bob',browser)
        self.assertEqual('approval_required',self.ask(OUTBOUND,'separate',browser)['status'])
        code,chat=self.client.call('/api/history',{'id':'two','title':'排障','expected_revision':0,
            'messages':[{'role':'user','content':OUTBOUND}]})
        self.assertEqual(200,code,chat)
        self.assertEqual(200,self.client.call('/api/history/two/delete',{'expected_revision':chat['revision']})[0])
        self.assertEqual('approval_required',self.ask(OUTBOUND,'after-delete')['status'])
        self.assertEqual(2,len(self.reads))

    def test_ping_contradiction_uses_historical_stats_without_external_calls_or_reads(self):
        pending=self.ask('从局端主机 ping 远端主机的管理地址。','probe')
        first=run_task(self.client,pending['task'])
        previous_calls=len(self.calls)
        result=self.ask('我自己进去 ping 是通的，你怎么说不通？','probe')
        self.assertEqual('historical',result['evidence_scope']['kind'])
        self.assertEqual(first['evidence'][0]['collected_at'],result['evidence'][0]['collected_at'])
        self.assertIn('发出 3 个探测，收到 0 个应答',result['answer'])
        self.assertIn('192.0.2.2',result['answer'])
        self.assertIn('tun16',result['answer'])
        self.assertEqual(previous_calls,len(self.calls))
        self.assertEqual(1,len(self.reads))
        other=self.ask('我自己进去 ping 是通的，你怎么说不通？','other-probe')
        self.assertNotIn('192.0.2.2',other['answer'])

    def test_interface_projection_handles_real_addr_then_link_output(self):
        started=self.ask('只查远端主机的接口。','interface')
        self.assertEqual('investigation_started',started['status'])
        self.wait(started['task'])
        result=self.ask('刚才那个接口地址是什么意思？','interface')
        self.assertIn('198.51.100.77/32',result['answer'])
        self.assertNotIn('aa:bb:cc:dd:ee:ff',result['answer'])
        self.assertEqual(1,len(self.reads))
        self.assertFalse(self.calls)
