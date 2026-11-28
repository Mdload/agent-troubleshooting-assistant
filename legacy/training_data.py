"""选定账号历史→本地标注包→脱敏JSONL训练集；全程不调用模型或网络。"""
import argparse
import copy
import datetime
from difflib import SequenceMatcher
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from version import VERSION

ROOT = Path(__file__).resolve().parents[1]


def history_snapshot(path, owner, ids):
    """只读指定记录；不存在、已删除或属于其他账号都不导出。"""
    path = Path(path).resolve()
    if not path.is_file() or not isinstance(owner, str) or not owner or not ids:
        raise ValueError('需要已有历史数据库、明确账号和所选对话')
    if len(ids) > 50 or len(set(ids)) != len(ids):
        raise ValueError('所选对话重复或过多')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        result = []
        for ident in ids:
            if not isinstance(ident, str):
                raise ValueError('对话编号无效')
            row = db.execute('SELECT id,revision,messages FROM chat_histories '
                             'WHERE owner=? AND id=? AND deleted=0', (owner, ident)).fetchone()
            if row is None:
                raise ValueError('所选历史不可用，请核对账号、删除状态和对话编号')
            result.append({'id': row['id'], 'revision': row['revision'],
                           'messages': json.loads(row['messages'])})
        return result
    finally:
        db.close()


def knowledge_snapshot(record):
    return {key: copy.deepcopy(value) for key, value in record.items()
            if key in ('id', 'title', 'historical_finding', 'operating_modes', 'device_groups',
                       'software_versions', 'evidence_date', 'documentation_date', 'module_id')}


def prepare(path, owner, ids, knowledge_records=None, knowledge_version=None):
    conversations = history_snapshot(path, owner, ids)
    labels = []
    for conversation in conversations:
        messages = conversation['messages']
        for index, message in enumerate(messages):
            if message['role'] != 'assistant' or index == 0 or messages[index-1]['role'] != 'user':
                continue
            labels.append({'conversation_id': conversation['id'], 'question_index': index-1,
                           'answer_index': index, 'verdict': 'unreviewed', 'corrected_answer': '',
                           'review_note': '', 'source_refs': []})
    return {'schema_version': 1, 'kind': 'local_training_review',
            'source_kind': 'historical_browser_transcript', 'owner': owner,
            'use_for_training': False, 'reviewer': '',
            'knowledge_version': knowledge_version,
            'knowledge_snapshot': {ref: knowledge_snapshot(record) for ref, record in (knowledge_records or {}).items()},
            'conversations': conversations, 'labels': labels,
            'note': '本地原始材料；不是标准答案。核实后设置verified/corrected或exclude，填写审核说明与可选来源；明确本批训练用途后导出。'}


class Redactor:
    """同批占位符一致；自动处理常见凭据、邮箱、手机号、用户路径和IPv4。"""
    def __init__(self):
        self.values = {}
        self.counts = {}

    def placeholder(self, kind, value):
        key = kind, value
        if key not in self.values:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.values[key] = '<%s_%d>' % (kind, self.counts[kind])
        return self.values[key]

    def __call__(self, text):
        if not isinstance(text, str):
            raise ValueError('样本文字必须是字符串')
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', '<PRIVATE_KEY>', text)
        text = re.sub(r'(?i)\bBearer\s+[^\s\"\x27,;]+', 'Bearer <SECRET>', text)
        text = re.sub(r'(?i)((?:api[_-]?key|password|passwd|token|secret|密码|密钥)\s*[\"\x27]?\s*[:=]\s*[\"\x27]?)[^\s\"\x27,;，；]+', r'\1<SECRET>', text)
        text = re.sub(r'(?i)\bsk-[a-z0-9_-]{12,}', '<SECRET>', text)
        text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1<USER>:<SECRET>@', text)
        text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', lambda m: self.placeholder('EMAIL', m.group()), text)
        text = re.sub(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)', lambda m: self.placeholder('PHONE', m.group()), text)
        text = re.sub(r'(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s]+', lambda m: m.group(1)+'<USER>', text)
        def address(match):
            try:
                ipaddress.IPv4Address(match.group())
            except ipaddress.AddressValueError:
                return match.group()
            return self.placeholder('IP', match.group())
        return re.sub(r'(?<![A-Za-z0-9_.])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_.])', address, text)


def normalized_question(text):
    return re.sub(r'[\W_]+', '', text.lower())


def grouped_splits(samples, seed=42):
    """同一对话及字符近重复问题归同组，再切分，避免跨集合泄漏。"""
    parent = list(range(len(samples)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(left, right):
        parent[find(right)] = find(left)
    questions = [normalized_question(s['messages'][-2]['content']) for s in samples]
    for i, sample in enumerate(samples):
        conversations = {p['conversation_id'] for p in sample['provenance']}
        for j in range(i):
            same = bool(conversations & {p['conversation_id'] for p in samples[j]['provenance']})
            a, b = questions[i], questions[j]
            near = a == b
            if not same and not near and min(len(a), len(b)) >= 8:
                matcher = SequenceMatcher(None, a, b, autojunk=True)
                near = matcher.quick_ratio() >= .9 and matcher.ratio() >= .9
            if same or near:
                union(i, j)
    groups = list(dict.fromkeys(find(i) for i in range(len(samples))))
    random.Random(seed).shuffle(groups)
    count = len(groups)
    # 小数据允许验证/测试为空，不复制训练样本来凑数量。
    train_end = max(1, int(count*.8)) if count else 0
    validation_end = max(train_end, int(count*.9))
    assignment = {group: 'train' if i < train_end else 'validation' if i < validation_end else 'test'
                  for i, group in enumerate(groups)}
    for i, sample in enumerate(samples):
        sample['split'] = assignment[find(i)]
    return samples


def dataset(review, history_path, knowledge_records, knowledge_version, seed=42):
    if not isinstance(review, dict):
        raise ValueError('审核包必须是结构化对象')
    if (review.get('schema_version') != 1 or review.get('kind') != 'local_training_review'
            or review.get('use_for_training') is not True
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
        raise ValueError('需要本批训练用途和审核人记录')
    snapshots = review.get('conversations')
    if not isinstance(snapshots, list) or not snapshots or any(not isinstance(s, dict) or 'id' not in s for s in snapshots):
        raise ValueError('缺少所选历史快照')
    current = history_snapshot(history_path, review.get('owner'), [s['id'] for s in snapshots])
    if current != snapshots:
        raise ValueError('历史已修改、删除或快照被改动，请重新准备并审核')
    conversations = {s['id']: s for s in snapshots}
    labels, seen = {}, set()
    annotations = review.get('labels')
    if not isinstance(annotations, list) or any(not isinstance(label, dict) for label in annotations):
        raise ValueError('缺少问答审核标注')
    for label in annotations:
        ident, index, qindex = label.get('conversation_id'), label.get('answer_index'), label.get('question_index')
        if ident not in conversations or type(index) is not int or type(qindex) is not int:
            raise ValueError('标注未绑定所选对话消息')
        messages = conversations[ident]['messages']
        if (index < 1 or index >= len(messages) or qindex != index-1
                or messages[index]['role'] != 'assistant' or messages[qindex]['role'] != 'user'):
            raise ValueError('标注问答顺序无效')
        key = ident, index
        if key in seen:
            raise ValueError('重复标注')
        seen.add(key)
        if label.get('verdict') not in ('unreviewed', 'verified', 'corrected', 'exclude'):
            raise ValueError('无效审核结论')
        if label['verdict'] in ('verified', 'corrected'):
            if not isinstance(label.get('review_note'), str) or not label['review_note'].strip():
                raise ValueError('可训练答案需要核实说明')
            corrected = label.get('corrected_answer')
            if label['verdict'] == 'corrected' and (not isinstance(corrected, str) or not corrected.strip()):
                raise ValueError('纠正样本缺少正确答案')
            refs = label.get('source_refs')
            if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) or r not in knowledge_records for r in refs):
                raise ValueError('知识来源未知或已撤回')
            for ref in refs:
                if review.get('knowledge_snapshot', {}).get(ref) != knowledge_snapshot(knowledge_records[ref]):
                    raise ValueError('审核时的知识来源已变化或未留快照，请重新准备并核实')
        labels[key] = label
    redactor = Redactor()
    samples, deduplicated = [], {}
    for (ident, index), label in labels.items():
        if label['verdict'] not in ('verified', 'corrected'):
            continue
        snapshot = conversations[ident]
        messages = []
        for position, message in enumerate(snapshot['messages'][:index+1]):
            text = message['content']
            if message['role'] == 'assistant':
                earlier = labels.get((ident, position))
                if not earlier or earlier['verdict'] not in ('verified', 'corrected'):
                    raise ValueError('多轮上下文含未核实回答，须先审核或排除该样本')
                if earlier['verdict'] == 'corrected':
                    text = earlier['corrected_answer']
            messages.append({'role': message['role'], 'content': redactor(text)})
        if any(m['role'] != ('user' if i % 2 == 0 else 'assistant') or not m['content'].strip()
               for i, m in enumerate(messages)):
            raise ValueError('训练消息须为非空、交替的用户与助手问答')
        provenance = {'conversation_id': ident, 'history_revision': snapshot['revision'],
                      'question_index': index-1, 'answer_index': index,
                      'verdict': label['verdict'], 'review_note': redactor(label['review_note']),
                      'source_refs': label['source_refs'],
                      'original_answer': redactor(snapshot['messages'][index]['content'])}
        signature = tuple((m['role'], m['content']) for m in messages)
        if signature in deduplicated:
            deduplicated[signature]['provenance'].append(provenance)
        else:
            sample = {'id': 'sample-%06d' % (len(samples)+1), 'messages': messages, 'provenance': [provenance]}
            deduplicated[signature] = sample
            samples.append(sample)
    if not samples:
        raise ValueError('没有已核实且可用于训练的样本')
    if len(samples) > 2000:
        raise ValueError('本地导出每批最多2000条样本，请缩小所选范围')
    grouped_splits(samples, seed)
    # 相关章节保存文本快照，而不是靠版本号暗示本地工作副本完全一致。
    refs = list(dict.fromkeys(r for s in samples for p in s['provenance'] for r in p['source_refs']))
    knowledge = {ref: {k: redactor(value) if isinstance(value, str) else copy.deepcopy(value)
                      for k, value in knowledge_records[ref].items()
                      if k in ('id', 'title', 'historical_finding', 'operating_modes', 'device_groups',
                               'software_versions', 'evidence_date', 'documentation_date', 'module_id')}
                 for ref in refs}
    return {'schema_version': 1, 'kind': 'local_reviewed_training_dataset',
            'created_at_utc': datetime.datetime.utcnow().isoformat()+'Z',
            'knowledge_version': review.get('knowledge_version') or knowledge_version,
            'export_runtime_version': knowledge_version, 'knowledge_snapshot': knowledge,
            'source_kind': 'reviewed_historical_browser_transcript',
            'split_seed': seed, 'samples': samples,
            'redaction_counts': redactor.counts,
            'note': '本地训练材料，未运行模型训练；字符近重复分组不等于语义去重。导出副本需按本地数据规则管理。'}


def write_new(path, value, jsonl=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
        if jsonl:
            for row in value:
                handle.write(json.dumps(row, ensure_ascii=False)+'\n')
        else:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')


def export_dataset(value, output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for split in ('train', 'validation', 'test'):
        write_new(output/(split+'.jsonl'), [{'messages': s['messages']} for s in value['samples'] if s['split'] == split], True)
    line_numbers = {'train': 0, 'validation': 0, 'test': 0}
    provenance = []
    for sample in value['samples']:
        line_numbers[sample['split']] += 1
        provenance.append({'id': sample['id'], 'split': sample['split'],
                           'split_row': line_numbers[sample['split']], 'sources': sample['provenance']})
    write_new(output/'provenance.jsonl', provenance, True)
    manifest = {key: field for key, field in value.items() if key != 'samples'}
    manifest['counts'] = {split: sum(s['split'] == split for s in value['samples']) for split in ('train', 'validation', 'test')}
    manifest['complete'] = True
    write_new(output/'manifest.json', manifest)
    return manifest['counts']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    staging = commands.add_parser('prepare')
    staging.add_argument('--history-db', type=Path, required=True)
    staging.add_argument('--owner', required=True, help='服务器登记的账号归属ID，不是用户名')
    staging.add_argument('--conversation', action='append', required=True)
    staging.add_argument('--output', type=Path, required=True)
    exporter = commands.add_parser('export')
    exporter.add_argument('--history-db', type=Path, required=True)
    exporter.add_argument('--review', type=Path, required=True)
    exporter.add_argument('--output', type=Path, required=True)
    exporter.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            from retrieval.knowledge_modules import load_corpus
            records, _ = load_corpus(ROOT)
            value = prepare(args.history_db, args.owner, args.conversation, {r['id']: r for r in records},
                            VERSION)
            write_new(args.output, value)
            print(json.dumps({'staged_conversations': len(value['conversations']), 'unreviewed_answers': len(value['labels'])}))
        else:
            from retrieval.knowledge_modules import load_corpus
            records, _ = load_corpus(ROOT)
            review = json.loads(args.review.read_text(encoding='utf-8-sig'))
            value = dataset(review, args.history_db, {r['id']: r for r in records},
                            VERSION, args.seed)
            print(json.dumps({'exported': export_dataset(value, args.output), 'model_calls': 0}))
    except ValueError as error:
        parser.exit(1, '训练数据准备或导出失败：'+str(error)+'。没有外发数据。\n')
    except (OSError, KeyError, TypeError, sqlite3.Error):
        parser.exit(1, '训练数据准备或导出失败；请核对本地标注、来源、历史版本与输出路径。没有外发数据。\n')


if __name__ == '__main__':
    main()
