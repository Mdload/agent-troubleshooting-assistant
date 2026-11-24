"""Croc案例的固定替身：只用于离线测试，不代表实机或真实模型输出。"""
from device_registry import profile
from workflow_test_support import current_probe_fixture
from ha_croc_case import assess_case

VARIANTS=('nonowner_stopped','owner_stopped','nonowner_running','owner_running','partial_vip','missing_unit','read_failure')


def case_probe(variant):
    if variant not in VARIANTS: raise ValueError('Unknown fixture')
    def probe(device,check):
        result=current_probe_fixture(device,check);p=profile(device)
        running=variant in ('nonowner_running','owner_running')
        if check=='services':
            if variant=='read_failure': return dict(result,ok=False,output='',error='timeout')
            result['output']=('MainPID=123\nId=keepalived.service\nLoadState=loaded\nActiveState=active\nSubState=running\n\n'
                'MainPID='+('456' if running else '0')+'\nId='+p['croc_unit']+
                '\nLoadState='+('not-found' if variant=='missing_unit' else 'loaded')+
                '\nActiveState='+('active' if running else 'inactive')+'\nSubState='+('running' if running else 'dead')+'\nResult=success\n')
        elif check=='interfaces':
            owner=variant in ('owner_stopped','owner_running','missing_unit','read_failure')
            rows=[]
            for index,(interface,vip) in enumerate(p['ha_vips'].items()):
                held=owner or variant=='partial_vip' and index==0
                rows.append(interface+' UP'+(' '+vip+'/24' if held else ''))
                rows.append(interface+' UP 00:11:22:33:44:55 <BROADCAST,UP>')
            rows.append('INTERFACE_ABSENT=tun16')
            result['output']='\n'.join(rows)
        elif check=='logs': result['output']='TEST FIXTURE: historical startup event; not a current role.'
        elif check=='routes': result['output']='TEST FIXTURE: main routes only; end-to-end not tested.'
        return result
    return probe


def case_planner(context):
    """按宿主建议演练反馈循环；不是LLM，不能用于模型准确率。"""
    report=context['diagnostic_case']
    pending=report['next_checks']
    if pending:
        first=pending[0];pair=[(x['device'],x['check']) for x in pending]
        if first['check']=='services' and (first['device'],'interfaces') in pair:
            return {'action':'read_overview','device':first['device']},0
        return dict(first,action='read'),0
    texts=[node['summary']+'建议：'+node['repair_advice'] for node in report['nodes']]
    refs=report['required_refs'] or [o['id'] for o in context['observations'] if o.get('ok') and o.get('freshness')=='current' and o.get('check')!='logs']
    return {'action':'answer','answer':'\n\n'.join(texts)+'\n\n'+report['assumption']+
            '完整根因和业务通畅仍未确认，未执行修复。','refs':refs,'basis':'evidence' if refs else 'limited'},0
