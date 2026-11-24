"""正常HA的Croc诊断路线：归纳本轮字段，不宣称根因或执行修复。"""
import ipaddress
import re
from device_registry import DEVICES, profile
from evidence_scope import collection_time
from topology_model import ha_pairs, peer_relation
from claim_text import prose, qualified

CASE_ID='ha_croc_business'
ASSUMPTION='按已有正常HA资料比较；当前配置、运行模式和角色转换阶段尚未核实。'
GAPS=['当前运行模式与配置','VRRP角色、候选资格及资源唯一性','采集间隔内的角色变化','服务停止触发者与完整退出时序','端到端业务与故障根因']
PAIR_LOG_PURPOSES = {
    'resource_missing_observed': '至少一个预期VIP在两次接口观测中均未出现，查看日志补充角色转换线索',
    'resource_overlap_observed': '两次接口观测出现重复VIP，查看日志核对角色转换时序',
    'resource_distribution_split': '不同预期VIP分别出现在两台接口观测中，查看日志核对资源变化时序',
}


def selected(question,policy):
    q=question.lower()
    return bool(policy and policy.get('mode')=='diagnostic' and policy.get('scene')=='ha_topology'
                and any(w in q for w in ('croc','业务','tun16','vip')))


def needs_extended_checks(question):
    return any(w in question.lower() for w in ('不通','故障','异常','没运行','不运行','未运行','失败','中断','没有业务','没业务'))


def prepare_scope(scope):
    # 仅准备已有目标的待审批检查项，不批准或执行。
    return {device:list(profile(device)['checks']) for device in scope}


def croc_state(output,unit):
    records={}
    # systemctl的属性输出顺序不能当作记录边界；多单元以空行分开。
    for block in re.split(r'\n\s*\n',output.replace('\r\n','\n').strip()):
        record={}
        for line in block.splitlines():
            if '=' not in line: continue
            key,value=line.split('=',1)
            if key in record: return 'unknown'
            record[key]=value
        ident=record.get('Id')
        if not ident:
            if record: return 'unknown'
            continue
        if ident in records: return 'unknown'
        records[ident]=record
    item=records.get(unit,{})
    if item.get('LoadState')!='loaded' or not re.fullmatch(r'[0-9]{1,10}',item.get('MainPID','')): return 'unknown'
    pid=int(item['MainPID'])
    if item.get('ActiveState') in ('inactive','failed') and pid==0: return 'stopped'
    if item.get('ActiveState')=='active' and item.get('SubState')=='running' and pid>0: return 'running'
    return 'unknown'  # 转换中或active/exited等不能硬套成持续运行/已停止。


def interface_addresses(output):
    rows={}
    for line in output.splitlines():
        parts=line.split()
        if len(parts)<2 or parts[1] not in ('UP','DOWN','UNKNOWN','LOWERLAYERDOWN','DORMANT'): continue
        addresses=[]
        try:
            for token in parts[2:]:
                if '/' not in token: raise ValueError()
                addresses.append(str(ipaddress.ip_interface(token).ip))
        except ValueError: continue  # MAC/标志是link行，不是addr行。
        name=parts[0].split('@',1)[0]
        if name in rows and rows[name]!=set(addresses): return None
        rows[name]=set(addresses)
    return rows


def vip_state(output,expected):
    rows=interface_addresses(output)
    if rows is None:return 'unknown'
    if any(interface not in rows for interface in expected): return 'unknown'
    if any(vip in addresses and interface!=required for interface,addresses in rows.items()
           for required,vip in expected.items()): return 'unknown'
    held=[vip in rows[interface] for interface,vip in expected.items()]
    return 'owner' if all(held) else 'partial' if any(held) else 'non_owner'


def current_observation(observations,device,check):
    items=[o for o in observations if o.get('device')==device and o.get('check')==check]
    if len(items)!=1: return None
    item=items[0]
    if (item.get('ok') is not True or item.get('origin')!='device_read' or item.get('freshness')!='current'
            or not isinstance(item.get('output'),str) or len(item['output'])>16000): return None
    return item


def assess_pairs(observations,scope):
    """只比较同时获批两台的接口快照；不把顺序采集说成同时持有或选举真值。"""
    results=[]
    for pair in ha_pairs():
        devices=pair['devices']
        if not all(device in scope and 'interfaces' in scope[device] for device in devices):continue
        items=[current_observation(observations,device,'interfaces') for device in devices]
        refs=[item['id'] for item in items if item]
        result={'id':pair['id'],'devices':list(devices),'finding':'insufficient',
            'summary':'缺少两台可比较的本轮接口观测，不能判断主备VIP分布。',
            'refs':refs,'resources':[],'collection_span_seconds':None,
            'evidence_complete':False,'simultaneous_snapshot':False,
            'vrrp_role_verified':False,'root_cause_confirmed':False,
            'limits':'顺序读取不是原子快照；当前模式、角色转换、VRRP通告与端到端业务尚未核实。'}
        if any(item is None for item in items):
            results.append(result);continue
        times=[collection_time(item.get('collected_at')) for item in items]
        if any(t is None for t in times):
            results.append(result);continue
        span=abs((times[0]-times[1]).total_seconds())
        result['collection_span_seconds']=round(span,3)
        if span>60:
            result.update(finding='collection_window_exceeded',
                summary='两台接口采集间隔超过60秒，不合并判断主备VIP分布。')
            results.append(result);continue
        rows=[interface_addresses(item['output']) for item in items]
        expected=pair['expected_vips']
        if any(row is None or any(interface not in row for interface in expected) for row in rows):
            results.append(result);continue
        if any(vip in addresses and interface!=required for row in rows for interface,addresses in row.items()
               for required,vip in expected.items()):
            result['summary']='VIP位于预期以外接口，当前登记可能不适用，不能合并判断。'
            results.append(result);continue
        resources=[{'interface':interface,'vip':vip,
                    'observed_holders':[device for device,row in zip(devices,rows) if vip in row[interface]],
                    'refs':list(refs)} for interface,vip in expected.items()]
        holders=[resource['observed_holders'] for resource in resources]
        if any(len(value)>1 for value in holders):
            finding='resource_overlap_observed'
            summary='两台各自采集结果中出现重复VIP；需核实角色转换时序及通告，尚不能确认同时双持或脑裂。'
        elif any(not value for value in holders):
            finding='resource_missing_observed'
            summary='两次接口观测中，至少一个预期VIP均未出现；需核实模式、配置与转换时序。'
        elif len({value[0] for value in holders})>1:
            finding='resource_distribution_split'
            summary='不同预期VIP分别出现在两台采集结果中，与单一资源持有者的正常HA假设不一致。'
        else:
            finding='single_resource_holder_observed'
            summary='两次接口观测中，'+DEVICES[holders[0][0]]['label']+'持有全部预期VIP，另一台未见这些VIP；仅说明采集时的资源分布。'
        result.update(finding=finding,summary=summary,resources=resources,evidence_complete=True)
        results.append(result)
    return results


def cross_node_needs(nodes,observations,scope,remaining_reads):
    """本节点的非持有状态只提示检查同组对端，不能推断对端已接管。"""
    needs=[]
    for node in nodes:
        if node['finding']!='non_owner_stop_expected':continue
        # 两项触发依据也必须属于本轮批准范围，不能借外来观测提出现场判断。
        if not {'services','interfaces'}<=set(scope.get(node['device'],())):continue
        relation=peer_relation(node['device'])
        if relation is None:continue
        device=relation['peer'];check='interfaces'
        tried=any(o.get('device')==device and o.get('check')==check for o in observations)
        item=current_observation(observations,device,check) if check in scope.get(device,()) else None
        source=current_observation(observations,node['device'],'interfaces')
        times=[collection_time(o.get('collected_at')) if o else None for o in (source,item)]
        usable=bool(item and all(stamp is not None for stamp in times)
                    and abs((times[0]-times[1]).total_seconds())<=60
                    and vip_state(item['output'],profile(device)['ha_vips'])!='unknown')
        if check not in scope.get(device,()):status='outside_scope'
        elif usable:status='evidence_available'
        elif tried:status='evidence_unavailable'
        elif remaining_reads<=0:status='budget_exhausted'
        else:status='ready'
        needs.append({'device':device,'check':check,'status':status,'relation':relation,
            'because_refs':list(node['refs']),'refs':[item['id']] if usable else [],
            'purpose':'本节点未持预期VIP，核对同组另一台的VIP分布；不能由主/备名称推断持有者。',
            'reason_source':'host_topology_dependency','grants_device_access':False})
    return needs


def assess_case(observations,scope,remaining_reads,include_routine_logs=True):
    nodes=[];core_checks=[];fault_logs=[];extra_checks=[];required_refs=[]
    seen={(o.get('device'),o.get('check')) for o in observations}
    for device,checks in scope.items():
        if device not in DEVICES: continue
        p=profile(device)
        service=current_observation(observations,device,'services')
        interface=current_observation(observations,device,'interfaces')
        state=croc_state(service['output'],p['croc_unit']) if service else 'unknown'
        role=vip_state(interface['output'],p['ha_vips']) if interface else 'unknown'
        if service and interface:
            times=[collection_time(o.get('collected_at')) for o in (service,interface)]
            if any(t is None for t in times) or abs((times[0]-times[1]).total_seconds())>60:
                state='unknown'  # 分开采集不是原子快照，间隔过大不合并推断。
        refs=[o['id'] for o in (service,interface) if o]
        finding='insufficient';summary='缺少可解析的本轮服务或VIP依据，不能判断停止是否符合HA预期。'
        advice='补充明确的服务/VIP观测；已有失败读取不自动重试。'
        if role=='owner' and state=='unknown':
            summary='接口已出现全部预期VIP，但登记Croc服务状态尚不可确认，不能按无VIP备机解释。'
        if role=='partial':
            finding='partial_vip';summary='只持有部分预期VIP，资源分布需要核实。'
            advice='先核实配置、角色转换及两台VIP分布，不要直接重启Croc。'
        elif role=='owner' and state=='stopped':
            finding='owner_service_stopped';summary='采集时持有预期VIP，但登记Croc服务未运行；与正常HA业务持有者的预期不一致。'
            advice='结合固定服务日志核对启动失败、门禁或角色转换，确认当前模式后再制定修复方案。'
        elif role=='non_owner' and state=='stopped':
            finding='non_owner_stop_expected';summary='采集时未持有预期VIP，登记Croc服务停止符合正常HA中非VIP持有节点的预期。'
            relation=peer_relation(device)
            if relation and 'interfaces' in scope.get(relation['peer'],()):
                advice='这两项状态不构成备机故障依据；可在已有批准范围内核对同组对端，不能假定对端已持有VIP。'
            else:
                advice='这两项状态不构成备机故障依据；业务不通需核对VIP持有者，对端接口不在本轮批准范围内。'
        elif role=='non_owner' and state=='running':
            finding='non_owner_service_running';summary='采集时未持有预期VIP，但登记Croc服务运行；与正常HA中非VIP持有节点的预期不一致。'
            advice='先核实当前模式、转换时序和资源分布，再决定处理方案，不要直接停止进程。'
        elif role=='owner' and state=='running':
            finding='owner_service_running';summary='VIP与登记Croc服务运行状态符合业务持有者的局部预期，尚不能证明业务通畅。'
            advice='按剩余批准范围查看路由及历史日志，进一步缺项应具体说明，不能直接定位全部根因。'
        if finding!='insufficient' or service and interface and role!='unknown': required_refs.extend(refs)
        extra=[o['id'] for o in observations if o.get('device')==device and o.get('ok') is True
               and o.get('freshness')=='current' and o.get('check') in ('logs','routes')]
        nodes.append({'device':device,'label':DEVICES[device]['label'],'vip_role':role,'croc_service':state,
                      'finding':finding,'summary':summary,'refs':refs,'additional_refs':extra,
                      'repair_advice':advice,'root_cause_confirmed':False})
        core=[check for check in ('services','interfaces') if (device,check) not in seen]
        choices=core_checks
        wanted=core
        if not core and finding in ('owner_service_stopped','non_owner_service_running'):
            wanted=['logs']
            choices=fault_logs
        elif not core and finding=='owner_service_running':
            wanted=['routes','logs'] if include_routine_logs else ['routes']
            choices=extra_checks
        for check in wanted:
            if check in checks and (device,check) not in seen:
                choices.append({'device':device,'check':check})
    needs=cross_node_needs(nodes,observations,scope,remaining_reads)
    # 优先核对由已取得证据指向的对端接口，再考虑原基础检查与日志。
    # 只重排现有批准候选；不添加目标、重试失败项或增加读取预算。
    preferred=[{'device':need['device'],'check':need['check']} for need in needs if need['status']=='ready']
    pairs=assess_pairs(observations,scope)
    # 单节点状态可能符合待机预期，但双节点VIP分布异常仍需要角色转换线索。
    pair_log_checks = [
        {'device': device, 'check': 'logs'}
        for pair in pairs if pair['evidence_complete'] and pair['finding'] in PAIR_LOG_PURPOSES
        for device in pair['devices']
        if 'logs' in scope.get(device, ()) and (device, 'logs') not in seen
    ]
    pending = core_checks + pair_log_checks + [choice for choice in fault_logs + extra_checks if choice not in pair_log_checks]
    next_checks=([choice for choice in preferred if choice in pending]+
                 [choice for choice in pending if choice not in preferred])[:max(0,remaining_reads)]
    for pair in pairs:
        if pair['evidence_complete']:required_refs.extend(pair['refs'])
    return {'id':CASE_ID,'title':'Croc业务不通：服务与VIP的关系','assumption':ASSUMPTION,
            'nodes':nodes,'pairs':pairs,'cross_node_needs':needs,'next_checks':next_checks,'required_refs':list(dict.fromkeys(required_refs)),
            'unverified':list(GAPS),'root_cause_confirmed':False,'repair_executed':False,
            'stop_reason':'bounded_checks_remain' if next_checks else 'available_evidence_collected_or_unavailable'}


def stop_cause_overclaim(answer):
    """只拦截已复现的排除式因果断言；不是完整语义判分器。"""
    text=prose(answer)
    patterns=[r'(?:说明|证明|确认|意味着|判定)[^。；\n]{0,40}(?:正常|干净|主动|手动)停止[^。；\n]{0,20}(?:而非|而不是|不是)崩溃',
              r'(?:排除|确定不是)崩溃']
    for pattern in patterns:
        for match in re.finditer(pattern,text):
            if qualified(text,match):continue
            if re.search(r'不能|无法|不足|尚未|并不|不等于|不代表',match.group()): continue
            return True
    return False


def pair_role_overclaim(answer):
    """有限拦截把顺序采集升级为同时双持/已确认脑裂的断言。"""
    text=prose(answer)
    for pattern in (r'(?:已经|已|确认|确定|判定|就是)[^。；\n]{0,5}(?:脑裂|双主)',
                    r'(?:两台|主备)[^。；\n]{0,10}同时[^。；\n]{0,8}(?:持有|拥有)[^。；\n]{0,8}VIP'):
        for match in re.finditer(pattern,text,re.I):
            if qualified(text,match,uncertain=True):continue
            return True
    return False


def completion_overclaim(answer):
    """只检查肯定的根因/恢复声明，保留否定、条件、提问与代码例子。"""
    text=prose(answer)
    pattern=r'根因(?:已经|已)?(?:确认|确定)(?:是|为)|根因就是|故障(?:已经|已)修复|业务(?:已经|已)恢复'
    for match in re.finditer(pattern,text):
        if qualified(text,match):continue
        return True
    return False


def answer_issues(report,answer,refs):
    issues=[]
    if not set(report['required_refs'])<=set(refs):
        issues.append({'id':'ha_case_refs','refs':report['required_refs'],
                       'statement':'解释服务停止与VIP关系必须同时引用相关服务和VIP观测，不能只凭inactive判故障。'})
    if completion_overclaim(answer):
        issues.append({'id':'ha_case_limits','refs':report['required_refs'],
                       'statement':'本案例未确认全部根因，未修复或验收业务；只能给局部状态、可能原因与建议。'})
    if stop_cause_overclaim(answer):
        issues.append({'id':'ha_stop_cause','refs':report['required_refs'],
                       'statement':'服务状态、MainPID和Result字段只能支持对应状态描述；本案例缺少足够的退出原因与时序，不能据此排除崩溃或确认停止触发者。'})
    if report.get('pairs') and pair_role_overclaim(answer):
        issues.append({'id':'ha_pair_nonatomic','refs':list(dict.fromkeys(
            ref for pair in report['pairs'] for ref in pair['refs'])),
            'statement':'两台接口按顺序采集，不能确认同一时刻双持VIP或脑裂；只描述各次观测的资源分布，保留转换时序、当前配置与VRRP通告未知。'})
    for node in report['nodes']:
        aliases=[node['label']]
        if len(report['nodes'])==1: aliases.append(node['label'][2:])
        if node['vip_role']=='owner':
            # 只拦截已复现的“已读到全部VIP却说未见VIP”，不把规则假设当当前断言。
            for clause in re.split(r'[。；\n]',prose(answer)):
                if len(report['nodes'])>1 and node['label'] not in clause:continue
                match=re.search(r'(?:未观测到|未见|没有|不持有|未持有)(?:业务|接收|任何|预期)?VIP'
                    r'(?:地址|记录)?(?=$|[，,。；;、\s（）()？?]|吗|呢|但|并|而|且|因此|所以)',clause,re.I)
                if not match:continue
                if qualified(clause,match,uncertain=True):continue
                issues.append({'id':'ha_observed_vip','refs':node['refs'],
                    'statement':node['label']+'本轮接口已出现全部预期VIP，不能反说未见VIP或套用无VIP备机条件。'
                                '登记服务状态仍以本轮可解析字段为准；单元未加载不能确认全部Croc进程。'
                                'VIP地址观测不等于VRRP角色/模式已核实。'})
                break
        match=re.search('(?:'+'|'.join(re.escape(a) for a in aliases)+r')[^。\n]{0,60}(?:Croc|croc)[^。\n]{0,20}'
                        r'(?:未运行|停止|inactive)(?P<link>[^。\n]{0,15})(?:说明|导致|因此|所以)[^。\n]{0,12}(?:故障|异常|不通)',answer)
        if node['finding']=='non_owner_stop_expected' and match and not re.search(r'不|不能|没有',match.group('link')):
            issues.append({'id':'ha_nonowner_expected','refs':node['refs'],'statement':node['summary']})
    return issues
