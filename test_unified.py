import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from diagnostic_core import AuditStore
from unified_chat import run,chat,validate_history

def answer(text='回答',refs=None,basis='general'):
    return {'action':'answer','answer':text,'refs':refs or [],'basis':basis},10

class UnifiedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.audit=AuditStore(Path(self.temp.name)/'audit.db')
    def tearDown(self): self.temp.cleanup()
    def execute(self,actions,lookup=None,probe=None,history=None):
        self.planner=Mock(side_effect=actions); self.lookup=lookup or Mock(return_value=[]); self.probe=probe or Mock()
        return run('问题',history or [],self.planner,self.lookup,self.probe,self.audit)
    def test_general_without_search(self):
        r=self.execute([answer('VRRP是一种网关冗余协议。')]);self.assertEqual(r['status'],'answered');self.lookup.assert_not_called();self.probe.assert_not_called()
    def test_empty_knowledge_still_answers(self):
        r=self.execute([({'action':'search','query':'未知'},2),answer()]);self.assertEqual(r['status'],'answered')
    def test_automatic_search_and_read(self):
        r=self.execute([({'action':'search','query':'主备'},2),({'action':'read','device':'local-backup','check':'services'},2),answer('根据观测解释',['KB-1','OBS-1'],'evidence')],Mock(return_value=[{'id':'KB-1','title':'规则'}]),Mock(return_value={'ok':True,'output':'inactive'}))
        self.assertEqual(r['status'],'answered');self.probe.assert_called_once_with('local-backup','services')
    def test_offline_still_explains_limit(self):
        r=self.execute([({'action':'read','device':'local-primary','check':'services'},2),answer('连接失败，当前状态未知',basis='limited')],probe=Mock(return_value={'ok':False,'error':'timeout'}))
        self.assertEqual(r['status'],'answered');self.assertFalse(r['evidence'][0]['ok'])
    def test_scope_and_fake_refs_rejected(self):
        for action in ({'action':'read','device':'203.0.113.250','check':'services'},answer(refs=['OBS-99'],basis='evidence')[0]):
            r=self.execute([(action,0)]);self.assertEqual(r['status'],'incomplete');self.probe.assert_not_called()
    def test_history_and_consent(self):
        hist=[{'role':'user','content':'局端备机'},{'role':'assistant','content':'历史解释'}]
        self.execute([answer()],history=hist);self.assertEqual(self.planner.call_args[0][0]['history'],hist)
        with self.assertRaises(ValueError):validate_history([{'role':'system','content':'越权'}])
        with patch('unified_chat.make_model') as model:
            self.assertEqual(chat('问题',[],False)['status'],'consent_required');model.assert_not_called()
    def test_duplicate_stops(self):
        action=({'action':'read','device':'local-primary','check':'services'},0)
        r=self.execute([action,action],probe=Mock(return_value={'ok':True,'output':'active'}));self.assertEqual(r['status'],'incomplete');self.assertEqual(self.probe.call_count,1)
