"""比较本人同一对话的两次历史采集；变化不等于修复、根因或当前健康。"""
import hashlib
import json
from device_registry import DEVICES,profile
from evidence_scope import collection_time


def object_fingerprint(device,check):
    item=DEVICES[device]
    identity={key:item[key] for key in ('address','hostname','profile')}
    identity.update(device=device,check=check)
    p=profile(device)
    identity['definition']=p['croc_unit'] if check=='services' else p['ha_vips']
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode('utf-8')).hexdigest()


def snapshot_fields(task):
    result=task.get('result') or {}
    report=result.get('diagnostic_report') or {}
    fields={};seen=set()
    for fact in report.get('facts',[]):
        device,check=fact.get('device'),fact.get('check')
        if (device not in task['scope'] or check not in task['scope'][device]
            or check not in ('services','interfaces')):continue
        key=(device,check)
        if key in seen:
            fields[key]=None;continue
        seen.add(key)
        refs=fact.get('refs',[])
        evidence=[o for o in result.get('evidence',[]) if o.get('id') in refs and
                  o.get('device')==device and o.get('check')==check]
        if (len(refs)!=1 or len(evidence)!=1 or fact.get('origin')!='device_read'
            or fact.get('freshness')!='current' or not fact.get('object_fingerprint')):continue
        item=evidence[0]
        if (item.get('ok') is not True or item.get('freshness')!='current'
            or item.get('origin')!='device_read' or item.get('collected_at')!=fact.get('collected_at')
            or collection_time(item.get('collected_at')) is None
            or not isinstance(item.get('output'),str) or len(item['output'])>16000):continue
        allowed=('running','stopped') if check=='services' else ('owner','partial','non_owner')
        if fact.get('value') not in allowed:continue
        fields[key]={'task_id':task['id'],'ref':refs[0],'collected_at':fact['collected_at'],
            'value':fact['value'],'text':fact['text'],'object_fingerprint':fact['object_fingerprint'],
            'raw_output':item['output']}
    return fields


def compare_snapshots(before,after):
    if before['id']==after['id']:raise ValueError('comparison_same_task')
    if before.get('conversation_id')!=after.get('conversation_id') or before.get('scene')!=after.get('scene'):
        raise ValueError('comparison_different_context')
    if any(t.get('status') in ('pending','approved','running') or not t.get('result') for t in (before,after)):
        raise ValueError('comparison_not_ready')
    left,right=snapshot_fields(before),snapshot_fields(after)
    common={(device,check) for device in before['scope'] if device in after['scope']
            for check in before['scope'][device] if check in after['scope'][device]
            and check in ('services','interfaces')}
    entries=[]
    for device,check in sorted(common):
        a,b=left.get((device,check)),right.get((device,check))
        reason=None
        if not a or not b:reason='missing_or_unverified_snapshot'
        elif a['object_fingerprint']!=b['object_fingerprint']:reason='object_definition_changed'
        elif collection_time(a['collected_at'])>=collection_time(b['collected_at']):reason='time_not_increasing'
        entries.append({'device':device,'check':check,
            'status':'unavailable' if reason else 'unchanged' if a['value']==b['value'] else 'changed',
            'reason':reason,'before':a,'after':b})
    return {'kind':'historical_snapshot_comparison','before_task':before['id'],'after_task':after['id'],
        'entries':entries,'comparable_fields':sum(row['status']!='unavailable' for row in entries),
        'changed_fields':sum(row['status']=='changed' for row in entries),
        'current_state_verified':False,'recovery_verified':False,'root_cause_confirmed':False,
        'device_reads':0,'model_api_calls':0,'grants_device_access':False,
        'note':'仅比较两次各自采集时刻的状态；不证明中间过程、当前状态、故障根因或业务已恢复。'
               '缺少旧版对象标识、有效证据或正向时间顺序的项目不计算变化。'}
