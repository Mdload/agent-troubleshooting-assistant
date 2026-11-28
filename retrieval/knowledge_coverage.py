"""按已审阅章节判断资料范围；规则覆盖判断不是通用语义验证器。"""
import re
from legacy.learning_intent import croc_concept_question, public_learning_topic
import retrieval.topology_entities as entities
from retrieval.lab_basics import BY_ID, basic_requirements, coverage as basic_coverage, is_basics_query, is_wiring_query, topic_ids
from retrieval.knowledge_catalog import query_scope, allows, scope_gaps

# 每个编号只登记原文明确覆盖的主题/内容。目录、提到某程序或历史案例
# 不能自动成为该程序的配置、操作手册或本轮观测。
SUPPORT = {
    'KB-001': {'business_path': ['mechanism'], 'management': ['mechanism']},
    'KB-002': {'business_path': ['configuration'],
               'croc': ['mechanism', 'configuration', 'ports']},
    'KB-003': {'router_ha': ['mechanism', 'configuration']},
    'KB-004': {'remote_ha': ['mechanism', 'configuration'], 'ppp': ['mechanism', 'configuration'],
               'startup': ['mechanism']},
    'KB-005': {'local_ha': ['mechanism', 'configuration']},
    'KB-006': {'fixed_test': ['mechanism', 'configuration']},
    'KB-007': {'read_only': ['mechanism', 'checklist']},
    'KB-008': {'inventory': ['history']},
    'KB-009': {'startup': ['mechanism', 'paths'], 'croc': ['paths']},
    'KB-010': {'local_ha': ['configuration'], 'remote_ha': ['configuration'],
               'router_ha': ['configuration']},
    'KB-011': {'history': ['history', 'mechanism']},
    'KB-012': {},
    'KB-013': {'local_ha': ['mechanism', 'configuration']},
    'KB-014': {'local_health': ['mechanism', 'configuration', 'checklist'],
               'local_ha': ['mechanism', 'checklist']},
    'KB-015': {'local_watchdog': ['mechanism', 'configuration', 'checklist']},
    'KB-016': {'local_ha': ['mechanism', 'checklist']},
    'KB-017': {'business_path': ['mechanism', 'checklist'],
               'tool_scope': ['mechanism', 'checklist']},
    'KB-018': {'recovery_time': ['mechanism']},
}
HISTORY_TOPICS = {
    'HA-001': 'remote_ha', 'HA-002': 'remote_ha', 'HA-003': 'local_ha',
    'HA-004': 'remote_ha', 'HA-005': 'fixed_test', 'HA-006': 'fixed_test',
    'HA-007': 'ppp', 'HA-008': 'local_ha',
    'HA-009': 'business_path', 'HA-010': 'ppp', 'HA-011': 'ppp',
    'HA-012': 'management', 'HA-013': 'business_path',
}
# 混合设备章节的空模式列表不能把正常HA启动规则推广到其他模式。
TOPIC_MODES = {
    'KB-008': {'inventory': ['normal_ha']},
    'KB-009': {'startup': ['normal_ha']},
    'KB-010': {'local_ha': ['normal_ha'], 'remote_ha': ['normal_ha'], 'router_ha': ['normal_ha']},
}
TOPIC_LABELS = {
    'business_path': '业务路径', 'management': '管理与业务链的区别',
    'croc': 'Croc内外层及程序', 'router_ha': 'Router主备',
    'remote_ha': '远端主备与候选链路', 'local_ha': '局端主备',
    'named_entity':'所问服务或脚本的已记录职责',
    'startup': '启动关系', 'fixed_test': '固定主机测试模式',
    'read_only': '只读检查边界', 'inventory': '设备/程序历史清单',
    'history': '历史冲突及日志时钟', 'local_health': '局端健康规则',
    'local_watchdog': '局端watchdog', 'tool_scope': '工具覆盖',
    'recovery_time': '恢复耗时', 'ppp': 'PPP', 'wiring': '完整接线/VLAN',
    'operator': '运营商与接口映射', 'algorithm': '算法91实现',
    'application': 'Node/nginx/MySQL业务关系', 'unknown_service': '所问服务',
}
ASPECT_LABELS = {
    'mechanism': '机制说明', 'configuration': '具体配置', 'ports': '监听端口',
    'paths': '程序/脚本路径', 'checklist': '只读核对项目',
    'procedure': '完整操作步骤及适用前提', 'history': '历史记录',
    'measurement': '实测结果', 'implementation': '源码与实现依据',
    'wiring': '接线和VLAN端口表', 'mapping': '模组/接口/运营商对应表',
    'examples': '基础命令与示例操作',
}
MODE_LABELS = {'normal_ha': '正常HA', 'fixed_primary_test': '固定主机测试模式',
               'maintenance': '维护模式'}
GAP_TOPICS = {'KB-012': ['wiring', 'operator', 'algorithm', 'application'],
              'KB-018': ['recovery_time']}


def record_coverage(record):
    if record.get('kind')=='reviewed_experience' and re.fullmatch(r'EXP-[a-f0-9]{32}',record.get('id','')):
        return record['knowledge_coverage']
    if record.get('kind') == 'lab_basics':
        return basic_coverage(record)
    ident = record['id']
    support = SUPPORT.get(ident, {})
    if ident in HISTORY_TOPICS:
        support = {HISTORY_TOPICS[ident]: ['history']}
    return {'supports': support, 'topic_modes': TOPIC_MODES.get(ident, {}),
            'gap_topics': GAP_TOPICS.get(ident, []),
            'scope': 'historical_example' if ident.startswith('HA-') else 'documented',
            'current_state_supported': False}


def is_comparison(question):
    return (any(t in question.lower() for t in
               ('区别', '差别', '分别', '各自', '分不清', '对比', '比较', '分工', '职责', '关系', '同一', '一样', '相同'))
            or bool(re.search(r'软件.{0,20}还是.{0,20}(?:线|硬件)',question))
            or '打包' in question and '拆包' in question)


def requirements(question, mode='unknown', group='unknown'):
    """显式对象优先；没识别出主题时保持未评估，不借普通词判充分。"""
    q = question.lower().replace('备用机', '备机')
    comparing = is_comparison(q)
    groups = [name for word, name in (('局端', 'local'), ('远端', 'remote'),
              ('router', 'router'), ('路由器', 'router')) if word in q]
    groups = list(dict.fromkeys(groups)) or ([] if group == 'unknown' else [group])
    if mode == 'unknown':
        mode = ('maintenance' if '维护模式' in q else
                'fixed_primary_test' if any(t in q for t in ('固定主机', '固定测试'))
                and '正常ha' not in q else 'normal_ha')
    topics = []
    if croc_concept_question(question):
        topics=['croc']
    elif entities.overview_requested(question):
        tail=q.rsplit('\n追问：',1)[-1]
        roles=[topic for words,topic in ((('router','路由器'),'router_ha'),(('远端',),'remote_ha'),(('局端',),'local_ha'))
               if any(word in tail for word in words)]
        topics=['business_path']+(roles or ['router_ha','remote_ha','local_ha'])
    elif re.search(r'新服务|新程序|服务[ab]\b', q):
        topics = ['unknown_service']
    elif any(t in q for t in ('运营商', '哪张卡', 'sim', '模组映射')):
        topics = ['operator']
    elif is_wiring_query(q) or any(t in q for t in ('完整接线', '物理接线', '交换机', 'vlan端口', '插在哪')):
        topics = ['wiring']
    elif any(t in q for t in ('算法91', '算法 91', 'schtype', '调度算法', '源码')):
        topics = ['algorithm']
    elif any(t in q for t in ('mysql', 'nginx', 'node')):
        topics = ['application']
    elif any(t in q for t in ('实测', '多少秒', '几秒', '恢复时间', '恢复耗时')):
        topics = ['recovery_time']
    else:
        if any(t in q for t in ('业务路径', '业务怎么走', '数据怎么走', '数据流', '经过哪些', '业务流量', '业务验证', '端到端')):
            topics.append('business_path')
        # 同问“候选门禁能否证明端到端”需要两组前提；路径概念不能吞掉所指脚本的机制依据。
        if 'business_path' in topics and any('remote' in item['groups'] and item['name'].lower() in
               ('remote-ha-candidate.sh','remote-ha-health.sh') for item in entities.matches(question)):
            topics.append('remote_ha')
        if any(t in q for t in ('ssh能', '管理链', '管理可达')):
            topics.append('management')
        if any(t in q for t in ('固定主机', '固定测试')):
            topics.append('fixed_test')
        if 'watchdog' in q or 'recv-q' in q or '监听队列' in q or '接收队列' in q:
            topics.append('local_watchdog' if 'remote' not in groups else 'remote_ha')
        elif any(t in q for t in ('croc-ha-health', 'max_owner_failures', 'fall', 'rise', '健康脚本', '健康判据')):
            topics.append('remote_ha' if 'remote' in groups else 'local_health')
            if 'remote' not in groups and re.search(r'(?<![a-z0-9_])(?:fall|rise)(?![a-z0-9_])', q):
                topics.append('local_ha')
        if any(t in q for t in ('只读', '健康检查脚本')):
            topics.append('read_only')
        if any(t in q for t in ('工具覆盖', '工具范围', '能读取哪些')):
            topics.append('tool_scope')
        if any(t in q for t in ('rc.local', '启动链', '谁负责启动', '从哪里启动')):
            topics.append('startup')
        if any(t in q for t in ('日志时间', '对齐日志', '时钟')):
            topics.append('history')
        elif any(t in q for t in ('历史故障', '历史案例')):
            topics.extend([{'local': 'local_ha', 'remote': 'remote_ha', 'router': 'router_ha'}[g]
                           for g in groups] or (['ppp'] if 'ppp' in q else ['history']))
        if any(t in q for t in ('设备清单', '哪些程序', '跑什么程序', '历史主机')):
            topics.append('inventory')
        # 比较问题需要每个显式机制的依据；先命中watchdog等主题不能吞掉另一方。
        if comparing:
            resource_relation = ('croc' in q and 'vip' in q and
                                 any(t in q for t in ('服务', '启停', '启动', '停止', '归属', '持有')))
            if resource_relation or any(t in q for t in ('keepalived', 'vrrp')):
                ha_groups = groups or (['local'] if 'local_watchdog' in topics else [])
                topics.extend({'local': 'local_ha', 'remote': 'remote_ha', 'router': 'router_ha'}[g]
                              for g in ha_groups)
            if 'croc' in q and not resource_relation and not any(t in topics for t in ('local_watchdog', 'local_health')):
                topics.append('croc')
            if 'router' in groups:
                topics.append('router_ha')
            if any(t in q for t in ('croc-ha-health', '健康脚本', '健康判据')):
                topics.append('remote_ha' if 'remote' in groups else 'local_health')
        if not topics:
            if any(t in q for t in ('端口', '监听什么', '监听哪个', '程序路径', '二进制', '脚本路径', '安装在哪')) and 'croc' in q:
                topics = ['croc']
            elif ('croc' in q and any(t in q for t in ('主备','主机','备机')) and
                  any(t in q for t in ('停止','启动','启停','不运行','没运行','未运行','持有'))):
                # 未明确端时分别列出两端依据，不能当成内外层原理，也不能猜是哪一端。
                topics = [{'local':'local_ha','remote':'remote_ha','router':'router_ha'}[g]
                          for g in (groups or ['local','remote'])]
            elif groups and any(t in q for t in ('主备', '切换', '接管', 'vip', 'croc', 'keepalived', '心跳', '健康', '选举', '优先级', '降权', '回主', '抢占')):
                topics = [{'local': 'local_ha', 'remote': 'remote_ha', 'router': 'router_ha'}[g] for g in groups]
            elif any(t in q for t in ('croc', 'tun16', '192.0.2.15', '192.0.2.25')):
                topics = ['croc']
            elif 'ppp' in q:
                topics = ['ppp']
            elif any(t in q for t in ('我们的服务', '实验室服务', '本项目服务')):
                topics = ['unknown_service']
    aspects = []
    if not topics and re.search(r'数据库名|用户名|账号|口令|密码|密钥',q):
        topics=['unknown_service']
        aspects=['configuration']
    if any(t in q for t in ('端口', '监听什么', '监听哪个')):
        aspects.append('ports')
    if any(t in q for t in ('程序路径', '脚本路径', '二进制', '安装在哪')):
        aspects.append('paths')
    # “谁负责启动”询问关系；只移除这段措辞，保留同问中的操作需求。
    procedure_question = q.replace('谁负责启动', '')
    if re.search(r'(怎么|如何|怎样|步骤|命令|操作说明).*(重启|启动|停止|操作|恢复|退出)|'
                 r'(重启|启动|停止|退出).*(步骤|命令)|手动|操作步骤', procedure_question):
        if '谁负责启动' in q:
            aspects.append('mechanism')
        aspects.append('procedure')
    elif re.search(r'(怎么|如何|怎样).*(检查|排查|核对|判断|读取|确认)|检查清单', q):
        aspects.append('checklist')
    if any(t in q for t in ('实测', '多少秒', '几秒')):
        aspects.append('measurement')
    if not aspects and any(t in q for t in ('哪个接口', '哪条接口', '心跳', '优先级', '配置值', '阈值', '多少个vip', '哪些vip', '地址', '配置参数')):
        aspects.append('configuration')
    if not aspects and any(t in q for t in ('历史', '以前', '当时')):
        aspects.append('history')
    aspects = aspects or ['mechanism']
    special = {'wiring': 'wiring', 'operator': 'mapping', 'algorithm': 'implementation'}
    result = []
    for topic in list(dict.fromkeys(topics)):
        # 端与主题绑定，不能拿局端规则回答远端；共享内外层章节不限端。
        own_group = {'local_ha': 'local', 'local_health': 'local',
                     'local_watchdog': 'local', 'remote_ha': 'remote', 'router_ha': 'router'}.get(topic)
        for aspect in ([special[topic]] if topic in special else aspects):
            result.append({'topic': topic, 'aspect': aspect, 'mode':
                'fixed_primary_test' if topic == 'fixed_test' else mode,
                # Croc机制描述的是隧道端点；比较Router不把端点资料限定成Router配置。
                'groups': [own_group] if own_group else ([] if topic == 'croc' and comparing else groups)})
    return result


def supports(source, need):
    if need['topic']=='named_entity':
        if need['mode']!='normal_ha':return False
        if need.get('groups') and need.get('documented_groups') and not set(need['groups'])<=set(need['documented_groups']):return False
        aliases=entities.record_entities(source)
        named=need.get('entity','').lower()
        return (need['aspect']=='mechanism' and (named in aliases or source.get('id') in need.get('context_refs',[])) and
                (not source.get('operating_modes') or need['mode'] in source['operating_modes']))
    info = record_coverage(source)
    if need['aspect'] not in info['supports'].get(need['topic'], []):
        return False
    modes = info['topic_modes'].get(need['topic'], source.get('operating_modes', []))
    groups = source.get('device_groups', [])
    return (not modes or need['mode'] in modes) and (
        not groups or not need['groups'] or set(need['groups']) <= set(groups))


def assessment_query(question, retrieval_hint=None, basics=False, reference_entities=()):
    """当前问题决定要回答什么；只在省略主题时借历史补全对象。"""
    if basics and retrieval_hint and public_learning_topic(retrieval_hint):
        # active/日志等例子中的词不能把公开重试主题改成systemd/日志教程。
        return retrieval_hint
    current_needs=requirements(question) if not basics else []
    refers_back=bool(re.search(r'它|这个|该(?:脚本|程序|服务)|上述|上面',question))
    inherited_object=(not current_needs or all(n['topic']=='business_path' for n in current_needs) or refers_back)
    if reference_entities and inherited_object and not entities.matches(question):
        # 只补所指名称，不把旧问题的参数/操作要求一起带来。
        question=' '.join(reference_entities)+'\n'+question
    explicit = topic_ids(question) if basics else requirements(question) or entities.matches(question)
    return question if explicit else retrieval_hint or question


def assess(question, sources, mode='unknown', group='unknown', basics=False, scope=None, conceptual_context=False,
           allow_unassessed_context=False):
    scope = scope if scope is not None else query_scope(question)
    sources = [s for s in sources if allows(s, scope)]
    needs = basic_requirements(question) if basics or (scope['active'] and is_basics_query(question)) else requirements(question, mode, group)
    named=entities.matches(question)
    named_composite = (any(n['topic']=='business_path' for n in needs) and
        any('remote' in item['groups'] and item['name'].lower() in
            ('remote-ha-candidate.sh','remote-ha-health.sh') for item in named))
    if named and (not needs or named_composite):
        # 仅承认原文明示的身份/职责；完整操作、源码与测量不能由名称命中认证。
        aspect='procedure' if re.search(r'步骤|完整操作|重启操作|源码|实现细节|实测',question) else 'mechanism'
        needs.extend([{'topic':'named_entity','entity':item['name'].lower(),'aspect':aspect,
                'mode':('maintenance' if '维护模式' in question else 'fixed_primary_test' if '固定测试' in question or '固定主机' in question else 'normal_ha') if mode=='unknown' else mode,
                'groups':[g for word,g in [('局端','local'),('远端','remote'),('Router','router'),('路由器','router')] if word in question],
                'documented_groups':item['groups'],
                'context_refs':entities.context_refs(item) if set(item['source_ids']) & {s['id'] for s in sources} else []} for item in named])
    known = [s for s in sources if s.get('id') in SUPPORT or s.get('id') in HISTORY_TOPICS
             or (s.get('kind')=='reviewed_experience' and re.fullmatch(r'EXP-[a-f0-9]{32}',s.get('id','')))
             or (s.get('kind') == 'lab_basics' and s.get('id') in BY_ID)]
    supported, missing, refs = [], [], set()
    for need in needs:
        found = [s['id'] for s in known if supports(s, need)]
        item = dict(need, refs=found)
        (supported if found else missing).append(item)
        refs.update(found)
    relevant = [s['id'] for s in known if any(n['topic'] in record_coverage(s)['supports'] or
                n['topic'] in record_coverage(s)['gap_topics'] for n in needs)]
    if not sources:
        status = 'missing'
    elif not needs:
        status = 'unassessed'
    elif not missing:
        status = 'supported'
    elif supported or relevant:
        status = 'partial'
    else:
        status = 'missing'
    labels = dict(TOPIC_LABELS, **{'basic_' + ident: item[2] for ident, item in BY_ID.items()})
    gaps = [labels[n['topic']] +
            ('（' + MODE_LABELS.get(n['mode'], n['mode']) + '）' if n['mode'] != 'normal_ha' else '') +
            '：缺少' + ASPECT_LABELS.get(n['aspect'], '所问细节的直接依据') for n in missing]
    if not needs and not scope['active']:
        gaps = ['所问对象、适用模式或所需依据尚未明确，不能确认资料是否充分']
    partition_gaps = scope_gaps(scope, sources)
    gaps.extend(partition_gaps)
    if partition_gaps and status == 'supported':
        status = 'partial'
    if any(n['topic'] == 'unknown_service' for n in missing):
        gaps.insert(0, '缺少服务名称、所在设备及配置/启动入口')
    # 概念讲解可以引用同模块的已整理背景；不能由标签命中认证任意参数/操作。
    # 主问题仍须由原 requirements 逐项覆盖，缺项/实测不足记录不进入背景白名单。
    background = set()
    if conceptual_context and status == 'supported' and all(n['aspect']=='mechanism' for n in needs):
        groups={g for n in needs for g in n.get('groups',[])}
        modes={n['mode'] for n in needs}
        modules={s.get('module_id') for s in known if s['id'] in refs}
        libraries={lib for s in known if s['id'] in refs for lib in s.get('knowledge_libraries',[])}
        for source in known:
            info=record_coverage(source)
            same_module=(source.get('module_id') in modules or libraries.intersection(source.get('knowledge_libraries',[])))
            if (same_module and any('mechanism' in aspects for aspects in info['supports'].values())
                    and not info['gap_topics']
                    and (not source.get('device_groups') or not groups or groups.intersection(source['device_groups']))
                    and (not source.get('operating_modes') or modes.intersection(source['operating_modes']))):
                background.add(source['id'])
    unassessed_refs=[]
    if allow_unassessed_context and status=='unassessed' and not needs and not partition_gaps:
        requested_mode=('maintenance' if '维护模式' in question else 'fixed_primary_test'
                        if any(t in question for t in ('固定主机','固定测试')) else 'normal_ha') if mode=='unknown' else mode
        unassessed_refs=[s['id'] for s in known if record_coverage(s)['supports']
                         and not record_coverage(s)['gap_topics'] and not s['id'].startswith('HA-')
                         and (basics or s.get('kind')!='lab_basics')
                         and (not s.get('operating_modes') or requested_mode in s['operating_modes'])]
        if unassessed_refs:gaps=[]
    return {'status': status, 'method': 'curated_scope_rules', 'semantic_verified': False,
            'requirements': needs, 'supported': supported, 'missing': missing,
            'supporting_refs': sorted(refs | background), 'context_refs': sorted(background - refs),
            'unassessed_refs':sorted(unassessed_refs),
            'related_refs': sorted(set(relevant) - refs - background),
            'gaps': list(dict.fromkeys(gaps)), 'current_state_supported': False}


def notice(coverage):
    status = coverage['status']
    if status == 'supported':
        return ''
    words={'ports':'监听端口还不确定','procedure':'具体操作步骤还没有核实',
        'paths':'程序实际放在哪里还不确定','configuration':'实际配置还没有确认',
        'measurement':'还没有实际测试结果','wiring':'接线情况还没有确认',
        'mapping':'接口与设备的对应关系还没有确认','history':'之前发生过什么还不确定',
        'implementation':'程序内部具体怎么做还不确定','mechanism':'设备具体怎样配合还不能完全确定',
        'checklist':'具体应该查哪些项目还需要确认','examples':'具体命令和操作例子还没有核实'}
    items=list(dict.fromkeys(words[n['aspect']] for n in coverage.get('missing',[]) if n.get('aspect') in words))
    return '；'.join(items[:3])+'。' if items else '还有一些细节没有确认。'


def answer_issue(coverage, basis, refs, sources):
    """阻止用无关编号把资料缺口包装成有据结论；不验证任意自然语言。"""
    if any(r not in coverage['supporting_refs']+coverage.get('unassessed_refs',[]) for r in refs):
        return 'unsupported_knowledge_citation'
    if coverage['status']=='unassessed' and coverage.get('unassessed_refs'):
        return None
    if coverage['status'] != 'supported':
        return 'knowledge_insufficient' if basis == 'evidence' else None
    if basis == 'evidence':
        cited = [s for s in sources if s['id'] in refs]
        if any(not any(supports(s, need) for s in cited) for need in coverage['requirements']):
            return 'knowledge_citation_gap'
    return None
