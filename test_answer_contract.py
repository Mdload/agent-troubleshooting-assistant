"""已见回答缺陷的开发回归，非独立模型质量评估。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from diagnostic_core import AuditStore
from response_contract import response_policy, remote_rules, answer_issues
from unified_chat import run, search


class AnswerContractTests(unittest.TestCase):
    def execute(self, question, answers, topology=False):
        self.planner=Mock(side_effect=[(a, 7) for a in answers])
        self.probe=Mock()
        with tempfile.TemporaryDirectory() as directory:
            return run(question,[],self.planner,search if topology else lambda q:[],
                       self.probe,AuditStore(Path(directory)/'audit.db'),permitted_checks={},
                       question_policy={'mode':'knowledge','domain':'topology' if topology else 'general'})

    @staticmethod
    def answer(text,refs=None):
        return {'action':'answer','answer':text,'refs':refs or [],'basis':'evidence' if refs else 'general'}

    def test_verbose_answer_gets_one_rewrite_without_slicing_conditions(self):
        bad=self.answer('默认情况需要保留成立条件。'*45)
        good=self.answer('列表可修改；元组自身的元素绑定不能修改，但其中的列表成员仍可修改。')
        result=self.execute('列表和元组有什么区别？',[bad,good])
        self.assertEqual(result['answer'],good['answer'])
        self.assertEqual(result['total_tokens'],14)
        self.assertEqual(self.planner.call_count,2)
        feedback=self.planner.call_args[0][0]['correction_feedback']
        self.assertIn('answer_too_long',[i['id'] for i in feedback['constraints']])
        self.probe.assert_not_called()

    def test_repeated_oversize_stops_and_preserves_shared_correction_budget(self):
        bad=self.answer('仍应保留条件而非裁剪。'*60)
        result=self.execute('Python列表是什么？',[bad,bad,self.answer('unused')])
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['reason'],'answer_contract_violation')
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)

    def test_requested_details_and_code_are_preserved(self):
        text='这是用户要求的详细说明，保留完整条件。'*35+'\n\n```python\nx = "a\\n"\n```'
        result=self.execute('请详细解释列表和元组。',[self.answer(text)])
        self.assertEqual(result['answer'],text)
        self.assertEqual(self.planner.call_count,1)
        # 简短正文中的代码字符串及路径不改写，也不靠切掉代码缩短。
        text='示例只演示字符串。\n\n```python\nvalue = '+repr('x'*600)+'\n```\n路径：`C:\\new\\notes`'
        self.assertEqual(self.execute('说明这个例子。',[self.answer(text)])['answer'],text)

    def test_explicit_length_takes_precedence_over_expand_request(self):
        policy=response_policy('详细说明，但不超过100字')
        self.assertEqual(policy['max_prose_chars'],100)
        self.assertTrue(answer_issues('长'*101,[],policy,[]))
        self.assertEqual(answer_issues('字'*100+' [KB-004]',[],policy,[]),[])
        self.assertEqual(response_policy('100字以内说明原理')['max_prose_chars'],100)
        self.assertEqual(response_policy('不要详细解释，也不用给代码')['max_prose_chars'],400)

    def test_candidate_claim_needs_its_remote_source_next_to_the_claim(self):
        bad=self.answer('candidate检查至少一条链路合格，这不等于端到端通过。[KB-009]',['KB-009'])
        good=self.answer('正常HA资料中，candidate检查至少一条候选链路合格；这不证明端到端业务通过。[KB-004]',['KB-004'])
        result=self.execute('remote-ha-candidate.sh是干什么的',[bad,good],True)
        self.assertEqual(result['answer'],good['answer'])
        self.assertEqual(result['refs'],['KB-004'])
        feedback=self.planner.call_args[0][0]['correction_feedback']
        self.assertIn('remote_candidate_source',[i['id'] for i in feedback['constraints']])
        self.probe.assert_not_called()

    def test_distant_ref_does_not_cover_the_candidate_paragraph(self):
        rules=remote_rules(search('remote-ha-candidate.sh是什么'),'remote')
        bad='candidate检查至少一条合格链路。\n\n其他背景来源。[KB-004]'
        issues=answer_issues(bad,['KB-004'],response_policy('解释一下'),rules)
        self.assertIn('remote_candidate_source',[i['id'] for i in issues])
        self.assertFalse(remote_rules(search('remote-ha-candidate.sh是什么'),'local'))
        self.assertFalse(remote_rules([{'id':'KB-004'}],'remote'))

    def test_resident_program_list_cannot_be_relabeled_as_dialers(self):
        bad=self.answer('拨号程序：dialManager_v1.py、manage_MAR及pingFor等。',['KB-009'])
        good=self.answer('remote-ha-routes.service管理业务路由，与拨号分工不同。[KB-004]\n\n启动资料还列有路由、信号采集和管理程序，不能把全部常驻程序都叫拨号程序。[KB-009]',['KB-004','KB-009'])
        result=self.execute('remote-ha-routes.service就是拨号程序吗',[bad,good],True)
        self.assertEqual(result['answer'],good['answer'])
        self.assertEqual(self.planner.call_count,2)

    def test_negative_or_quoted_code_does_not_become_a_dialing_assertion(self):
        rules=remote_rules(search('remote-ha-routes.service是什么'),'remote')
        for text in ('remote-ha-routes.service不负责拨号。',
                     '不能说Croc负责拨号。',
                     '错误示例：Croc负责拨号。',
                     '`Croc负责拨号。`',
                     '不能据此认为两台配置完全一致。',
                     '```text\nCroc负责拨号\n```'):
            self.assertEqual(answer_issues(text,[],response_policy('解释'),rules),[],text)
        self.assertIn('remote_program_roles',[i['id'] for i in answer_issues(
            'Croc负责拨号。',[],response_policy('解释'),rules)])
        self.assertIn('remote_program_roles',[i['id'] for i in answer_issues(
            '两台启动机制完全相同。',[],response_policy('解释'),rules)])
        self.assertIn('remote_program_roles',[i['id'] for i in answer_issues(
            '`Croc`负责拨号。',[],response_policy('解释'),rules)])




    def test_ping_necessity_error_gets_one_correction_and_keeps_tools_disabled(self):
        bad=self.answer('ping通只是TCP业务正常的必要非充分条件。反过来ping不通也不代表TCP失败。')
        good=self.answer('ping通只说明本次ICMP应答成功，不能证明TCP业务正常；ICMP被过滤时TCP也可能正常。')
        result=self.execute('ping通能证明TCP业务正常吗？',[bad,good])
        self.assertEqual(result['answer'],good['answer'])
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)
        self.probe.assert_not_called()

    def test_ping_guard_preserves_negation_questions_and_scoped_examples(self):
        policy=response_policy('ping通能证明TCP业务正常吗？')
        for text in ('ping通不是TCP业务正常的必要条件。',
                     '不能说ping通是业务正常的必要条件。',
                     'ping通是业务正常的必要条件吗？',
                     '错误示例：ping通是业务正常的必要条件。',
                     '```text\nping通是业务正常的必要条件。\n```',
                     '如果把业务定义为ICMP监测，ping通是该监测成功的必要条件。'):
            self.assertEqual(answer_issues(text,[],policy,[]),[],text)
        self.assertEqual(answer_issues('列表用于保存有序元素。',[],response_policy('Python列表是什么'),[]),[])


    def test_internal_framing_is_translated_without_extra_model_call_or_losing_conditions(self):
        bad=self.answer('按资料里的正常主备模式，主机负责业务。以上是文档记录的分工方式。当前情况未查看。')
        result=self.execute('主机和备机平时分别干什么？',[bad])
        self.assertEqual(result['status'],'answered');self.assertEqual(self.planner.call_count,1)
        self.assertNotIn('文档',result['answer']);self.assertIn('当前情况未查看',result['answer'])
        self.assertIn('平时一台工作、一台待机',result['answer']);self.probe.assert_not_called()

    def test_translation_preserves_raw_code_citations_and_uncertainty(self):
        from response_contract import plain_language_answer
        text='角色回调的预期行为不代表硬件损坏，尚未检查主机。[OBS-1]\n\n`角色回调`\n```text\n以上是文档描述的机制。\n```'
        result=plain_language_answer(text)
        self.assertIn('不代表硬件损坏，尚未检查主机。[OBS-1]',result)
        self.assertIn('`角色回调`',result);self.assertIn('```text\n以上是文档描述的机制。\n```',result)
        self.assertIn('自动执行的动作',result)

    def test_explicit_source_question_can_explain_provenance(self):
        policy=response_policy('你引用的是哪份资料，给原文来源？')
        self.assertFalse(policy['avoid_internal_meta'])
        self.assertEqual(answer_issues('根据资料中的记载解释。',[],policy,[]),[])

    def test_explicit_concept_only_does_not_request_a_live_target(self):
        from question_routing import classify
        self.assertEqual(classify('只讲平时的情况，不查现场：Croc没运行，就说明设备坏了吗？')['route'],'lab_knowledge')
        self.assertEqual(classify('不查现场，局端主机现在正常吗？')['route'],'current_unverified')


    def test_simplification_does_not_turn_immutability_into_unconditional_hashability(self):
        bad=self.answer('列表能改，元组不可变，所以元组可以作为字典的键。')
        good=self.answer('列表可以增删或替换元素，元组不能替换已有的元素绑定。元组要作字典键，还要求所有成员可哈希。')
        result=self.execute('简单解释列表和元组有什么不同。',[bad,good])
        self.assertEqual(result['answer'],good['answer']);self.assertEqual(self.planner.call_count,2)
        self.assertIn('tuple_key_condition',[i['id'] for i in self.planner.call_args[0][0]['correction_feedback']['constraints']])
        self.probe.assert_not_called()


class ReasoningPromptTests(unittest.TestCase):
    def test_conditions_and_evidence_policy_reaches_every_actual_prompt_route(self):
        from chat_model import select_prompt, ANSWER_REASONING_POLICY
        cases=[({'mode':'knowledge','domain':'general'},'general_learning'),
               ({'mode':'knowledge','domain':'basics'},'general_learning'),
               ({'mode':'knowledge','domain':'lab'},'lab_knowledge'),
               ({'mode':'diagnostic'},'diagnostic'),
               ({'mode':'material_analysis'},'material_analysis')]
        for policy,expected in cases:
            prompt,kind=select_prompt({'workflow_policy':policy})
            self.assertEqual(kind,expected)
            self.assertEqual(prompt.count(ANSWER_REASONING_POLICY),1)

if __name__=='__main__':unittest.main()
