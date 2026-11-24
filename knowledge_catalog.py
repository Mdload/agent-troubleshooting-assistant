"""一份语料的逻辑分区。标签控制检索范围，不表示现场身份已确认。"""
import json
import re
from pathlib import Path
from lab_basics import is_basics_query, topic_ids

ROOT = Path(__file__).resolve().parent
REGISTRY = json.loads((ROOT / 'planning/knowledge-libraries.json').read_text(encoding='utf-8'))
LIBRARIES = REGISTRY['libraries']
LIBRARY_MAP = {item['id']: item for item in LIBRARIES}
METADATA_FIELDS = ('knowledge_libraries', 'device_types', 'deployment_environments',
                   'software_versions', 'version_status', 'source_scope')


def annotate(record):
    """现有章节只按有来源的职责归类，绝不推定六卡/三卡或云/物理机。"""
    ident = record['id']
    if record.get('kind') == 'lab_basics':
        libraries = ['general_basics']
        scope = 'public_principle'
    else:
        libraries = ['ha_system']
        if ident in ('KB-001', 'KB-002', 'KB-007', 'KB-009'):
            libraries.append('device_common')
        if ident in ('KB-005', 'KB-013', 'KB-014', 'KB-015', 'KB-016', 'KB-018', 'HA-003', 'HA-008'):
            libraries.append('croc_local')
        if ident == 'KB-003':
            libraries.append('network_support')
        scope = 'historical_snapshot'
    record.update(knowledge_libraries=libraries, device_types=[],
                  deployment_environments=[], software_versions=[],
                  version_status='principle' if scope == 'public_principle' else 'unconfirmed',
                  source_scope=scope)
    # 后续核实设备后，管理员可按编号登记标签；归类不会生成或替换正文。
    metadata = REGISTRY.get('record_metadata', {}).get(ident, {})
    if set(metadata) - set(METADATA_FIELDS):
        raise ValueError('Unknown knowledge metadata field')
    if any(name not in LIBRARY_MAP for name in metadata.get('knowledge_libraries', [])):
        raise ValueError('Unknown knowledge library')
    record.update(metadata)
    return record


def catalog(records):
    result = []
    for library in LIBRARIES:
        own = [r for r in records if library['id'] in r.get('knowledge_libraries', [])]
        docs = [r for r in own if not r['id'].startswith('HA-')]
        cases = [r for r in own if r['id'].startswith('HA-')]
        environments = {env: sum(env in r.get('deployment_environments', []) for r in own)
                        for env in library.get('deployment_environments', [])}
        result.append(dict(library, document_count=len(docs), historical_case_count=len(cases),
                           record_count=len(own), state='has_material' if own else 'awaiting_material',
                           module_count=1 if own else 0,
                           module_path='docs/module-knowledge/'+library['id']+'.md',
                           environment_counts=environments))
    return {'schema_version': 1, 'architecture': 'module_handbooks_with_source_archive',
            'unique_record_count': len(records), 'libraries': result,
            'note': '同一章节可属于多个分区，语料不重复。计划主题不是已收录资料；数量不是回答准确率。'}


def _types(text):
    result = []
    for name, pattern in (('six_card', r'(?<![零〇一二三四五六七八九十百千万\d])(?:六|6\s*|六张|6\s*张)卡'),
                          ('three_card', r'(?<![零〇一二三四五六七八九十百千万\d])(?:三|3\s*|三张|3\s*张)卡')):
        if re.search(pattern, text):
            result.append(name)
    return result


def query_scope(question, library=None):
    """追问显式指定新设备时以追问为准；没有明确类型就不猜映射。"""
    if library is not None and library not in LIBRARY_MAP:
        raise ValueError('Unknown knowledge library')
    q = question.lower()
    tail = q.rsplit('\n追问：', 1)[-1]
    types = _types(tail) or _types(q)
    local = '局端' in q
    env_text = tail if any(t in tail for t in ('云服务器', '云端', '物理机', '实体机')) else q
    environments = []
    if local and any(t in env_text for t in ('云服务器', '云端')):
        environments.append('cloud')
    if local and any(t in env_text for t in ('物理机', '实体机')):
        environments.append('physical')
    versions = re.findall(r'(?<![a-z0-9])v\d+(?:\.\d+)+(?![a-z0-9])', tail)
    versions += ['v'+value for value in re.findall(r'(?:版本\s*[:：]?\s*|croc\s*)(\d+(?:\.\d+)+)', tail)]
    if not (types or local or 'croc' in q):
        versions = []
    selected = list(types)
    if local and (environments or types or versions):
        selected.append('croc_local')
    if versions and not selected:
        selected.append('device_common')
    if library:
        selected = [library]
        if library in ('six_card', 'three_card'):
            if types and library not in types:
                raise ValueError('Knowledge library conflicts with device type')
            types = [library]
    active = bool(types or environments or versions or library)
    allowed = list(dict.fromkeys(selected + (['general_basics', 'device_common'] if active else [])))
    return {'active': active, 'selected_libraries': list(dict.fromkeys(selected)),
            'allowed_libraries': allowed, 'device_types': types,
            'deployment_environments': environments, 'software_versions': list(dict.fromkeys(versions)),
            'public_topic_ids': topic_ids(question) if is_basics_query(question) else []}


def allows(record, scope):
    if not scope['active']:
        return True
    if not set(record.get('knowledge_libraries', [])) & set(scope['allowed_libraries']):
        return False
    public_id=record.get('reference_id') if record.get('kind')=='reviewed_experience' else record.get('id')
    if 'general_basics' in record.get('knowledge_libraries', []) and public_id not in scope['public_topic_ids']:
        return False
    # 通用原理可以补充解释；空标签的历史具体配置不代表适用于所有设备。
    for field in ('device_types', 'deployment_environments', 'software_versions'):
        if scope[field]:
            values = record.get(field, [])
            if values and not set(scope[field]) & set(values):
                return False
            if not values and record.get('source_scope') != 'public_principle':
                return False
    return True


def filter_sources(sources, scope):
    return [s for s in sources if allows(s, scope)]


def scope_gaps(scope, sources):
    if not scope['active']:
        return []
    gaps = []
    for name in scope['selected_libraries']:
        # 通用基础命中不代表设备专属资料已经入库。
        if not any(name in s.get('knowledge_libraries', []) for s in sources):
            gaps.append(LIBRARY_MAP[name]['title'] + '分区未找到适用资料')
    if scope['deployment_environments'] and not any(s.get('source_scope') != 'public_principle' for s in sources):
        labels = {'cloud': '云服务器', 'physical': '物理机'}
        gaps.append('缺少适用于' + '、'.join(labels[e] for e in scope['deployment_environments']) + '的局端配置依据')
    if scope['software_versions'] and not any(s.get('source_scope') != 'public_principle' for s in sources):
        gaps.append('所问软件版本尚无直接资料：' + '、'.join(scope['software_versions']))
    return gaps


def describe_scope(scope, sources):
    return dict(scope, selected_titles=[LIBRARY_MAP[n]['title'] for n in scope['selected_libraries']],
                gaps=scope_gaps(scope, sources), current_state_supported=False)
