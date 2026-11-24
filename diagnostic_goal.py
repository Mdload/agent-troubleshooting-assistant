"""Interpret narrow diagnostic goals from the approved observations."""
from device_registry import DEVICES, PING_TARGETS, PROBE_CHECKS, TUNNEL_CHECKS, tunnel_endpoints, profile
from diagnostic_probes import KEEPALIVED_FIELDS, ping_counts, ping_route
from evidence_scope import collection_time
from ha_croc_case import assess_pairs, current_observation, interface_addresses
from topology_model import ha_pairs
from link_inspection import check_interfaces


def vip_ownership(observations, scope):
    pairs = {pair['id']: pair for pair in assess_pairs(observations, scope)}
    findings, refs, states = [], [], []
    for pair in ha_pairs():
        devices = pair['devices']
        if not any(device in scope for device in devices):
            continue
        observed = pairs.get(pair['id'])
        if observed and observed['evidence_complete']:
            findings.append(observed['summary'] + citations(observed['refs']))
            refs.extend(observed['refs'])
            states.append('observed' if observed['finding'] == 'single_resource_holder_observed' else 'inconsistent')
        elif observed and observed['finding'] == 'collection_window_exceeded':
            findings.append(pair['label'] + '：' + observed['summary'])
            states.append('unverified')
        else:
            findings.append(pair['label'] + '的VIP归属未确认，需要两台可比较的本轮接口观测。')
            states.append('unverified')
    if 'inconsistent' in states:
        status = 'inconsistent'
    else:
        status = 'observed' if states and all(state == 'observed' for state in states) else 'unverified'
    return status, refs, '\n\n'.join(findings) or 'VIP归属未确认，需要同组两台的本轮接口观测。'


def citations(refs):
    return ' ' + ' '.join('[' + ref + ']' for ref in refs) if refs else ''


def recent_vip_holders(observations, scope, now):
    holders={}
    for pair in assess_pairs(observations,scope):
        if pair['finding']!='single_resource_holder_observed':continue
        stamps=[collection_time(current_observation(observations,device,'interfaces')['collected_at'])
                for device in pair['devices']]
        if all(0 <= (now-stamp).total_seconds() <= 120 for stamp in stamps):
            device=pair['resources'][0]['observed_holders'][0]
            holders[profile(device)['group']]=device
    return holders


def connectivity_overview(goal, observations, scope):
    """Report verified local observations without declaring a cross-side link up."""
    findings,refs,interface_facts=[],[],[]
    for device,checks in scope.items():
        if 'interfaces' not in checks:continue
        item=current_observation(observations,device,'interfaces')
        label=DEVICES[device]['label']
        if not item:
            findings.append(label+'：未取得本轮有效接口结果')
            continue
        management=profile(device)['management_interface']
        interfaces=check_interfaces(item['output'],(management,'tun16'))
        state=interfaces['tun16']['state']
        fact={'absent':'未发现 tun16','unconfirmed':'接口结果中未确认 tun16 状态'}.get(state,'发现 tun16（'+state+'）')
        management_state=interfaces[management]['state']
        if management_state not in ('unconfirmed','UNKNOWN'):
            fact+='，管理口 '+management+' '+('未发现' if management_state=='absent' else management_state)
        interface_facts.append({'device':device,'ref':item['id'],'interfaces':interfaces})
        findings.append(label+'：'+fact+citations([item['id']]))
        refs.append(item['id'])
    limits={
        'management_path':'管理地址能否互通仍待实测。可以继续说“两端互相 ping 一下”。',
        'tunnel':'是否有流量通过 Croc 隧道仍待验证。',
        'business_path':'两端实际业务收发仍待验证。需要业务终端的发送和接收证据。',
        'connectivity_overview':'接口观测还不能回答两端能否通信。可以继续说“实测两端能否互相通信”，分别测 tun16 和固定管理地址。',
    }
    answer=limits[goal]
    if findings:
        answer+='\n\n'+'；'.join(findings)+'。'
    if goal!='management_path':
        _, vip_refs, vip_summary=vip_ownership(observations,scope)
        if vip_refs:
            answer+='\n\n'+vip_summary
            refs.extend(vip_refs)
    return {'goal':goal,'status':'unverified','refs':list(dict.fromkeys(refs)),
            'interface_facts':interface_facts,'answer':answer}


def local_facts(goal, observations, scope):
    facts = {}
    for device, checks in scope.items():
        for check in checks:
            item = current_observation(observations, device, check)
            if not item or check == 'logs':
                continue
            if goal == 'management_path':
                facts[device] = ('采集端通过登记管理路径取得状态', item['id'])
                break
            if goal == 'routing' and item['output'].strip():
                if check == 'policy_routes':
                    facts[device] = ('取得 IPv4 策略规则和全部路由表', item['id'])
                elif check == 'routes' and device not in facts:
                    facts[device] = ('取得主路由表和策略规则输出', item['id'])
            if goal == 'tunnel' and check == 'interfaces':
                rows = interface_addresses(item['output'])
                if rows is not None and 'tun16' in rows:
                    facts[device] = ('采集时存在 tun16', item['id'])
                elif 'INTERFACE_ABSENT=tun16' in item['output'].splitlines():
                    facts[device] = ('采集时未发现 tun16', item['id'])
    return facts


GOAL_LIMITS = {
    'management_path': '设备之间的管理地址互通未验证。需要从指定源设备到目标地址的定向探测。',
    'tunnel': 'Croc 隧道是否能承载流量仍待验证。需要核对两端隧道和连接状态，并验证隧道内收发。',
    'routing': '指定业务流的实际转发路径未验证。需要确认源、目的及命中的策略表和下一跳。',
    'business_path': '两端实际业务收发未验证。需要指定业务端点的发送和接收证据。',
}


def assess_icmp(goal, observations, scope):
    lines, refs, replies, directions, measurements = [], [], [], [], []
    tunnel_replies = {}
    for device, checks in scope.items():
        for check in checks:
            if check not in PROBE_CHECKS:
                continue
            tunnel = check in TUNNEL_CHECKS
            target = tunnel_endpoints(device,check)[1] if tunnel else PING_TARGETS[check]
            item = current_observation(observations, device, check)
            counts = ping_counts(item['output']) if item else None
            if tunnel and counts and ping_route(item['output']).get('interface')!='tun16':
                counts = None
            no_route = bool(item and item['output'].strip() == 'ICMP_ROUTE_UNAVAILABLE=1')
            mismatch = bool(tunnel and item and item['output'].strip() == 'ICMP_TUNNEL_PATH_MISMATCH=1')
            if tunnel:
                tunnel_replies[profile(device)['group']] = counts[1] if counts else None
            else:
                directions.append((device,target))
                replies.append(counts[1] if counts else None)
            measurements.append({'source': device, 'target': target, 'check': check,
                                 'outcome': 'measured' if counts else 'no_route' if no_route else 'path_mismatch' if mismatch else 'unavailable',
                                 'ref': item['id'] if counts or no_route or mismatch else None,
                                 'sent': counts[0] if counts else None,
                                 'received': counts[1] if counts else None})
            label = DEVICES[device]['label'] + '到' + ('对端 tun16 '+target if tunnel else
                    DEVICES[target]['label']+'（管理地址 '+DEVICES[target]['address']+'）')
            if counts:
                tx, rx = counts
                status = f'发出 {tx} 个探测，收到 {rx} 个应答' if rx else f'发出 {tx} 个探测，未收到应答'
                route=ping_route(item['output'])
                fields=[title+' '+route[key] for key,title in (('source','源地址'),('interface','出口'),('gateway','下一跳')) if key in route]
                method='。探测前选路：'+'，'.join(fields) if fields else ''
                lines.append(label + '，' + status + method + '。' + citations([item['id']]))
                refs.append(item['id'])
            elif no_route:
                lines.append(label+'，路由查询未返回可用路径，本次未发包。'+citations([item['id']]))
                refs.append(item['id'])
            elif mismatch:
                lines.append(label+'，本端 tun16 地址或出口不匹配，本次未发包。'+citations([item['id']]))
                refs.append(item['id'])
            else:
                lines.append(label + '，本轮没有可判读的探测结果。')
    summary = '\n\n'.join(lines) or '本轮没有批准的定向 ICMP 探测。'
    if len(directions)==2 and directions[0]==directions[1][::-1] and None not in replies:
        if all(replies): conclusion='两边都收到管理地址的 ping 应答。'
        elif any(replies): conclusion='只有一个方向收到 ping 应答。'
        else: conclusion='两个方向都没有收到 ping 应答。'
        summary=conclusion+'\n\n'+summary
    elif len(directions)==2 and directions[0]==directions[1][::-1] and any(
            m['outcome']=='no_route' and m['check'] in PING_TARGETS for m in measurements) and any(replies):
        summary='管理地址仅一个方向收到应答，另一端未查到可用路由。\n\n'+summary
    if set(tunnel_replies)=={'local','remote'} and all(tunnel_replies.values()):
        summary='tun16 双向均收到 ICMP 应答。\n\n'+summary
    summary += '\n\n'+('实际业务收发仍待验证。' if tunnel_replies else '这次测的是管理地址，业务收发仍待验证。')
    return {'goal':goal,'status':'observed' if refs and all(m['outcome']=='measured' for m in measurements) else 'unverified',
            'refs':refs,'answer':summary,'measurements':measurements}


def assess_ha_configuration(goal, observations, scope):
    lines, refs = [], []
    for device, checks in scope.items():
        if 'ha_config' not in checks:
            continue
        item = current_observation(observations, device, 'ha_config')
        if not item:
            continue
        output = item['output'].splitlines()
        fields = [(key, value) for key, value in (line.split('=',1) for line in output if '=' in line)
                  if key in KEEPALIVED_FIELDS]
        if not fields:
            continue
        refs.append(item['id'])
        details = '，'.join(KEEPALIVED_FIELDS[key] + ' ' + value for key,value in fields[:12])
        suffix = '。配置引用或输出限制可能影响完整性。' if any(x in output for x in ('INCLUDES=1','TRUNCATED=1')) else '。'
        lines.append(DEVICES[device]['label'] + '的文件记录 ' + details + suffix + citations([item['id']]))
    answer = '\n\n'.join(lines) or '本轮未取得可判读的 Keepalived 配置字段。'
    if lines:
        answer += '\n\n以上来自磁盘文件，当前进程加载的内容及通告收发仍待核实。'
    return {'goal':goal,'status':'unverified','refs':refs,'answer':answer}


def assess_goal(goal, observations, scope, path=None, passive_overview=False):
    if goal=='connectivity_overview' and any(item.get('check') in PROBE_CHECKS for item in observations):
        observed={(item.get('device'),item.get('check')) for item in observations}
        attempted={d:[c for c in checks if (d,c) in observed] for d,checks in scope.items()}
        result=assess_icmp(goal,observations,attempted)
        if any('interfaces' in checks for checks in attempted.values()):
            overview=connectivity_overview(goal,observations,attempted)
            result['interface_facts']=overview['interface_facts']
        return result
    if goal == 'icmp_reachability' or (goal == 'management_path'
            and any(check in PING_TARGETS for checks in scope.values() for check in checks)):
        return assess_icmp(goal, observations, scope)
    if goal == 'ha_configuration':
        return assess_ha_configuration(goal, observations, scope)
    if goal=='connectivity_overview' or (passive_overview and goal in ('management_path','tunnel','business_path')
            and scope and all(checks==['interfaces'] for checks in scope.values())):
        return connectivity_overview(goal,observations,scope)
    if goal == 'vip_ownership':
        status, refs, answer = vip_ownership(observations, scope)
        return {'goal': goal, 'status': status, 'refs': refs, 'answer': answer}
    if goal not in GOAL_LIMITS:
        return None
    facts = []
    if goal == 'business_path':
        labels = {
            ('services', 'running'): 'Croc 运行',
            ('services', 'stopped'): 'Croc 停止',
            ('interfaces', 'owner'): '出现全部预期 VIP',
            ('interfaces', 'partial'): '出现部分预期 VIP',
            ('interfaces', 'non_owner'): '未出现预期 VIP',
            ('routes', 'route_output_collected'): '取得路由输出',
        }
        for segment in path['segments']:
            for node in segment['nodes']:
                device = node['device']
                for check, field in node['fields'].items():
                    fact = labels.get((check, field['value']))
                    if fact:
                        facts.append((device, fact, field['refs'][0]))
    else:
        facts = [(device, fact, ref) for device, (fact, ref) in local_facts(goal, observations, scope).items()]
    summary = '；'.join(DEVICES[device]['label'] + fact + citations([ref]) for device, fact, ref in facts)
    return {'goal': goal, 'status': 'unverified', 'refs': list(dict.fromkeys(ref for _, _, ref in facts)),
            'answer': GOAL_LIMITS[goal] + ('\n\n采集到的局部状态：' + summary + '。' if summary else '')}
