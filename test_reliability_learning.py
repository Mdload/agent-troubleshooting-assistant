"""真实失败形成的开发回归：公开知识追问、精简视图与权限边界。"""
import copy
import unittest
from unittest.mock import Mock
from workflow_test_support import RunningApp
from unified_chat import search
from response_contract import response_policy, answer_issues, reference_link_rules
from knowledge_modules import model_modules


class ReliabilityLearningTests(unittest.TestCase):
    def test_plain_model_text_still_cannot_bypass_host_reference_scope(self):
        from chat_model import make_model
        from evaluation.ha_model import FixtureResponse
        opener=Mock()
        opener.open.return_value=FixtureResponse({'choices':[{'finish_reason':'stop',
            'message':{'content':'Croc是设备里的软件。[KB-999]'}}],'usage':{'total_tokens':3}})
        planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1',
                           'LLM_API_KEY':'OFFLINE_FIXTURE','LLM_MODEL_ID':'offline-fixture'},opener)
        probe=Mock()
        with RunningApp(planner,probe) as client:
            code,result=client.chat('Croc是软件还是一条线？')
            self.assertEqual((code,result['status']),(200,'incomplete'))
            self.assertNotIn('[KB-999]',result['answer'])
            self.assertEqual(opener.open.call_count,2)
        probe.assert_not_called()

    def test_discussing_an_answer_does_not_request_a_device_read(self):
        from question_routing import classify
        for question in ('你刚才说远端Croc启动，那个说法是什么意思？',
                         '我复述一下：远端Croc打包，Router转送，局端Croc拆包，对吗？'):
            self.assertEqual(classify(question)['intent'],'explain')
        self.assertEqual(classify('你刚才说远端Croc启动，请检查远端主机现在是否运行')['route'],'diagnostic')
        self.assertEqual(classify('state MASTER是不是始终负责业务，priority什么时候起作用')['domain'],'basics')

    def test_current_question_does_not_inherit_old_requirements(self):
        from knowledge_coverage import assessment_query, assess
        current='Croc是一条线还是软件，它究竟做什么？'
        hint='局端主机为什么停止Croc，Keepalived如何接管？\n追问：'+current
        self.assertEqual(assessment_query(current,hint),current)
        coverage=assess(assessment_query(current,hint),search(current))
        self.assertEqual({n['topic'] for n in coverage['requirements']},{'croc'})
        self.assertEqual(coverage['status'],'supported')
        self.assertEqual(assessment_query('那它呢？',hint),hint)
        comparison='那Router和Croc的分工有什么区别？'
        self.assertEqual(assessment_query(comparison,hint,reference_entities=['remote-ha-candidate.sh']),comparison)

    def test_concept_background_cannot_replace_primary_evidence(self):
        from knowledge_coverage import assess, answer_issue
        import qa
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-001','KB-002','KB-018')]
        coverage=assess('Croc是软件还是一条线？',sources,conceptual_context=True)
        self.assertIn('KB-001',coverage['context_refs'])
        self.assertNotIn('KB-018',coverage['supporting_refs'])
        self.assertIsNone(answer_issue(coverage,'evidence',['KB-001','KB-002'],sources))
        self.assertEqual(answer_issue(coverage,'evidence',['KB-001'],sources),'knowledge_citation_gap')
        private=assess('新服务A监听什么端口？',sources,conceptual_context=True)
        self.assertNotEqual(private['status'],'supported')
        self.assertEqual(private['context_refs'],[])
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-002','KB-005','KB-013','KB-018')]
        coverage=assess('Keepalived和Croc是软件吗，它们分别负责什么？',sources,conceptual_context=True)
        self.assertIn('KB-013',coverage['context_refs'])
        self.assertIsNone(answer_issue(coverage,'evidence',['KB-002','KB-013'],sources))
        self.assertNotIn('KB-018',coverage['supporting_refs'])

    def test_unrecognized_phrasing_can_cite_provided_context_but_not_a_known_gap(self):
        from knowledge_coverage import assess, answer_issue
        import qa
        sources=[s for s in qa.RECORDS if s['id'] in ('KB-001','KB-018')]
        coverage=assess('顺着发送这个动作讲一遍好吗？',sources,allow_unassessed_context=True)
        self.assertEqual(coverage['status'],'unassessed')
        self.assertFalse(coverage['semantic_verified'])
        self.assertEqual(coverage['unassessed_refs'],['KB-001'])
        self.assertEqual(coverage['gaps'],[])
        self.assertIsNone(answer_issue(coverage,'limited',['KB-001'],sources))
        self.assertEqual(answer_issue(coverage,'limited',['KB-018'],sources),'unsupported_knowledge_citation')

    def test_discussion_keeps_documented_path_sources_and_tool_error_principles(self):
        from unified_chat import prefetch_query
        from question_routing import classify
        anchor='我在现场电脑上点一下发送，文件为什么就能到实验室电脑上，能顺着这个动作讲一遍吗？'
        followup='你说接收地址能在两台机器之间转移，我发文件怎么知道该发给哪台？'
        query=prefetch_query(followup,[{'role':'user','content':anchor}])
        self.assertIn(anchor,query)
        self.assertIn('KB-001',{s['id'] for s in search(query)})
        route=classify(followup,[{'role':'user','content':anchor}])
        self.assertEqual((route['domain'],route['intent']),('topology','explain'))
        self.assertEqual(classify('Agent一遇到工具报错就把异常文本塞回模型，让它自己继续，靠谱吗？')['domain'],'basics')
        self.assertEqual(classify('我现在不了解这套拓扑，你能大概讲讲吗')['intent'],'explain')
        self.assertEqual(classify('如果文件传到一半，当班那台突然坏了，另一台接手后还能继续吗？',
                                 [{'role':'user','content':anchor}])['intent'],'explain')

    def test_unfamiliar_lab_concept_uses_overview_but_parameters_do_not(self):
        from unified_chat import search
        from knowledge_coverage import assess
        question='这里两台设备互相备用，是两台一起传还是只有一台干活？'
        sources=search(question)
        self.assertIn('KB-001',{s['id'] for s in sources})
        self.assertIn('KB-005',{s['id'] for s in sources})
        private='我们的新服务监听哪个端口？'
        self.assertNotEqual(assess(private,search(private))['status'],'supported')
        self.assertFalse(response_policy('详细解释局端优先级配置')['prefer_plain_explanation'])

    def chain(self, questions, ident):
        contexts=[]
        def planner(context):
            contexts.append(copy.deepcopy(context))
            text='这是公开原理，实际结果仍要核对。['+ident+']'
            if context['response_contract']['source_display']=='expanded':
                text+=' [对应原文]('+context['sources'][0]['reference_urls'][0]+')'
            return {'action':'answer','answer':text,
                    'refs':[ident],'basis':'evidence'},3
        probe=Mock()
        with RunningApp(planner,probe) as client:
            history=[]
            for q in questions:
                code,result=client.chat(q,history=history,conversation_id='public-learning')
                self.assertEqual((code,result['status']),(200,'answered'),result)
                history += [{'role':'user','content':q},{'role':'assistant','content':result['answer']}]
            code,blocked=client.chat('请检查局端主机现在的服务状态',history=history[-8:],conversation_id='public-learning')
            self.assertEqual(blocked['status'],'current_unverified')
        probe.assert_not_called()
        for context in contexts:
            self.assertEqual(context['tool_catalog'],{})
            self.assertEqual(context['remaining_reads'],0)
            self.assertEqual(context['knowledge_sufficiency']['supporting_refs'],[ident])
            self.assertEqual([s['id'] for s in context['sources']],[ident])
            self.assertFalse(context['knowledge_sufficiency']['current_state_supported'])
        return contexts

    def test_ha_config_fields_and_source_followup_stay_in_public_topic(self):
        from question_routing import classify
        self.assertEqual(classify('不用读取设备，我只问你给的示例：主机降到 70、备机是 90 时该怎么判断，单凭这两个数能不能确定会切换？')['domain'],'basics')
        contexts=self.chain([
            '我想先离线学会看 Keepalived 配置，判断它有没有检测业务、什么情况下会切到备机，第一步该从哪里看起？',
            '那 fall 是3、weight 是-20，就一定切换吗？',
            '我有点没跟上：主机降到 70、备机是 90 时，到底会不会切，刚才“仍高于对端”的那句是不是写反了？',
            '这些判断有没有我能自己打开看的公开资料？'],'KB-129')
        self.assertEqual(contexts[-1]['response_contract']['source_display'],'expanded')
        view=model_modules(contexts[-1])
        self.assertTrue(view['knowledge_modules'][0]['sections'][0]['reference_urls'])

    def test_tool_retry_and_compensation_followup_keep_reference_scope(self):
        contexts=self.chain([
            '我做的 Agent 偶尔等工具等到超时，能不能统一捕获异常后重试三次，失败就撤销前面的操作？',
            '那重启服务后只有 active，没有这次请求的记录怎么办？',
            '有没有我能直接打开的公开文档，具体哪一节讲“超时结果未知”和“补偿时不能误删别人的并发变更”？'],'KB-130')
        self.assertEqual(contexts[-1]['response_contract']['source_display'],'expanded')

    def test_beginner_model_projection_uses_curated_plain_text_without_changing_archive(self):
        q='我刚来实验室，你能带我跟着一份文件从现场电脑走到实验室电脑，讲讲中间是谁接手、各负责哪一步吗？'
        sources=search(q)
        self.assertEqual({s['id'] for s in sources},{'KB-001','KB-003','KB-004','KB-005'})
        context={'sources':sources,'response_contract':response_policy(q)}
        before=copy.deepcopy(context)
        view=model_modules(context)
        content=' '.join(s['content'] for m in view['knowledge_modules'] for s in m['sections'])
        self.assertNotIn('120',content)
        self.assertNotIn('192.0.2.15',content)
        self.assertIn('现场',content)
        self.assertEqual(context,before)
        self.assertTrue(all(s['evidence_date']=='2026-09-28' for s in sources))
        detail={'sources':sources,'response_contract':response_policy('详细解释远端的优先级配置')}
        detailed=' '.join(s['content'] for m in model_modules(detail)['knowledge_modules'] for s in m['sections'])
        self.assertIn('120',detailed)

    def test_clickable_reference_url_is_preserved_without_consuming_prose_budget(self):
        text='参考[原文](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)。'
        self.assertEqual(answer_issues(text,[],response_policy('用20字给出公开资料'),[]),[])
        self.assertIn('#section-9.2.2',text)
        policy=response_policy('给出公开文档')
        rules=reference_link_rules([{'id':'KB-130','reference_urls':['https://example.org/reference']}],policy)
        self.assertEqual(answer_issues('请看某书。',['KB-130'],policy,rules)[0]['id'],'requested_reference_link')
        self.assertEqual(answer_issues('[原文](https://example.org/reference)',['KB-130'],policy,rules),[])
        self.assertTrue(answer_issues('[另一本](https://example.org/other)',['KB-130'],policy,rules))

    def test_software_or_wire_followup_keeps_tunnel_concept_source_and_plain_style(self):
        from unified_chat import prefetch_query
        from knowledge_coverage import assess
        q='这里的 Croc 隧道我还没理解，它是远端和局端里运行的软件，还是它们之间另外接的一条线？'
        history=[{'role':'user','content':'我刚来实验室，你能带我跟着一份文件从现场电脑走到实验室电脑，讲讲中间是谁接手、各负责哪一步吗？'}]
        query=prefetch_query(q,history) or q
        sources=search(query)
        self.assertIn('KB-002',assess(query,sources)['supporting_refs'])
        self.assertTrue(response_policy(q)['prefer_plain_explanation'])
        self.assertEqual({n['topic'] for n in assess(query,sources)['requirements']},{'croc'})
        recap='所以我可以理解为：远端 Croc 负责打包，Router 负责转送，局端 Croc 负责拆包再交给实验室电脑，对吗？'
        coverage=assess(recap,search(recap))
        self.assertEqual(coverage['status'],'supported')
        self.assertEqual({n['topic'] for n in coverage['requirements']},{'croc','router_ha'})
        self.assertTrue(response_policy(recap)['prefer_plain_explanation'])
        recap_sources=search(recap)
        context={'question':recap,'sources':recap_sources,
                 'workflow_policy':{'mode':'knowledge'},'response_contract':response_policy(recap),
                 'knowledge_sufficiency':coverage}
        before=copy.deepcopy(context)
        view=model_modules(context)
        self.assertEqual({s['id'] for s in view['sources']},set(coverage['supporting_refs']))
        self.assertEqual(context,before)
        context['workflow_policy']['mode']='diagnostic'
        self.assertEqual({s['id'] for s in model_modules(context)['sources']},{s['id'] for s in recap_sources})


if __name__=='__main__':unittest.main()
