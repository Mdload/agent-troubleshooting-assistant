"""经验提炼的来源、审核、适用范围与隐私边界；全程本地无模型。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from legacy.experience_learning import collect, review, approved_records, retrieve, write_new


class ExperienceLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'docs').mkdir()
        self.source = self.root/'docs/rule.md'
        self.source.write_text('正常HA中，无VIP节点停止Croc可符合局部预期。', encoding='utf-8')
        self.export = {'conversations': [{'id': 'synthetic-chat', 'consent_to_extract': True,
            'messages': [{'id': 'u1', 'role': 'user', 'content': '无VIP的Croc停止是否故障？'},
                         {'id': 'a1', 'role': 'assistant', 'content': '错误的原回答仅用于摘要追溯。'},
                         {'id': 'u2', 'role': 'user', 'content': 'UNSELECTED_PRIVATE_CONTEXT'}],
            'feedback': [{'question_id': 'u1', 'answer_id': 'a1', 'verdict': 'corrected',
                'corrected_answer': '正常HA假设下，无VIP且Croc停止符合局部预期；当前业务仍未知。',
                'reason': '纠正把服务停止一律当故障的说法。',
                'conditions': {'lesson_type': 'principle', 'operating_modes': ['normal_ha'],
                    'device_groups': ['local'], 'software_versions': [], 'evidence_date': None},
                'verification_sources': [{'path': 'docs/rule.md', 'quote': '无VIP节点停止Croc可符合局部预期。'}]}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def drafts(self):
        return collect(self.export, self.root)

    def approved(self):
        draft = self.drafts()
        item = draft['items'][0]
        return review(draft, item['id'], 'approve', 'synthetic-test-reviewer', item['candidate_hash'],
                      '仅测试审核管线，不是实际人工审核。', self.root)

    def test_import_is_draft_even_if_input_claims_approved_and_omits_unselected_chat(self):
        self.export['conversations'][0]['feedback'][0]['status'] = 'approved'
        draft = self.drafts()
        item = draft['items'][0]
        self.assertEqual(item['status'], 'needs_review')
        self.assertIsNone(item['review'])
        self.assertEqual(approved_records(draft, self.root), [])
        serialized = json.dumps(draft, ensure_ascii=False)
        self.assertNotIn('UNSELECTED_PRIVATE_CONTEXT', serialized)
        self.assertNotIn('错误的原回答', serialized)
        self.assertFalse(item['grants_permissions'])
        self.assertFalse(item['current_state_supported'])

    def test_consent_and_explicit_resolution_are_required(self):
        self.export['conversations'][0]['consent_to_extract'] = False
        self.assertEqual(self.drafts()['items'], [])
        self.export['conversations'][0]['consent_to_extract'] = True
        self.export['conversations'][0]['feedback'][0]['verdict'] = 'thumbs_up'
        self.assertEqual(self.drafts()['items'], [])

    def test_deduplicate_retains_both_selected_provenances(self):
        second = copy.deepcopy(self.export['conversations'][0])
        second['id'] = 'another-chat'
        self.export['conversations'].append(second)
        draft = self.drafts()
        self.assertEqual(len(draft['items']), 1)
        self.assertEqual(len(draft['items'][0]['provenance']), 2)

    def test_wrong_message_binding_is_rejected(self):
        self.export['conversations'][0]['feedback'][0]['question_id'] = 'u2'
        with self.assertRaises(ValueError): self.drafts()

    def test_credential_assignments_are_rejected_without_echoing_values(self):
        self.export['conversations'][0]['feedback'][0]['corrected_answer'] = 'LLM_API_KEY=PRIVATE_TEST_VALUE'
        with self.assertRaises(ValueError) as raised: self.drafts()
        self.assertNotIn('PRIVATE_TEST_VALUE', str(raised.exception))

    def test_private_path_and_fabricated_quote_cannot_become_evidence(self):
        source = self.export['conversations'][0]['feedback'][0]['verification_sources'][0]
        for replacement in ({'path': '../secret.md', 'quote': 'secret'},
                            {'path': 'docs/rule.md', 'quote': '不存在的证明'}):
            source.clear(); source.update(replacement)
            with self.assertRaises(ValueError): self.drafts()

    def test_live_state_and_undated_history_are_not_reusable_lessons(self):
        conditions = self.export['conversations'][0]['feedback'][0]['conditions']
        for kind in ('current_state', 'historical_case'):
            conditions['lesson_type'] = kind
            with self.assertRaises(ValueError): self.drafts()

    def test_candidate_hash_and_nonempty_sources_are_required_to_approve(self):
        draft = self.drafts(); item = draft['items'][0]
        with self.assertRaises(ValueError):
            review(draft, item['id'], 'approve', 'tester', 'wrong-hash', '检查', self.root)
        self.export['conversations'][0]['feedback'][0]['verification_sources'] = []
        draft = self.drafts(); item = draft['items'][0]
        with self.assertRaises(ValueError):
            review(draft, item['id'], 'approve', 'tester', item['candidate_hash'], '检查', self.root)

    def test_changed_candidate_or_source_requires_new_review(self):
        for mutation in ('candidate', 'source'):
            with self.subTest(mutation=mutation):
                approved = self.approved()
                if mutation == 'candidate': approved['items'][0]['candidate_answer'] += '擅自修改'
                else: self.source.write_text('changed rule', encoding='utf-8')
                with self.assertRaises(ValueError): approved_records(approved, self.root)
                self.source.write_text('正常HA中，无VIP节点停止Croc可符合局部预期。', encoding='utf-8')

    def test_rejection_and_forged_status_are_not_exported_as_approved(self):
        draft = self.drafts(); item = draft['items'][0]
        rejected = review(draft, item['id'], 'reject', 'tester', item['candidate_hash'], '依据不足', self.root)
        self.assertEqual(approved_records(rejected, self.root), [])
        draft['items'][0]['status'] = 'approved'
        with self.assertRaises(ValueError): approved_records(draft, self.root)

    def test_search_requires_matching_mode_group_and_review(self):
        query = '无VIP时Croc停止算故障吗'
        self.assertEqual(retrieve(self.drafts(), query, 'normal_ha', 'local', root=self.root), [])
        approved = self.approved()
        self.assertTrue(retrieve(approved, query, 'normal_ha', 'local', root=self.root))
        for mode, group in (('unknown', 'local'), ('maintenance', 'local'), ('normal_ha', 'remote')):
            self.assertEqual(retrieve(approved, query, mode, group, root=self.root), [])

    def test_software_version_condition_cannot_be_silently_ignored(self):
        self.export['conversations'][0]['feedback'][0]['conditions']['software_versions'] = ['0.8.11']
        approved = self.approved()
        query = '无VIP时Croc停止算故障吗'
        self.assertEqual(retrieve(approved, query, 'normal_ha', 'local', root=self.root), [])
        self.assertTrue(retrieve(approved, query, 'normal_ha', 'local', '0.8.11', self.root))

    def test_new_output_does_not_overwrite_reviewed_material(self):
        path = self.root/'result.json'
        write_new(path, self.drafts())
        with self.assertRaises(FileExistsError): write_new(path, {'items': []})


if __name__ == '__main__': unittest.main()
