"""把宿主解析出的事实、条件判断和待查项分开；不替模型认证整段回答。"""
from device_registry import DEVICES, profile
from evidence_scope import CHECK_LABELS, collection_time
from ha_croc_case import PAIR_LOG_PURPOSES, current_observation, croc_state, vip_state
from snapshot_comparison import object_fingerprint


PURPOSES = {
    'services': ('确认登记Croc服务的采集状态', '区分运行、停止和无法解析；不据此确认停止原因。'),
    'interfaces': ('核对预期VIP在接口中的分布', '与服务状态对照；单台快照不能确认VRRP角色或资源唯一性。'),
    'routes': ('补充业务路径的路由与策略线索', '核对已采集的主路由和策略规则；尚不能证明端到端可达。'),
    'logs': ('查找固定服务的历史事件线索', '补充启动/退出时序；日志事件不能替代当前状态或单独确认根因。'),
}


def check_purpose(check):
    purpose, value = PURPOSES.get(check, ('核对批准范围内的局部状态', '仅支持本项检查的局部结论。'))
    return {'purpose': purpose, 'can_resolve': value, 'reason_source': 'host_check_contract'}


def investigation_plan(case, observations, scope, remaining_reads, terminal=None):
    """候选项仅来自当前审批；展示建议不执行、不重试，也不延长授权。"""
    seen = {(o.get('device'), o.get('check')) for o in observations}
    remaining = max(0, remaining_reads)
    candidates = []
    for choice in case['next_checks']:
        device, check = choice['device'], choice['check']
        if check not in scope.get(device, ()) or (device, check) in seen:
            continue
        node = next((node for node in case['nodes'] if node['device'] == device), None)
        purpose = check_purpose(check)
        dependency = next((need for need in case.get('cross_node_needs', [])
                           if need['device']==device and need['check']==check and need['status']=='ready'), None)
        pair = next((pair for pair in case.get('pairs', [])
                     if pair['evidence_complete'] and pair['finding'] in PAIR_LOG_PURPOSES
                     and device in pair['devices']), None) if check == 'logs' else None
        if dependency:
            purpose.update(purpose=dependency['purpose'],reason_source=dependency['reason_source'],
                           because_refs=list(dependency['because_refs']),relation_id=dependency['relation']['id'])
        elif pair:
            purpose.update(purpose=PAIR_LOG_PURPOSES[pair['finding']], reason_source='host_pair_evidence',
                           because_refs=list(pair['refs']), relation_id=pair['id'])
        elif check == 'logs' and node and node['finding'] == 'owner_service_stopped':
            purpose.update(purpose='持有预期VIP但Croc服务停止，补充启动或退出线索',
                           reason_source='host_node_evidence', because_refs=list(node['refs']))
        elif check == 'routes' and node and node['finding'] == 'owner_service_running':
            purpose.update(purpose='VIP和Croc符合局部预期，继续核对业务路径线索',
                           reason_source='host_node_evidence', because_refs=list(node['refs']))
        candidates.append(dict(choice, **purpose))
    candidates = candidates[:remaining]
    if terminal in ('investigation_inactive', 'expired', 'cancelled'):
        stop = '调查已取消或失效；停止后续检查，保留已取得证据。'
    elif terminal and terminal != 'answer_returned':
        stop = '本轮未完成，已停止后续调用；已有证据保留，失败项不自动重试。'
    elif terminal:
        stop = '本轮回答已返回；检查建议不代表根因已确认，也不会继续执行。'
    elif remaining == 0:
        stop = '本轮读取预算已用完，依据已有证据给出局部结论并列明缺项。'
    elif not candidates:
        stop = '没有建议继续执行的批准项，依据已有证据回答并说明缺项。'
    else:
        stop = '仅考虑尚未尝试、已批准且预算允许的检查。'
    return {'next_checks': candidates, 'remaining_reads': remaining,
        'cross_node_needs': case.get('cross_node_needs', []),
        'run_ended': terminal is not None, 'stop_explanation': stop,
        'suggestions_execute_tools': False, 'grants_device_access': False,
        'continuation_note': '本轮已经结束；待查项仅供新调查申请参考，须重新批准。' if terminal else
                             '候选检查仍须经过宿主范围、有效期和预算校验。',
        'attempted_failures': [{'ref': o['id'], 'device': o['device'], 'check': o['check']}
                              for o in observations if o.get('ok') is not True]}


def build_report(case, observations, scope, remaining_reads, terminal):
    facts, assessments, unknowns = [], [], list(case['unverified'])
    for need in case.get('cross_node_needs', []):
        if need['status']=='evidence_available':continue
        status_text={'outside_scope':'接口不在本轮批准范围内',
            'evidence_unavailable':'接口已尝试但未取得可解析的本轮证据，不自动重试',
            'budget_exhausted':'接口尚未读取，本轮读取预算已用完',
            'ready':('接口尚未读取，本轮已结束，不会继续执行' if terminal else
                     '接口尚未读取，可在已有批准和剩余预算内核对')}[need['status']]
        unknowns.append(need['relation']['peer_label']+'的'+status_text+'；不能假定其已接管VIP')
    for node in case['nodes']:
        device = node['device']
        p = profile(device)
        for check in ('services', 'interfaces'):
            if check not in scope.get(device, ()):
                continue
            item = current_observation(observations, device, check)
            if item is None or collection_time(item.get('collected_at')) is None:
                unknowns.append(DEVICES[device]['label'] + '的' + CHECK_LABELS[check] + '未取得唯一、成功的本轮证据')
                continue
            # 独立展示每条采集事实；跨检查或跨节点合并仍采用case中的时间窗口守卫。
            state = croc_state(item['output'], p['croc_unit']) if check == 'services' else vip_state(item['output'], p['ha_vips'])
            text = ({'running': '登记Croc服务在采集时为运行状态', 'stopped': '登记Croc服务在采集时为停止状态'}
                    if check == 'services' else
                    {'owner': '接口快照中出现全部预期VIP', 'partial': '接口快照中只出现部分预期VIP',
                     'non_owner': '接口快照中未出现预期VIP'}).get(state)
            if text is None:
                unknowns.append(DEVICES[device]['label'] + '的' + CHECK_LABELS[check] + '输出不足以解析登记状态')
                continue
            facts.append({'text': text, 'device': device, 'check': check, 'refs': [item['id']],
                'collected_at': item['collected_at'], 'origin': item['origin'],
                'freshness': item['freshness'], 'kind': 'parsed_observation',
                'value':state,'object_fingerprint':object_fingerprint(device,check)})
        if node['finding'] != 'insufficient':
            assessments.append({'text': node['summary'], 'device': device, 'refs': list(node['refs']),
                                'assumption': case['assumption'], 'kind': 'conditional_assessment'})
    for pair in case.get('pairs', []):
        if pair['evidence_complete']:
            assessments.append({'text': pair['summary'], 'refs': list(pair['refs']),
                'assumption': pair['limits'], 'kind': 'sequential_pair_assessment'})
    return {'schema_version': 1, 'facts': facts, 'assessments': assessments,
        'unknowns': list(dict.fromkeys(unknowns)),
        'investigation': investigation_plan(case, observations, scope, remaining_reads, terminal),
        'root_cause_confirmed': False, 'model_answer_semantically_verified': False,
        'note': '采集事实仅代表对应时刻；判断受所列前提限制，完整根因和业务状态仍需核实。'}
