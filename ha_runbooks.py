"""Topology-specific investigations compiled to fixed, passive checks.

Runbook structure informed by the HolmesGPT runbook catalog. All checks and
evidence rules here refer to this lab's Linux tools and HA configuration.
"""
import re

from device_registry import DEVICES, profile
from diagnostic_goal import citations
from ha_croc_case import current_observation
from learning_intent import answer_discussion, forbids_inspection, read_method_question
from link_inspection import wants_ppp_connections


RUNBOOKS = {
    'topology_health': ('已接入拓扑设备的主备资源与服务', 'device_state', ('interfaces','services')),
    'ha_state': ('主备接管和服务', 'vip_ownership', ('interfaces', 'services')),
    'services': ('服务运行状态', 'device_state', ('services',)),
    'interfaces': ('网口和拨号接口状态', 'device_state', ('interfaces',)),
    'routing': ('IPv4 策略路由', 'routing', ('policy_routes',)),
    'logs': ('固定服务的近期日志', 'device_state', ('logs',)),
    'croc_fault': ('Croc 传输故障', 'business_path', ('interfaces', 'services', 'logs', 'policy_routes')),
}
CHECK_LABELS = {'interfaces':'接口','services':'服务','logs':'日志','policy_routes':'策略路由'}

ACTIVITY = re.compile(r'现在|当前|目前|刚刚|还在|帮我|麻烦|请查|查看|核对|排查|查一下|再查|再看|^(?:接着|继续|然后|先|改|换)?(?:查|看)|怎么样|正常吗|起来了[吗没]|有没有|能不能|是不是|呢[？?。]*$')
CONCEPT = re.compile(r'如果|假如|假设|举例|原理|一般|平时|通常|历史|上次|怎么查|如何|什么意思|是什么|有什么区别|怎么(?:配置|接线|工作)')
EXTERNAL_OR_ACTION = re.compile(r'router|路由器|交换机|独立|三卡|六卡|云服务器|物理机|[36]卡|重启|关机|停(?:掉|止)服务|启动一下|修改|删除|清空|切换到|执行命令|sudo|\b\d{1,3}(?:\.\d{1,3}){3}\b', re.I)
CROC_FAULT = re.compile(r'不通|没(?:有)?业务|收不到|传不过去|收发失败|断(?:了|开)|中断|掉线|停止|没(?:有)?运行|起不来|异常|卡住|失败|抖动')
TOPICS = (
    ('ha_state', re.compile(r'接管|接手|接替|谁在顶|谁在工作|倒换|切换|(?<![a-z0-9_])vip(?![a-z0-9_])|主备(?:状态|正常|怎么样|情况)', re.I)),
    ('routing', re.compile(r'路由表|策略路由|路由规则|从哪.{0,5}出去|走哪张表')),
    ('logs', re.compile(r'日志|最近.{0,8}(?:报错|启动记录)')),
    ('services', re.compile(r'服务|进程|keepalived|croc', re.I)),
    ('interfaces', re.compile(r'接口|网口|拨号|ppp|tun\d+', re.I)),
)


def menu(options):
    return {name:label for name,(label,_,checks) in RUNBOOKS.items()
            if any(set(checks)<=set(item['checks']) for item in options.values())}


def compile_runbook(name, devices, options):
    if not isinstance(name,str) or name not in RUNBOOKS or not isinstance(devices,list) or not devices:
        raise ValueError('invalid_runbook')
    if any(not isinstance(device,str) or device not in options for device in devices) or len(set(devices))!=len(devices):
        raise ValueError('invalid_runbook_devices')
    _,goal,checks=RUNBOOKS[name]
    if any(not set(checks)<=set(options[device]['checks']) for device in devices):
        raise ValueError('invalid_runbook_scope')
    return goal,{device:list(checks) for device in devices}


def local_intent(question, explicit, previous):
    """Recognize clear categories even if the model is unavailable."""
    from topology_observations import operational_question
    field=operational_question(question)
    if (forbids_inspection(question) or read_method_question(question) or answer_discussion(question)
            or re.search(r'重拨|复位|重置|停用|禁用|(?:不要|先别|不用|不必)看', question)
            or (CONCEPT.search(question) and not field) or EXTERNAL_OR_ACTION.search(question)
            or re.search(r'(?<![a-z])(?:ping|icmp|ssh)(?![a-z])|发包|先别查|只|仅|别查|不要查|顺便|同时|以及|另外', question,re.I)):
        return None
    sides={side for side in ('局端','远端') if side in question}
    if (ACTIVITY.search(question) and re.search(r'故障|异常|健康|正常|有问题',question)
            and re.search(r'拓扑|设备',question) and not re.search(r'服务|进程|日志|路由|接口|网口',question)):
        devices=explicit or [d for d in DEVICES if not sides or profile(d)['side_label'] in sides]
        return 'topology_health',devices,False
    referring=bool(re.match(r'^(?:那|继续|接着|再|然后|这台|它|对面|另一端)',question))
    devices=explicit or [device for device in DEVICES if profile(device)['side_label'] in sides]
    continued=False
    if not devices and referring and not re.search(r'备机|主机|主备|另一端|对面|另[一外].{0,2}台',question):
        devices=previous.get('devices',[])
        continued=bool(devices)
    if not devices:
        return None
    if field:
        return field,devices,continued
    if (re.search(r'croc|业务|视频|隧道',question,re.I) and CROC_FAULT.search(question)
            and not re.search(r'keepalived|日志|路由表|策略路由|路由规则',question,re.I)):
        return 'croc_fault',devices,continued
    if not ACTIVITY.search(question):
        return None
    matches=[name for name,pattern in TOPICS if pattern.search(question)]
    if (wants_ppp_connections(question) and all(profile(device)['group']=='remote' for device in devices)
            and set(matches) <= {'interfaces', 'services'} and not re.search(r'配置|进程|服务',question)):
        return 'interfaces',devices,continued
    if len(matches)!=1:
        return None
    if matches[0]=='ha_state' and not explicit:
        groups={profile(device)['group'] for device in devices}
        devices=[device for device in DEVICES if profile(device)['group'] in groups]
    return matches[0],devices,continued


def next_check(name, catalog, observations, case):
    candidates=[(device,check) for device,item in catalog.items() for check in item['checks']]
    initial=RUNBOOKS[name][2]
    if name=='croc_fault':initial=initial[:2]
    for check in initial:
        match=next((entry for entry in candidates if entry[1]==check),None)
        if match:return match
    if name!='croc_fault':return None
    attempted={item['device'] for item in observations if item['check'] in ('logs','policy_routes')}
    for item in case['next_checks']:
        device=item['device']
        if item['check']=='logs' and device not in attempted and (device,'logs') in candidates:
            return device,'logs'
    for node in case['nodes']:
        device=node['device']
        if (node['finding']=='owner_service_running' and device not in attempted
                and (device,'policy_routes') in candidates):
            return device,'policy_routes'
    return None


def summarize(name, observations, scope, case, goal_result, question=''):
    from execution_workflow import result_summary
    from link_inspection import check_interfaces, ppp_summary

    refs,lines=[],[]
    if name in ('croc_fault','topology_health'):
        for node in case['nodes']:
            if node['refs']:
                refs.extend(node['refs'])
                lines.append(node['label']+'：'+node['summary']+citations(node['refs']))
        for pair in case['pairs']:
            if pair['evidence_complete'] and pair['finding']!='single_resource_holder_observed':
                refs.extend(pair['refs'])
                lines.append(pair['summary']+citations(pair['refs']))
        for device,checks in scope.items():
            for check in ('logs','policy_routes'):
                item=current_observation(observations,device,check) if check in checks else None
                if item:
                    refs.append(item['id'])
                    lines.append(DEVICES[device]['label']+'：'+result_summary(item)+citations([item['id']]))
        header='业务端到端收发仍待验证，以下是本轮取得的 Croc 服务和 VIP 线索。'
        if any(node['finding'] in ('owner_service_stopped','non_owner_service_running','partial_vip')
               for node in case['nodes']):
            header='发现服务与 VIP 分布不一致，业务端到端收发仍待验证。'
        if name=='topology_health':
            incomplete=any(node['finding']=='insufficient' for node in case['nodes']) or any(
                not pair['evidence_complete'] for pair in case['pairs'])
            inconsistent=any(node['finding'] in ('owner_service_stopped','non_owner_service_running','partial_vip')
                             for node in case['nodes']) or any(
                pair['evidence_complete'] and pair['finding']!='single_resource_holder_observed' for pair in case['pairs'])
            header=('本轮信息不完整，暂时无法判断已接入设备的主备资源是否一致。' if incomplete else
                    '本轮发现主备资源与服务分布不一致。' if inconsistent else
                    '本轮所查设备中，未发现 VIP 与 Croc 服务分布不一致。')
            lines.append('本轮检查'+ '、'.join(DEVICES[d]['label'] for d in scope)+'；路由器、交换机、终端及实际业务收发尚未验证。')
    elif name in ('ha_state','routing'):
        refs.extend(goal_result['refs'])
        header=goal_result['answer']
    else:
        header=''
    if name in ('ha_state','services','interfaces','logs'):
        for device,checks in scope.items():
            for check in checks:
                if name=='ha_state' and check!='services':continue
                item=current_observation(observations,device,check)
                if item:
                    refs.append(item['id'])
                    if check=='interfaces':
                        from topology_observations import address_question,interface_fields,operational_question
                        port=address_question(question)
                        if port:
                            value=interface_fields(device,item['output']).get(port)
                            lines.append(DEVICES[device]['label']+' '+(value or port+' 未取得有效地址')+citations([item['id']]))
                            continue
                        if operational_question(question)=='interfaces':
                            values=interface_fields(device,item['output'])
                            summary='，'.join(values.values()) or '未取得可判读的接口状态'
                            lines.append(DEVICES[device]['label']+' '+summary+citations([item['id']]))
                            continue
                        ports = profile(device)['interfaces']
                        summary = ppp_summary(item['output'], ports, question)
                        if not summary:
                            fields=check_interfaces(item['output'],ports)
                            states=[port+' '+field['state'] for port,field in fields.items()
                                    if field['state'] not in ('unconfirmed','absent')]
                            summary='，'.join(states)+'。' if states else '未取得可判读的接口状态。'
                    else:
                        summary=result_summary(item)
                        if check=='services' and name=='services':
                            from topology_observations import project
                            states=project(device,check,item['output'])
                            units=[unit for unit in ('croc','keepalived') if unit in question.lower()]
                            if len(units)==1:
                                focused=[v for k,v in states.items() if k.lower().startswith(units[0])]
                                summary='，'.join(focused) or '未取得 '+units[0]+' 的明确运行状态。'
                    lines.append(DEVICES[device]['label']+'：'+summary+citations([item['id']]))
    failures=[DEVICES[item['device']]['label']+'的'+CHECK_LABELS[item['check']] for item in observations
              if item['check'] in CHECK_LABELS and not current_observation(observations,item['device'],item['check'])]
    if failures:
        lines.append('未取得有效结果：'+'、'.join(failures)+'。')
    return '\n\n'.join(filter(None,[header]+lines)),list(dict.fromkeys(refs))
