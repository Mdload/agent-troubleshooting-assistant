"""所选对话纠正→待审经验→显式审核→可检索候选；不微调、不自动改线上知识。"""
import argparse
import collections
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
MODES = ('normal_ha', 'fixed_primary_test', 'maintenance')
GROUPS = ('local', 'remote', 'router')


def utc_now():
    return datetime.datetime.utcnow().isoformat()+'Z'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def safe_text(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Invalid or oversized text')
    if re.search(r'(?i)-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)|\bBearer\s+\S+|'
                 r'(?:api[_-]?key|password|passwd|token|密钥|密码)\s*[:=]\s*\S+', value):
        raise ValueError('Potential credential detected; redact before import')
    return value.strip()


def source_snapshot(source, root=ROOT):
    # 只读明确指定的本地知识文档，不能借来源路径读取私密配置。
    relative = source.get('path')
    if not isinstance(relative, str): raise ValueError('Missing source path')
    path = (root/relative).resolve()
    docs = (root/'docs').resolve()
    if docs not in path.parents or path.suffix.lower() != '.md':
        raise ValueError('Source must be a local docs Markdown file')
    quote = safe_text(source.get('quote'), 500)
    text = path.read_text(encoding='utf-8-sig')
    if quote not in text: raise ValueError('Source quote not found')
    return {'path': path.relative_to(root.resolve()).as_posix(), 'quote': quote,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_conditions(value):
    if not isinstance(value, dict): raise ValueError('Missing applicability conditions')
    if set(value) != {'lesson_type', 'operating_modes', 'device_groups', 'software_versions', 'evidence_date'}:
        raise ValueError('Unknown or missing applicability field')
    if value['lesson_type'] not in ('principle', 'historical_case'):
        raise ValueError('Transient live state is not reusable knowledge')
    for field, allowed in (('operating_modes', MODES), ('device_groups', GROUPS)):
        if not isinstance(value[field], list) or any(v not in allowed for v in value[field]):
            raise ValueError('Invalid applicability value')
    versions = value['software_versions']
    if not isinstance(versions, list) or any(not isinstance(v, str) or not re.fullmatch(r'v?\d+(?:\.\d+){1,3}', v) for v in versions):
        raise ValueError('Invalid software versions')
    if value['lesson_type'] == 'historical_case':
        try: datetime.datetime.strptime(value['evidence_date'], '%Y-%m-%d')
        except (ValueError, TypeError): raise ValueError('Historical case needs evidence date')
    elif value['evidence_date'] is not None:
        raise ValueError('Principle has no live collection date')
    return copy.deepcopy(value)


def collect(export, root=ROOT):
    """只提取用户选择且同意的纠正，丢弃原回答正文和其他未选消息。"""
    drafts, by_hash = [], {}
    seen_conversations = set()
    for conversation in export.get('conversations', []):
        if conversation.get('consent_to_extract') is not True: continue
        ident = safe_text(conversation.get('id'), 100)
        if ident in seen_conversations: raise ValueError('Duplicate conversation ID')
        seen_conversations.add(ident)
        messages = conversation.get('messages', [])
        by_id = {message['id']: message for message in messages}
        if len(by_id) != len(messages): raise ValueError('Duplicate message ID')
        positions = {message['id']: index for index, message in enumerate(messages)}
        for feedback in conversation.get('feedback', []):
            if feedback.get('verdict') not in ('resolved', 'corrected'): continue
            question = by_id.get(feedback.get('question_id'), {})
            answer = by_id.get(feedback.get('answer_id'), {})
            if question.get('role') != 'user' or answer.get('role') != 'assistant':
                raise ValueError('Feedback must bind user question and assistant answer')
            qpos, apos = positions[question['id']], positions[answer['id']]
            if apos <= qpos or any(message.get('role') == 'user' for message in messages[qpos+1:apos]):
                raise ValueError('Answer does not belong to selected question')
            payload = {'question': safe_text(question.get('content'), 1000),
                       'candidate_answer': safe_text(feedback.get('corrected_answer')),
                       'correction_reason': safe_text(feedback.get('reason'), 1000),
                       'conditions': validate_conditions(feedback.get('conditions')),
                       'verification_sources': [source_snapshot(source, root) for source in feedback.get('verification_sources', [])]}
            # 用摘要追溯原回答；不保存或信任原模型输出的正确性。
            provenance = {'conversation_id': ident, 'question_id': question['id'], 'answer_id': answer['id'],
                          'original_answer_sha256': fingerprint(answer.get('content', ''))}
            candidate_hash = fingerprint(payload)
            if candidate_hash in by_hash:
                by_hash[candidate_hash]['provenance'].append(provenance)
                continue
            item = dict(payload, id='EXP-'+candidate_hash[:16], candidate_hash=candidate_hash,
                        status='needs_review', review=None, provenance=[provenance],
                        created_at_utc=utc_now(), current_state_supported=False, grants_permissions=False)
            drafts.append(item)
            by_hash[candidate_hash] = item
    return {'schema_version': 1, 'kind': 'conversation_experience_drafts', 'items': drafts}


def candidate_payload(item):
    return {key: item[key] for key in ('question', 'candidate_answer', 'correction_reason',
                                      'conditions', 'verification_sources')}


def valid_candidate(item, root=ROOT):
    if fingerprint(candidate_payload(item)) != item['candidate_hash']:
        raise ValueError('Candidate changed; requires a new import and review')
    safe_text(item['question'], 1000)
    safe_text(item['candidate_answer'])
    validate_conditions(item['conditions'])
    for source in item['verification_sources']:
        if source_snapshot(source, root) != source: raise ValueError('Source changed; review again')


def review(store, ident, decision, reviewer, expected_hash, note, root=ROOT):
    if decision not in ('approve', 'reject'): raise ValueError('Invalid review decision')
    reviewer, note = safe_text(reviewer, 100), safe_text(note, 1000)
    result = copy.deepcopy(store)
    matches = [item for item in result['items'] if item['id'] == ident]
    if len(matches) != 1: raise ValueError('Unknown or duplicate candidate')
    item = matches[0]
    if item['status'] != 'needs_review' or item['candidate_hash'] != expected_hash:
        raise ValueError('Review state or hash mismatch')
    valid_candidate(item, root)
    if decision == 'approve' and not item['verification_sources']:
        raise ValueError('Approval needs verified source excerpts')
    item['status'] = 'approved' if decision == 'approve' else 'rejected'
    item['review'] = {'reviewer': reviewer, 'decision': decision, 'note': note,
                      'candidate_hash': expected_hash, 'reviewed_at_utc': utc_now()}
    return result


def approved_records(store, root=ROOT):
    result = []
    for item in store.get('items', []):
        if item.get('status') != 'approved': continue
        valid_candidate(item, root)
        review_data = item.get('review') or {}
        if (review_data.get('decision') != 'approve' or not review_data.get('reviewer') or
            review_data.get('candidate_hash') != item['candidate_hash'] or not item['verification_sources']):
            raise ValueError('Approved item missing matching review')
        result.append(item)
    return result


def retrieve(store, question, mode, group, version=None, root=ROOT):
    """独立原型检索，只查已审核且适用条件明确匹配的经验，不接工具权限。"""
    from qa import tokens
    query = set(tokens(question))
    candidates = []
    for item in approved_records(store, root):
        scope = item['conditions']
        if scope['operating_modes'] and mode not in scope['operating_modes']: continue
        if scope['device_groups'] and group not in scope['device_groups']: continue
        if scope['software_versions'] and version not in scope['software_versions']: continue
        doc = collections.Counter(tokens(item['question']+' '+item['candidate_answer']))
        if len(query & set(doc)) >= 2: candidates.append((item, doc))
    if not candidates: return []
    average = sum(sum(doc.values()) for _, doc in candidates)/len(candidates)
    frequencies = collections.Counter(token for _, doc in candidates for token in doc)
    hits = []
    for item, doc in candidates:
        score = 0
        for token in query & set(doc):
            idf = math.log(1+(len(candidates)-frequencies[token]+0.5)/(frequencies[token]+0.5))
            score += idf*doc[token]*2.2/(doc[token]+1.2*(0.25+0.75*sum(doc.values())/average))
        hits.append({'id': item['id'], 'answer': item['candidate_answer'], 'score': round(score, 4),
                     'conditions': item['conditions'], 'verification_sources': item['verification_sources'],
                     'current_state_supported': False, 'grants_permissions': False})
    return sorted(hits, key=lambda item: item['score'], reverse=True)[:3]


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    importer = commands.add_parser('import')
    importer.add_argument('input', type=Path)
    importer.add_argument('output', type=Path)
    reviewer = commands.add_parser('review')
    reviewer.add_argument('input', type=Path)
    reviewer.add_argument('output', type=Path)
    reviewer.add_argument('--id', required=True)
    reviewer.add_argument('--decision', choices=('approve', 'reject'), required=True)
    reviewer.add_argument('--reviewer', required=True)
    reviewer.add_argument('--expected-hash', required=True)
    reviewer.add_argument('--note', required=True)
    publisher = commands.add_parser('export')
    publisher.add_argument('input', type=Path)
    publisher.add_argument('output', type=Path)
    searcher = commands.add_parser('search')
    searcher.add_argument('input', type=Path)
    searcher.add_argument('question')
    searcher.add_argument('--mode', choices=('unknown',)+MODES, default='unknown')
    searcher.add_argument('--group', choices=('unknown',)+GROUPS, default='unknown')
    searcher.add_argument('--version')
    args = parser.parse_args()
    if args.command is None: parser.error('Choose import, review, export or search')
    store = json.loads(args.input.read_text(encoding='utf-8-sig'))
    if args.command == 'import': result = collect(store)
    elif args.command == 'review':
        result = review(store, args.id, args.decision, args.reviewer, args.expected_hash, args.note)
    elif args.command == 'export':
        result = {'kind': 'reviewed_knowledge_candidate_not_deployed', 'items': approved_records(store),
                  'requires_runtime_integration': True}
    else:
        print(json.dumps(retrieve(store, args.question, args.mode, args.group, args.version), ensure_ascii=False))
        return
    write_new(args.output, result)
    print(json.dumps({'items': len(result['items']), 'output': str(args.output), 'model_calls': 0}, ensure_ascii=True))


if __name__ == '__main__':
    main()
