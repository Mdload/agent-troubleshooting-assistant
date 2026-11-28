"""Router 的合成证据回放。"""
import re
from device_registry import DEVICES, ROUTER_CONTRACT
from device_tools import ROUTER_CHECKS
from ha_croc_case import croc_state, vip_state
from evidence_scope import collection_time


def read_commands(check):
    if not isinstance(check,str) or check not in ROUTER_CHECKS:raise ValueError('Unsupported Router check')
    return ROUTER_CHECKS[check]


def replay_observation(observations,scope,device,check):
    """仅接受回放明确标注的合成证据；不是当前设备事实入口。"""
    if check not in scope.get(device,()):return None
    rows=[item for item in observations if item.get('device')==device and item.get('check')==check]
    if len(rows)!=1:return None
    item=rows[0]
    if (item.get('ok') is not True or item.get('origin')!='synthetic' or item.get('freshness')!='synthetic'
            or not isinstance(item.get('output'),str) or len(item['output'])>16000
            or collection_time(item.get('collected_at')) is None):return None
    return item


def assess_replay(observations,scope):
    """离线解析单台采集字段，保留模式/时序/实际流量未知，不判断选举角色。"""
    nodes=[]
    for device in DEVICES:
        if DEVICES[device]['profile'] != 'router_ha':continue
        fields={}
        for check in ('services','interfaces','routes'):
            item=replay_observation(observations,scope,device,check)
            if item is None:continue
            if check=='services':value=croc_state(item['output'],'keepalived.service')
            elif check=='interfaces':value=vip_state(item['output'],ROUTER_CONTRACT['ha_vips'])
            else:
                values=re.findall(r'^IP_FORWARD=([01])$',item['output'],re.M)
                value=values[0] if len(values)==1 else 'unknown'
            if value=='unknown':continue
            fields[check]={'value':value,'refs':[item['id']],'collected_at':item['collected_at']}
        nodes.append({'device':device,'label':DEVICES[device]['label'],'fields':fields,
            'root_cause_confirmed':False,'current_role':'unknown','execution_enabled':False})
    return {'kind':'synthetic_router_contract_replay','nodes':nodes,
            'configuration_verified_current':False,'grants_device_access':False,
            'limits':'各项为顺序合成观测，不确认同时持有、现场角色、当前模式或端到端业务。'}
