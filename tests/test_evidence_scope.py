"""证据性质由宿主保留，部分读数不能标为完整HA/业务验收。"""
if not __package__:
    import _bootstrap

import json,tempfile,threading,unittest,urllib.request
from pathlib import Path
from unittest.mock import Mock,patch
from diagnostic_core import AuditStore
from evidence_scope import mark_observation,build_scope
from unified_chat import run
from tests.workflow_test_support import RunningApp

def answer(text='服务处于active，其他状态仍未验证',refs=None):
    return {'action':'answer','answer':text,'refs':refs or ['OBS-1'],'basis':'evidence'},2

class EvidenceScopeTests(unittest.TestCase):
    def execute(self,probe,actions):
        with tempfile.TemporaryDirectory() as tmp:
            self.planner=Mock(side_effect=actions)
            audit=AuditStore(Path(tmp)/'audit.db')
            result=run('检查局端备机',[],self.planner,Mock(return_value=[]),Mock(return_value=probe),audit)
            self.events=audit.read(result['task'])
            return result

    def test_synthetic_mark_survives_model_omission(self):
        result=self.execute({'ok':True,'collected_at':'SIMULATION','output':'[合成测试快照，不是真实设备状态]\nactive'},
                            [({'action':'read','device':'local-backup','check':'services'},1),answer()])
        self.assertEqual(result['evidence_scope']['kind'],'synthetic')
        self.assertTrue(result['answer'].startswith('以下为合成状态回放'))
        self.assertIn('不代表真实设备现状',result['answer'])
        self.assertEqual(self.planner.call_args_list[1][0][0]['evidence_scope']['kind'],'synthetic')
        self.assertTrue(any(body.get('origin')=='synthetic' for step,kind,body in self.events if kind=='observation'))

    def test_trusted_device_read_still_does_not_prove_whole_health(self):
        result=self.execute({'ok':True,'address':'192.0.2.14','collected_at':'2026-09-30T14:00:00.000001Z','output':'active'},
                            [({'action':'read','device':'local-backup','check':'services'},1),answer()])
        scope=result['evidence_scope']
        self.assertEqual(scope['kind'],'device_read')
        self.assertEqual(scope['unverified'],['完整HA健康','端到端业务可用'])
        self.assertEqual(scope['checked'][0]['check'],'services')
        self.assertEqual(scope['successful_reads'],1)

    def test_self_reported_origin_does_not_promote_unconfirmed_data(self):
        result=self.execute({'ok':True,'origin':'device_read','output':'active','collected_at':'PRIVATE NOT A TIME'},
                            [({'action':'read','device':'local-backup','check':'services'},1),answer()])
        self.assertEqual(result['evidence'][0]['origin'],'unconfirmed')
        self.assertIn('不能视为',result['evidence_scope']['note'])

    def test_synthetic_marker_overrides_live_looking_fields(self):
        item=mark_observation({'ok':True,'address':'192.0.2.14','collected_at':'2026-09-30T14:00:00Z',
                               'output':'[合成测试快照，不是真实设备状态]\nactive'},'local-backup','services')
        self.assertEqual(item['origin'],'synthetic')

    def test_failed_reads_not_listed_as_successful_scope(self):
        item=mark_observation({'ok':False,'error':'timeout'},'local-backup','interfaces')
        item.update(device='local-backup',check='interfaces',id='OBS-1')
        scope=build_scope([item])
        self.assertEqual(scope['kind'],'failed')
        self.assertEqual(scope['successful_reads'],0)
        self.assertEqual(scope['checked'],[])

    def test_prefix_cannot_overflow_history_limit(self):
        result=self.execute({'ok':True,'collected_at':'SIMULATION','output':'active'},
                            [({'action':'read','device':'local-backup','check':'services'},1),answer('x'*3000),answer('简短说明')])
        self.assertEqual(result['status'],'answered')
        self.assertLessEqual(len(result['answer']),3000)
        self.assertEqual(self.planner.call_count,3)
        self.assertEqual(result['evidence_scope']['successful_reads'],1)

    def test_http_keeps_scope_outside_answer_text(self):
        planner=Mock(side_effect=[({'action':'read','device':'local-backup','check':'services'},1),answer()])
        probe=Mock(return_value={'ok':True,'collected_at':'SIMULATION','output':'active'})
        with RunningApp(planner,probe,lookup=Mock(return_value=[])) as client:
            task=client.diagnostic_chat('检查局端备机')[1]['task'];client.login();client.approve(task['id'])
            result=client.resume(task['id'])[1]['result']
            self.assertEqual(result['evidence_scope']['kind'],'synthetic')
            self.assertIn('完整HA健康',result['evidence_scope']['unverified'])

if __name__=='__main__':unittest.main()
