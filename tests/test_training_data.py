"""只使用临时历史：纠正、归属、撤回、脱敏、上下文和数据分组。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from server.chat_history import HistoryStore
from legacy.training_data import prepare, dataset, export_dataset, grouped_splits, write_new, Redactor


class TrainingDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'history.db'
        self.store = HistoryStore(self.path)
        self.messages = [{'role': 'user', 'content': '局端为什么会切换？IP 192.0.2.13'},
                         {'role': 'assistant', 'content': '错误原答：一定6秒恢复。password=BAD_SECRET'},
                         {'role': 'user', 'content': '那备机呢？联系13812345678 a@example.test'},
                         {'role': 'assistant', 'content': '需要结合备机健康和优先级。'}]
        self.store.put('account-a', {'id': 'chat-a', 'title': '测试', 'messages': self.messages, 'expected_revision': 0})

    def review(self):
        value = prepare(self.path, 'account-a', ['chat-a'])
        value.update(use_for_training=True, reviewer='local-reviewer')
        for label in value['labels']:
            label.update(verdict='verified', review_note='已核对该问题与对应答案。')
        value['labels'][0].update(verdict='corrected', corrected_answer='健康失败会按配置降权；192.0.2.13的配置周期不等于实测恢复时间。')
        return value

    def test_prepare_is_unreviewed_and_does_not_mutate_or_mix_accounts(self):
        value = prepare(self.path, 'account-a', ['chat-a'])
        self.assertFalse(value['use_for_training'])
        self.assertTrue(all(label['verdict'] == 'unreviewed' for label in value['labels']))
        self.assertIn('错误原答', value['conversations'][0]['messages'][1]['content'])
        with self.assertRaises(ValueError):
            prepare(self.path, 'account-b', ['chat-a'])
        self.assertEqual(self.store.get('account-a', 'chat-a')['revision'], 1)

    def test_corrected_targets_and_context_replace_wrong_answer_but_preserve_trace(self):
        value = dataset(self.review(), self.path, {}, 'local-v1')
        self.assertEqual(len(value['samples']), 2)
        rendered = json.dumps([s['messages'] for s in value['samples']], ensure_ascii=False)
        self.assertNotIn('错误原答', rendered)
        self.assertIn('健康失败会按配置降权', value['samples'][1]['messages'][1]['content'])
        self.assertIn('错误原答', value['samples'][0]['provenance'][0]['original_answer'])
        for secret in ('BAD_SECRET', '192.0.2.13', '13812345678', 'a@example.test'):
            self.assertNotIn(secret, json.dumps(value, ensure_ascii=False))
        self.assertEqual(value['samples'][0]['split'], value['samples'][1]['split'])
        self.assertIn('<IP_1>', rendered)

    def test_requires_scope_review_and_verified_context(self):
        unreviewed = prepare(self.path, 'account-a', ['chat-a'])
        with self.assertRaises(ValueError):
            dataset(unreviewed, self.path, {}, 'v1')
        value = self.review()
        value['labels'][0]['verdict'] = 'unreviewed'
        with self.assertRaisesRegex(ValueError, '上下文'):
            dataset(value, self.path, {}, 'v1')
        value['labels'][1]['verdict'] = 'exclude'
        with self.assertRaisesRegex(ValueError, '没有已核实'):
            dataset(value, self.path, {}, 'v1')

    def test_changed_deleted_and_edited_snapshots_fail_before_export(self):
        value = self.review()
        tampered = copy.deepcopy(value)
        tampered['conversations'][0]['messages'][1]['content'] = '伪造历史'
        with self.assertRaisesRegex(ValueError, '历史已修改'):
            dataset(tampered, self.path, {}, 'v1')
        self.store.put('account-a', {'id': 'chat-a', 'title': '修改', 'messages': self.messages, 'expected_revision': 1})
        with self.assertRaisesRegex(ValueError, '历史已修改'):
            dataset(value, self.path, {}, 'v1')
        value = self.review()
        self.store.delete('account-a', 'chat-a', 2)
        with self.assertRaises(ValueError):
            dataset(value, self.path, {}, 'v1')

    def test_duplicate_labels_unknown_sources_and_empty_correction_are_rejected(self):
        for change in ('duplicate', 'unknown', 'empty'):
            value = self.review()
            if change == 'duplicate':
                value['labels'].append(copy.deepcopy(value['labels'][0]))
            elif change == 'unknown':
                value['labels'][0]['source_refs'] = ['KB-NOT-FOUND']
            else:
                value['labels'][0]['corrected_answer'] = ''
            with self.subTest(change=change), self.assertRaises(ValueError):
                dataset(value, self.path, {}, 'v1')

    def test_same_conversation_and_near_duplicates_never_cross_splits(self):
        samples = []
        questions = ['局端健康检查失败之后为什么会降权？', '局端健康检查失败后为什么会降权？', '无关问题一', '无关问题二']
        for i, question in enumerate(questions):
            samples.append({'messages': [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': '已核实'}],
                            'provenance': [{'conversation_id': 'a' if i in (1, 2) else str(i)}]})
        grouped_splits(samples)
        self.assertEqual(samples[0]['split'], samples[1]['split'])
        self.assertEqual(samples[1]['split'], samples[2]['split'])

    def test_jsonl_export_readable_and_provenance_aligned_without_overwrite(self):
        value = dataset(self.review(), self.path, {}, 'local-v1')
        output = self.root/'dataset'
        counts = export_dataset(value, output)
        self.assertEqual(sum(counts.values()), 2)
        self.assertTrue(json.loads((output/'manifest.json').read_text(encoding='utf-8'))['complete'])
        rows = [json.loads(line) for split in ('train', 'validation', 'test')
                for line in (output/(split+'.jsonl')).read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all(set(row) == {'messages'} for row in rows))
        self.assertEqual(len((output/'provenance.jsonl').read_text(encoding='utf-8').splitlines()), 2)
        with self.assertRaises(FileExistsError):
            export_dataset(value, output)

    def test_credentials_private_keys_and_urls_redacted_and_new_files_exclusive(self):
        redactor = Redactor()
        text = 'Bearer MY_TOKEN api_key="REAL_VALUE" https://bob:MY_PASSWORD@example.test/a\n-----BEGIN PRIVATE KEY-----\nMY_KEY\n-----END PRIVATE KEY-----'
        cleaned = redactor(text)
        for secret in ('MY_TOKEN', 'REAL_VALUE', 'MY_PASSWORD', 'MY_KEY'):
            self.assertNotIn(secret, cleaned)
        path = self.root/'selected.json'
        write_new(path, {'ok': True})
        with self.assertRaises(FileExistsError):
            write_new(path, {'overwrite': True})

    def test_reviewed_source_snapshot_changes_require_new_review(self):
        record={'id':'KB-001','title':'示例','historical_finding':'已核实的旧规则。',
                'operating_modes':['normal_ha'],'evidence_date':'2026-09-28'}
        records={'KB-001':record}
        value=self.review()
        value['knowledge_snapshot']=prepare(self.path,'account-a',['chat-a'],records,'v1')['knowledge_snapshot']
        value['labels'][0]['source_refs']=['KB-001']
        output=dataset(value,self.path,records,'v1')
        self.assertEqual(output['knowledge_snapshot']['KB-001']['historical_finding'],'已核实的旧规则。')
        changed=copy.deepcopy(records);changed['KB-001']['historical_finding']='不同版本的新规则。'
        with self.assertRaisesRegex(ValueError,'知识来源已变化'):
            dataset(value,self.path,changed,'v2')

    def test_identical_samples_merge_provenance(self):
        self.store.put('account-a',{'id':'chat-b','title':'重复问题','messages':self.messages,'expected_revision':0})
        value=self.review()
        other=prepare(self.path,'account-a',['chat-b'])
        value['conversations']+=other['conversations']
        for label in other['labels']:
            source=copy.deepcopy(value['labels'][label['answer_index']//2])
            source['conversation_id']='chat-b';value['labels'].append(source)
        output=dataset(value,self.path,{},'v1')
        self.assertEqual(len(output['samples']),2)
        self.assertTrue(all(len(s['provenance'])==2 for s in output['samples']))

    def test_cli_prepare_review_export_is_readable_and_uses_only_selected_history(self):
        script=Path(__file__).resolve().parents[1]/'legacy/training_data.py'
        staged=self.root/'review.json';output=self.root/'cli-dataset'
        result=subprocess.run([sys.executable,'-B',str(script),'prepare','--history-db',str(self.path),
            '--owner','account-a','--conversation','chat-a','--output',str(staged)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        review=json.loads(staged.read_text(encoding='utf-8'))
        self.assertEqual(len(review['conversations']),1)
        self.assertIn('KB-014',review['knowledge_snapshot'])
        approved=self.review()
        review.update(use_for_training=True,reviewer='local-reviewer',labels=approved['labels'])
        staged.write_text(json.dumps(review,ensure_ascii=False),encoding='utf-8')
        result=subprocess.run([sys.executable,'-B',str(script),'export','--history-db',str(self.path),
            '--review',str(staged),'--output',str(output)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(sum(json.loads(result.stdout)['exported'].values()),2)
        self.assertNotIn('BAD_SECRET',(output/'provenance.jsonl').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
