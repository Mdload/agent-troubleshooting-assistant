"""业务路径证据覆盖；管理链可达不代替业务链或未登记终端验收。"""
from device_registry import DEVICES, profile
from evidence_scope import collection_time
from ha_croc_case import current_observation, croc_state, vip_state
from router_contract import replay_observation, assess_replay


def selected(question,policy):
    if not policy or policy.get('mode')!='diagnostic' or policy.get('scene')!='ha_topology':return False
    if policy.get('diagnostic_goal'):return policy['diagnostic_goal']=='business_path'
    q=question.lower()
    fault=any(term in q for term in ('不通','故障','异常','中断','没业务','没有业务','失败'))
    path=any(term in q for term in ('全链路','端到端','完整路径','业务路径','跨节点'))
    return fault and (path or '局端' in q and '远端' in q or 'ping' in q and '业务' in q)


def assess_path(observations,scope,synthetic_replay=False):
    segments=[]
    for group,label in (('remote','远端Croc入口'),('local','局端Croc接收')):
        nodes=[]
        for device in scope:
            if device not in DEVICES or profile(device)['group']!=group:continue
            p=profile(device);fields={}
            for check in ('services','interfaces','routes'):
                if check not in scope[device]:continue
                item=(replay_observation(observations,scope,device,check) if synthetic_replay
                      else current_observation(observations,device,check))
                if item is None or collection_time(item.get('collected_at')) is None:continue
                if check=='services':value=croc_state(item['output'],p['croc_unit'])
                elif check=='interfaces':value=vip_state(item['output'],p['ha_vips'])
                else:value='route_output_collected' if item['output'].strip() else 'unknown'
                if value!='unknown':fields[check]={'value':value,'refs':[item['id']]}
            nodes.append({'device':device,'fields':fields,'current_role':'unknown'})
        segments.append({'id':group,'label':label,'nodes':nodes,
            'status':'partial_evidence' if any(node['fields'] for node in nodes) else 'unverified',
            'end_to_end_verified':False})
    routers=assess_replay(observations,scope)['nodes'] if synthetic_replay else []
    segments.insert(1,{'id':'router','label':'中间Router转发','nodes':routers,
        'status':'synthetic_partial_evidence' if any(node['fields'] for node in routers) else 'not_connected',
        'end_to_end_verified':False,'execution_enabled':False})
    gaps=['交换与接入网络、两端业务终端的回程路由及实际流量未核验',
          '当前HA模式、角色转换与资源唯一性未完整核验；顺序读取不是同时快照',
          '管理SSH或ping成功不证明业务路径畅通']
    if not synthetic_replay:
        gaps.insert(0,'Router工具未开放，本轮未读取Router；待核对转发开关、业务VIP和路由')
    elif not any(node['fields'] for node in routers):gaps.insert(0,'未取得可用的Router合成回放依据')
    for segment in segments:
        if segment['status']=='unverified':gaps.append(segment['label']+'未取得本轮有效状态依据')
    return {'kind':'synthetic_business_path_replay' if synthetic_replay else 'business_path_evidence_coverage',
        'segments':segments,'gaps':gaps,'current_state_verified':False,
        'end_to_end_verified':False,'root_cause_confirmed':False,'grants_device_access':False,
        'note':'只核对各段证据覆盖；不把管理连通、服务/VIP或路由输出升级为业务验收。'}


def attach(result,path):
    result['business_path']=path
    report=result.get('diagnostic_report')
    if isinstance(report,dict):report['unknowns']=list(dict.fromkeys(report['unknowns']+path['gaps']))
    return result
