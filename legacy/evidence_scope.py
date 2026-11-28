"""宿主生成证据性质/范围，不让模型把局部读取升级为整体验收。"""
import datetime
from devices.device_tools import DEVICES, DEVICE_LABELS
from devices.device_registry import EXTRA_CHECK_LABELS

CHECK_LABELS={'services':'服务状态','interfaces':'接口/VIP/监听或连接','routes':'主路由与策略规则',
              'logs':'固定服务的限量历史日志',**EXTRA_CHECK_LABELS}

def collection_time(value):
    if not isinstance(value,str): return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ','%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.datetime.strptime(value,fmt)
        except ValueError: pass
    return None

def valid_collection_time(value):
    return collection_time(value) is not None

def mark_observation(item,device,check,read_window=None):
    """从可信工具返回的标识/采集时间判断性质，忽略原始origin自报。"""
    result=dict(item)
    output=result.get('output','')
    if (result.get('collected_at')=='SIMULATION' or
        isinstance(output,str) and output.startswith('[合成测试快照')):
        origin='synthetic'
    elif (result.get('address')==DEVICES[device][0] and
          valid_collection_time(result.get('collected_at'))):
        origin='device_read'
    else: origin='unconfirmed'
    result.update(origin=origin,supports=CHECK_LABELS[check])
    if check == 'logs':
        result['evidence_role']='event_history'
    if read_window is not None:
        # worker使用采集端UTC；设备自身时间、旧文件日期和origin自报都不算本轮证据。
        collected=collection_time(result.get('collected_at'))
        began,finished=read_window
        tolerance=datetime.timedelta(seconds=1)  # 兼容只精确到秒的时间串。
        if origin=='synthetic': freshness='synthetic'
        elif origin!='device_read' or result.get('clock_source')!='collector_utc': freshness='unconfirmed'
        elif collected < began-tolerance or collected > finished+tolerance: freshness='stale'
        else: freshness='current'
        result['freshness']=freshness
    return result

def build_scope(observations):
    if not observations: return None
    all_synthetic=all(o.get('origin')=='synthetic' for o in observations)
    good=[o for o in observations if o.get('ok') is True]
    origins={o.get('origin','unconfirmed') for o in good}
    kind=next(iter(origins)) if len(origins)==1 else 'mixed' if origins else 'failed'
    fresh=[o for o in good if o.get('freshness')=='current']
    if kind=='device_read' and any('freshness' in o for o in good):
        if not fresh: kind='stale'
        elif len(fresh)!=len(good): kind='mixed'
    labels=[DEVICE_LABELS[o['device']]+'的'+CHECK_LABELS[o['check']] for o in good]
    if kind=='synthetic': note='合成状态回放，未读取真实设备。'
    elif kind=='unconfirmed': note='读取结果的来源性质未确认，不能视为已核实的现场状态。'
    elif kind=='mixed': note='读取结果含不同性质的证据，请按各条来源标记区分。'
    elif kind=='stale': note='未取得时间与来源均符合本轮读取要求的证据，当前设备状态未核实。'
    elif kind=='failed': note=('合成状态回放，未读取真实设备。' if all_synthetic else '')+'本轮设备读取未成功，现场状态未核实。'
    else: note='设备读取结果仅代表各条采集时刻的局部状态，不保证阅读时状态未发生变化。'
    if labels: note+='已读取：'+'、'.join(labels)+'。'
    if any(o.get('check')=='logs' for o in good):
        note+='日志仅支持对应历史事件；设备日志时间未校准，不能据此确认当前角色、服务状态或排除故障。'
    note+='完整HA健康与端到端业务尚未验证。'
    return {'kind':kind,'note':note,'all_synthetic':all_synthetic,'successful_reads':len(good),'current_reads':len(fresh),
            'failed_reads':len(observations)-len(good),
            'checked':[{'id':o['id'],'device':o['device'],'check':o['check'],
                        'origin':o.get('origin','unconfirmed'),'collected_at':o.get('collected_at'),
                        'freshness':o.get('freshness','unconfirmed')} for o in good],
            'unverified':['完整HA健康','端到端业务可用']}

def scope_prefix(scope):
    if not scope: return ''
    if scope['kind']=='synthetic' or scope.get('all_synthetic'): return '以下为合成状态回放，不代表真实设备现状。\n\n'
    if scope['kind']=='mixed':
        return '本回答包含不同性质的读取结果，不能合并视为已核实的现场状态。\n\n'
    if scope['kind']=='unconfirmed':
        return '部分读取结果的来源性质未确认，请勿视为已核实的现场状态。\n\n'
    if scope['kind']=='stale':
        return '读取结果未满足本轮采集要求，不能用来确认当前设备状态。\n\n'
    return ''
