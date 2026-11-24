"""每模块一本知识手册；原资料单独留档，章节编号只用于定位和追溯。"""
import copy
import json
import re
from knowledge_freshness import source_for_model
from pathlib import Path
from knowledge_catalog import LIBRARIES, LIBRARY_MAP

MANIFEST = 'planning/module-knowledge.json'
BOOKS = 'docs/module-knowledge'
BODY_FIELDS = ('historical_finding', 'historical_outcome')
MODEL_POLICY = ('knowledge_modules是每模块整合知识中按本题选出的相关章节，正文content与适用范围在各section内，id沿用来源编号；sources仅作引用索引。'
                '优先依据整合知识回答，不复述原始参考资料、来源行、文件路径或校验值。用户明确追问出处时再说明相关来源；成立条件和证据日期改变结论时仍须保留。'
                '正文的必要引用由界面按需展示，refs仍完整列出实际使用编号，不能丢失引用校验。'
                '追问出处时从reference_urls给出具体可点击的Markdown链接，逐项说明支持内容和适用限制；资料整理时核对不代表本轮重新联网。')


def clean_body(text):
    """移走独立来源行；代码、配置、否定条件和普通正文原样保留。"""
    lines, fence = [], None
    for line in text.splitlines():
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            marker = match.group(1)[0]
            fence = None if fence == marker else marker
        if fence is None and re.match(r'^\s*(?:来源|参考)[：:]', line):
            continue
        lines.append(line)
    # 原文已按主题整理；只删除完全相同的段落，不猜测不同条件的句子等价。
    result, seen, paragraph, fence, protected = [], set(), [], None, False
    def flush():
        value = '\n'.join(paragraph).strip()
        if value and (protected or value not in seen):
            result.append(value)
            seen.add(value)
    for line in lines:
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            protected = True
            marker = match.group(1)[0]
            fence = None if fence == marker else marker
        if not line.strip() and fence is None:
            flush()
            paragraph, protected = [], False
        else:
            paragraph.append(line)
    flush()
    return '\n\n'.join(result)


def primary_module(record):
    names = record.get('knowledge_libraries', [])
    for name in ('six_card', 'three_card', 'general_basics', 'croc_local',
                 'network_support', 'device_common', 'ha_system'):
        if name in names:
            return name
    raise ValueError('Record has no registered knowledge module')


def section_marker(ident, field, closing=False):
    return '<!-- %srecord:%s:%s -->' % ('/' if closing else '', ident, field)


def render_book(library, records):
    lines = ['# ' + library['title'], '',
             '本模块的日常知识入口。章节保留各自的模式、日期和适用条件；原始资料及出处单独存档。', '']
    if not records:
        lines += ['专属资料待核实，本模块尚无可用正文。', '']
    order = ['KB-001','KB-002','KB-008','KB-009','KB-010','KB-003','KB-004','KB-005',
             'KB-013','KB-014','KB-015','KB-016','KB-018','KB-017','KB-006','KB-007','KB-011','KB-012']
    def position(record):
        ident = record['id']
        if ident in order:
            return 0, order.index(ident)
        if ident.startswith('KB-'):
            return 0, int(ident[3:])+100
        return (2 if record.get('kind') == 'reviewed_experience' else 1), ident
    for record in sorted(records, key=position):
        lines += ['## ' + record['title'], '',
                  '适用模式：' + ('、'.join(record.get('operating_modes', [])) or '按正文说明') +
                  '；设备组：' + ('、'.join(record.get('device_groups', [])) or '按正文说明') +
                  '；证据日期：' + (record.get('evidence_date') or '无现场采集日期') + '。', '']
        for field, label in (('device_types','设备类型'), ('deployment_environments','部署环境'),
                             ('software_versions','软件版本')):
            if record.get(field):
                lines += [label+'：'+'、'.join(record[field])+'。', '']
        if record['id'].startswith('HA-'):
            lines += ['历史案例（'+record.get('curation_status','未登记审核状态')+'），不代表当前设备状态。',
                      record.get('verification_limits',''), '']
        elif record.get('kind') == 'reviewed_experience':
            lines += ['已核实经验补充，适用范围按本节条件；不代表当前设备状态。', '']
        for field in BODY_FIELDS:
            if record.get(field):
                lines += [section_marker(record['id'], field), record[field],
                          section_marker(record['id'], field, True), '']
    return '\n'.join(lines).rstrip() + '\n'


def compile_archive(root):
    """只在整理/更新时读取原始资料，生成七本手册及无正文的来源索引。"""
    from knowledge import load_archive_knowledge
    from knowledge_catalog import annotate
    from knowledge_coverage import record_coverage
    root = Path(root)
    knowledge, sources = load_archive_knowledge(root)
    cases = json.loads((root/'planning/cases.json').read_text(encoding='utf-8-sig'))['cases']
    for record in cases:
        annotate(record)
        record['knowledge_coverage'] = record_coverage(record)
    records = copy.deepcopy(cases + knowledge)
    metadata = []
    for record in records:
        record['module_id'] = primary_module(record)
        record['module_path'] = BOOKS + '/' + record['module_id'] + '.md'
        record['provenance_notes'] = [line for field in BODY_FIELDS
                                     for line in record.get(field, '').splitlines()
                                     if re.match(r'^\s*(?:来源|参考)[：:]', line)]
        for field in BODY_FIELDS:
            record[field] = clean_body(record.get(field, ''))
        meta = {key: value for key, value in record.items() if key not in BODY_FIELDS}
        meta['body_fields'] = [field for field in BODY_FIELDS if record[field]]
        metadata.append(meta)
    files = {BOOKS+'/'+library['id']+'.md': render_book(library, [r for r in records
             if library['id'] in r['knowledge_libraries']]) for library in LIBRARIES}
    manifest = {'schema_version': 1, 'architecture': 'module_handbooks_with_source_archive',
                'records': metadata, 'knowledge_sources': sources}
    files[MANIFEST] = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    return files


def load_corpus(root):
    """日常仅读整合手册，不再将原始资料作为问答正文入口。"""
    root = Path(root)
    manifest = json.loads((root/MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1:
        raise ValueError('Unsupported module knowledge schema')
    books, records, seen = {}, [], set()
    for metadata in manifest['records']:
        record = copy.deepcopy(metadata)
        ident, module = record['id'], record['module_id']
        if ident in seen or not re.fullmatch(r'(?:KB|HA)-\d+', ident) or module not in LIBRARY_MAP:
            raise ValueError('Invalid or duplicate module knowledge record')
        seen.add(ident)
        expected = BOOKS + '/' + module + '.md'
        if record['module_path'] != expected:
            raise ValueError('Invalid module knowledge path')
        if module not in books:
            books[module] = (root/expected).read_text(encoding='utf-8')
        fields = record.pop('body_fields')
        if set(fields) - set(BODY_FIELDS):
            raise ValueError('Invalid module knowledge fields')
        for field in BODY_FIELDS:
            record[field] = ''
            if field in fields:
                start, end = section_marker(ident, field), section_marker(ident, field, True)
                text = books[module]
                if text.count(start) != 1 or text.count(end) != 1:
                    raise ValueError('Missing or duplicate module knowledge section: ' + ident)
                begin, finish = text.index(start)+len(start), text.index(end)
                if finish < begin:
                    raise ValueError('Module knowledge section order is invalid: ' + ident)
                record[field] = text[begin:finish].strip()
                if not record[field]:
                    raise ValueError('Empty module knowledge section: ' + ident)
        records.append(record)
    return records, manifest['knowledge_sources']


def module_document(ident, records):
    if ident not in LIBRARY_MAP:
        raise ValueError('Unknown knowledge module')
    own = [record for record in records if ident in record.get('knowledge_libraries', [])]
    return {'id': ident, 'title': LIBRARY_MAP[ident]['title'],
            'content': render_book(LIBRARY_MAP[ident], own),
            'section_count': len(own), 'experience_count': sum(
                r.get('kind') == 'reviewed_experience' for r in own),
            'source_ids': [r['id'] for r in own], 'current_state_supported': False}


def model_modules(context):
    """模型按模块读取相关章节，后台仍用原编号校验引用及范围。"""
    view = copy.deepcopy(context)
    modules, others, refs = {}, [], []
    selected=context.get('sources', [])
    if (context.get('workflow_policy',{}).get('mode')=='knowledge'
            and context.get('response_contract',{}).get('prefer_plain_explanation')
            and context.get('knowledge_sufficiency',{}).get('status')=='supported'):
        allowed=set(context['knowledge_sufficiency']['supporting_refs'])
        selected=[s for s in selected if s['id'] in allowed]
    for original in selected:
        source = source_for_model(original, context.get('question', ''))
        if not source.get('knowledge_libraries'):
            others.append(copy.deepcopy(source))
            continue
        name = primary_module(source)
        module = modules.setdefault(name, {'id': name, 'title': LIBRARY_MAP[name]['title'], 'sections': []})
        hidden = set(BODY_FIELDS) | {'provenance_notes', 'verification_sources', 'reference_urls', 'sources','teaching_summary'}
        metadata = {key: copy.deepcopy(value) for key, value in source.items() if key not in hidden}
        if context.get('response_contract', {}).get('source_display') == 'expanded':
            metadata['provenance_notes'] = source.get('provenance_notes', [])
            metadata['reference_urls'] = source.get('reference_urls', [])
        plain=context.get('response_contract',{}).get('prefer_plain_explanation')
        content=(source.get('teaching_summary') if plain and source.get('kind')=='topology' else None)
        module['sections'].append(dict(metadata, content=clean_body(content or source.get('historical_finding', '')),
                                       outcome=clean_body(source.get('historical_outcome', ''))))
        refs.append({'id': source['id'], 'module_id': name})
    if modules:
        view['knowledge_modules'] = list(modules.values())
        view['sources'] = refs + others
    return view
