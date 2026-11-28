"""模拟用户暴露问题的开发回归；不作为新的独立质量评估。"""
import unittest
from unittest.mock import Mock
import retrieval.qa as qa
import retrieval.topology_entities as entities
from retrieval.knowledge_coverage import assess, answer_issue
from legacy.question_routing import classify
from legacy.unified_chat import search, prefetch_query
from tests.workflow_test_support import RunningApp

NEW='我是刚来实验室的新同学，局端、远端、Router 和 Croc 分别是干什么的，它们怎么配合把数据传过去？'
AGENT='我一直没想明白，Agent 调用工具之后为什么还要把结果交回模型，程序直接把工具结果给我不行吗？'


class SimulatedUserRegressions(unittest.TestCase):
    def test_generic_agent_word_is_not_a_local_script_alias(self):
        self.assertFalse(entities.matches(AGENT))
        self.assertEqual(classify(AGENT)['domain'],'general')
        self.assertIsNone(prefetch_query(AGENT,[]))
        self.assertTrue(entities.matches('测试节点agent.py的路径在哪里'))
        self.assertEqual(classify('测试节点agent.py的路径在哪里')['route'],'lab_knowledge')

    def test_source_followup_does_not_convert_speakers_time_to_device_state(self):
        history=[{'role':'user','content':AGENT}]
        for q in ['你刚才说“这些判断写不进 if”，这是不是又太绝对了，能不能给我一个公开出处，而不是重复 agent.py 的路径记录？',
                  '我问的是你刚才那个 Agent 判断的公开出处，怎么又扯到读取设备了？']:
            with self.subTest(question=q):
                self.assertEqual(classify(q,history)['domain'],'general')
                self.assertEqual(classify(q,history)['intent'],'explain')

    def test_explicit_device_request_keeps_original_scope_even_when_agent_is_mentioned(self):
        for q in ['请帮我检查远端主机当前Croc，解释Agent如何选工具',
                  'Agent请检查局端备机现在的服务状态']:
            with self.subTest(question=q):
                result=classify(q)
                self.assertEqual(result['route'],'diagnostic')
                self.assertTrue(result['scope'])

    def test_intro_has_direct_role_and_path_sources_without_technical_inference(self):
        sources=search(NEW)
        self.assertEqual({s['id'] for s in sources},{'KB-001','KB-003','KB-004','KB-005'})
        coverage=assess(NEW,sources)
        self.assertEqual(coverage['status'],'supported')
        self.assertIsNone(answer_issue(coverage,'evidence',[s['id'] for s in sources],sources))

    def test_simpler_followup_requests_endpoints_without_old_router_requirement(self):
        q='我再试一次，你能先用简单的话说说局端和远端分别是干什么的吗？'
        query=prefetch_query(q,[{'role':'user','content':NEW}])
        sources=search(query)
        coverage=assess(query,sources)
        self.assertEqual(coverage['status'],'supported')
        self.assertEqual({n['topic'] for n in coverage['requirements']},{'business_path','local_ha','remote_ha'})
        self.assertTrue({'KB-001','KB-004','KB-005'} <= set(coverage['supporting_refs']))
        self.assertIsNone(answer_issue(coverage,'evidence',['KB-001','KB-004','KB-005'],sources))

    def test_unspecified_ha_mechanism_is_general_without_guessing_specific_end(self):
        for q in ['我对这套主备还不熟，主机上停掉 keepalived 后，业务到底是停了还是会自动转到备机？',
                  '那你先讲一般原理吧：停掉 keepalived 和停掉业务服务有什么区别，我还需要提供什么才能判断这套环境会不会切换？']:
            with self.subTest(question=q):
                self.assertEqual(classify(q)['domain'],'basics')
                self.assertIsNone(prefetch_query(q,[]))
        self.assertEqual(classify('局端主机停掉Keepalived后按什么机制切换')['route'],'lab_knowledge')
        self.assertEqual(classify('主机现在Keepalived停止了，请检查')['route'],'clarify')

    def test_hypothetical_service_failure_uses_general_ha_without_borrowing_local_values(self):
        q='如果主机还能 ping 通，但业务已经连不上了，备机会自己接管吗？'
        self.assertEqual(classify(q)['route'],'general')
        self.assertIsNone(prefetch_query(q,[]))
        self.assertEqual(classify('先讲一般原理吧，备机通常根据什么信号判断该接管，业务访问失败算不算？')['route'],'general')
        self.assertEqual(classify('请检查局端主机现在业务不通，备机接管了吗')['route'],'diagnostic')

    def test_project_transfer_path_does_not_require_users_to_know_component_names(self):
        for q in ('假如我要把外面现场的一段视频传回实验室，数据一路上会经过哪些设备，它们各自做了什么？',
                  '数据传到实验室时经过哪些设备？'):
            sources=search(q)
            self.assertIn('KB-001',{s['id'] for s in sources})
            self.assertEqual(assess(q,sources)['status'],'supported')
            self.assertEqual(set(assess(q,sources)['supporting_refs']),{'KB-001','KB-003','KB-004','KB-005'})
            self.assertIsNone(answer_issue(assess(q,sources),'evidence',['KB-001','KB-003','KB-004','KB-005'],sources))
        self.assertEqual(search('视频传到电影院会经过哪些设备？'),[])

    def test_how_to_inspect_is_an_explanation_even_when_status_words_are_embedded(self):
        questions=['先不操作设备，你告诉我该查哪些文件或配置项，才能看出有没有检测业务、失败几次才切换？',
                   '我就是想了解查看方法，不用连接设备：如果用的是 keepalived，该看哪个配置文件、哪些字段？']
        for q in questions:
            self.assertEqual(classify(q)['route'],'general')
        self.assertEqual(classify('请检查局端主机当前有没有业务探测失败')['route'],'diagnostic')

    def test_component_role_followup_keeps_router_and_path_sources(self):
        q='Croc、Router 和 tun16 我还是分不清，你能用寄快递打个比方讲讲它们各自干什么吗？'
        query=prefetch_query(q,[{'role':'user','content':'假如我要把外面现场的一段视频传回实验室，数据一路上会经过哪些设备，它们各自做了什么？'}]) or q
        sources=search(query)
        coverage=assess(query,sources)
        self.assertTrue({'KB-001','KB-002','KB-003'} <= {s['id'] for s in sources})
        self.assertEqual(coverage['status'],'supported')
        self.assertTrue({'KB-001','KB-002','KB-003'} <= set(coverage['supporting_refs']))
        self.assertIsNone(answer_issue(coverage,'evidence',['KB-001','KB-002','KB-003'],sources))

    def test_generic_agent_chain_uses_no_lab_sources_or_device_access(self):
        contexts=[]
        def planner(context):
            contexts.append(context)
            return {'action':'answer','answer':'程序也可以按规则处理工具结果，模型适用于需要它参与判断的环节。','refs':[],'basis':'general'},3
        probe=Mock()
        with RunningApp(planner,probe) as client:
            history=[]
            for q in [AGENT,'你说“必须回到模型”的依据是什么，if不也能形成反馈循环吗？',
                      '我问的是你刚才那个 Agent 判断的公开出处，怎么又扯到读取设备了？']:
                code,result=client.chat(q,history=history,conversation_id='agent-feedback')
                self.assertEqual((code,result['status']),(200,'answered'))
                history += [{'role':'user','content':q},{'role':'assistant','content':result['answer']}]
        self.assertTrue(all(c['sources']==[] and c['tool_catalog']=={} for c in contexts))
        self.assertTrue(all(c['workflow_policy']['domain']=='general' for c in contexts))
        probe.assert_not_called()


if __name__=='__main__':unittest.main()
