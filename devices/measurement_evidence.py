"""Bounded typed ICMP facts and finite, citation-scoped numeric consistency checks.

This is a guard for explicit counts, not a natural-language truth classifier.
"""
import ipaddress
import re

from devices.device_registry import DEVICES, PING_TARGETS, TUNNEL_CHECKS, PROBE_CHECKS, tunnel_endpoints
from devices.diagnostic_probes import ping_counts, ping_route
from legacy.claim_text import prose


def project_measurement(device, check, output):
    if check not in PROBE_CHECKS:
        return None
    target=tunnel_endpoints(device,check)[1] if check in TUNNEL_CHECKS else DEVICES[PING_TARGETS[check]]['address']
    value={'kind':'icmp','source_device':device,'target_ip':target}
    counts=ping_counts(output)
    route=ping_route(output)
    if counts and (check not in TUNNEL_CHECKS or route.get('interface')=='tun16'):
        value.update(outcome='measured',sent=counts[0],received=counts[1])
        safe_route={}
        for key in ('source','gateway'):
            try:safe_route[key]=str(ipaddress.IPv4Address(route[key]))
            except (KeyError,ValueError):pass
        if re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}',route.get('interface','')):
            safe_route['interface']=route['interface']
        if safe_route:value['route']=safe_route
        return value
    if output.strip()=='ICMP_ROUTE_UNAVAILABLE=1':
        return dict(value,outcome='route_unavailable')
    if check in TUNNEL_CHECKS and output.strip()=='ICMP_TUNNEL_PATH_MISMATCH=1':
        return dict(value,outcome='tunnel_path_mismatch')
    return None


NUMBER=r'(?:\d{1,3}|零|一|二|两|三)'
NUMBERS={'零':0,'一':1,'二':2,'两':2,'三':3}
REF=re.compile(r'\[(OBS-[1-9]\d*)\]')


def number(value):
    return NUMBERS[value] if value in NUMBERS else int(value)


def count_claims(clause):
    """Keep each finite assertion's metric, value and local polarity."""
    if re.search(r'如果|假设|例如|示例|可能|是否|能否|不代表|不等于|不能(?:据此)?(?:证明|说)|'
                 r'无法(?:确认|证明)|尚未(?:确认|核实)',clause):
        return []
    claims=[]
    ratios=[]
    for match in re.finditer(r'(?<![\d./])('+NUMBER+r')\s*/\s*('+NUMBER+r')(?![\d./])',clause):
        ratios.append((match.start(),match.end()))
        labels=list(re.finditer(r'丢包|损失|未应答|未收到应答|应答|回包|收到|接收|发包|发送',clause[:match.start()]))
        label=labels[-1][0] if labels else ''
        metric='lost' if label in ('丢包','损失','未应答','未收到应答') else 'sent' if label in ('发包','发送') else 'received'
        negative=bool(re.search(r'(?:并非|不是|不等于)\s*$',clause[:match.start()]))
        claims.append({'metric':metric,'value':number(match[1]),'denominator':number(match[2]),'negative':negative})
    for metric,pattern in (
            ('sent',r'(?:发出|发送|发了|发包|已发|传送)(?:了)?\s*('+NUMBER+r')\s*(?:个|次)?\s*(?:包|探测|ICMP)'),
            ('received',r'(?:收到|接收到|应答|回包|回应)(?:了)?\s*('+NUMBER+r')\s*(?:个|次)?'),
            ('lost',r'(?:丢包|损失)(?:了)?\s*('+NUMBER+r')\s*(?:个|次|包)?')):
        for match in re.finditer(pattern,clause,re.I):
            if any(match.start(1)<end and match.end(1)>start for start,end in ratios):continue
            claims.append({'metric':metric,'value':number(match[1])})
    all_replied=re.search(r'(?:全部|全都|均|都)(?:成功)?(?:收到|收到了|获得)(?:了)?(?:应答|回复|回应|回包)|'
                 r'(?:包|探测)(?:全部|全都|均|都)(?:有|得到|获得)(?:应答|回复|回应)|'
                 r'(?:全部|全都)(?:应答|回包)',clause)
    if all_replied:
        negative=bool(re.search(r'(?:并未|未|没(?:有)?|并非|不是)\s*$',clause[:all_replied.start()]))
        claims.append({'metric':'all_replied','value':not negative})
        counted=re.search(r'('+NUMBER+r')\s*(?:个|次)?\s*(?:包|探测)\s*(?:并未|未|没有|并非|不是)?\s*(?:全部|全都|均|都)',clause)
        if counted:claims.append({'metric':'sent','value':number(counted[1])})
    if re.search(r'(?:未|没(?:有)?)(?:收到|接收到)(?:任何)?(?:应答|回复|回应|回包)|没有(?:应答|回包)|'
                 r'一(?:个)?包(?:也|都)?未收',clause):
        claims.append({'metric':'received','value':0})
    return claims


def supports_claims(fact, claims):
    if fact.get('outcome')!='measured':return False
    values={'sent':fact['sent'],'received':fact['received'],'lost':fact['sent']-fact['received'],
            'all_replied':fact['received']==fact['sent']}
    for claim in claims:
        same=values[claim['metric']]==claim['value']
        if 'denominator' in claim:same=same and fact['sent']==claim['denominator']
        if same==claim.get('negative',False):return False
    return True


def measurement_claim_issues(answer, observations, answer_refs):
    facts={row['id']:row['measurement'] for row in observations
           if row.get('ok') is True and row.get('measurement',{}).get('kind')=='icmp'}
    if not facts:return []
    issues=[]
    # Citations immediately after a sentence belong to that sentence as a group.
    text=re.sub(r'([。；;！!])([^\S\n]*(?:\[OBS-[1-9]\d*\][^\S\n]*)+)',
                lambda m: m[2].strip()+m[1],prose(answer))
    for sentence in re.split(r'[。；;！!\n]',text):
        sentence=re.sub(r'但是|然而|不过|但|而(?:实际|事实上)',',',sentence)
        sentence_refs=REF.findall(sentence)
        for match in re.finditer(r'[^，,]+',sentence):
            clause=match[0]
            claims=count_claims(clause)
            if not claims:continue
            refs=REF.findall(clause)
            if not refs and sentence_refs:
                # A following citation grounds an immediately preceding numeric
                # clause; a citation on another sentence cannot qualify it.
                following=REF.search(sentence,match.end())
                refs=[following[1]] if following else sentence_refs[-1:]
            if not refs:refs=answer_refs
            selected=[(ref,facts[ref]) for ref in refs if ref in facts]
            if not selected:
                if len(facts)==1:selected=list(facts.items())
                else:
                    issues.append({'id':'icmp_measurement_source_required','refs':sorted(facts),
                                   'statement':'此明确测量结论必须定位到对应 OBS；不能省略引用来绕过数值核对。'})
                    continue
            universal=bool(re.search(r'(?:这|那|上述)?(?:两|三|四|多|[2-9])(?:项|次|条)[^，,。；]{0,24}(?:都|均)|'
                                     r'所有(?:的)?(?:探测|测量|检查)|(?:各|每)(?:项|次|条)(?:探测|测量|检查)?',clause))
            accepted=[supports_claims(fact,claims) for _,fact in selected]
            if all(accepted) if universal else any(accepted):continue
            for (ref,fact),accepted_fact in zip(selected,accepted):
                if universal and accepted_fact:continue
                issue={'id':'icmp_measurement_mismatch','ref':ref,'measurement':fact,
                       'statement':'按此原始测量纠正发包数、应答数和全收结论；未发包没有计数。'}
                if issue not in issues:issues.append(issue)
    return issues
