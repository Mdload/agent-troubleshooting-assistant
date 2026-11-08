"""统一聊天工具循环；权限、预算和引用由宿主校验。"""
import json
import re
import time
import uuid
import qa
from device_tools import DEVICES, CHECKS, tool_catalog
from diagnostic_core import AuditStore
from diagnostic_web import SLOT, ROOT, remote_probe
from chat_model import make_model, ModelFailure
from model_trace import traced_plan
from evidence_scope import mark_observation, build_scope, scope_prefix, CHECK_LABELS
from device_tools import DEVICE_LABELS
from mechanism_guard import mechanism_constraints, observation_constraints, contradicted_constraints

class ProtocolFailure(ValueError):
    """可纠正的回答格式错误；工具权限与预算错误仍立即停止。"""
    def __init__(self, code):
        self.code=code
        super().__init__(code)

class MechanismFailure(ProtocolFailure):
    def __init__(self, issues, code='unsupported_mechanism_claim'):
        super().__init__(code)
        self.issues=issues

class ToolRejection(ValueError):
    """只记录拒绝类别，不存模型给出的任意目标、命令或查询。"""
    def __init__(self, code):
        self.code=code
        super().__init__(code)

def correction_feedback(code, sources, observations, issues=None):
    valid={s['id'] for s in sources}|{o['id'] for o in observations if o.get('ok') is True}
    feedback={'code':code,'valid_refs':sorted(valid),
            'message':'上一响应未通过格式或引用校验。请重新输出规定的JSON；只引用valid_refs，'
                      '不要重复已完成的工具，也不要声称未成功执行的检查。'}
    if code=='escaped_answer_layout':
        feedback['message']='正文把换行写成了文字转义。请用合法JSON转义形成实际换行，保留原结论与引用，不再调用工具。代码字符串和Windows路径中的反斜杠不得改写，相关例子放在代码块或行内代码中。'
    if issues:
        feedback.update(constraints=issues,
                        message='上一回答包含与本轮规则或观测范围冲突的明确结论。请按constraints纠正，'
                                '保留成立条件与未核验项；只引用valid_refs，不重复工具。')
    return feedback

def needs_topology(question):
    q=qa.normalize_query(question).lower()
    return qa.health_counter_context(q) or any(t in q for t in ('实验室','局端','远端','router','路由器','croc','watchdog','recv-q',
                               'max_owner_failures','preempt_delay','9965','主备','备机',
                               '拓扑','keepalived','tun16','ppp','owner-fault','vrrp','主机','recv-q'))

def new_general_topic(question):
    return not needs_topology(question) and any(t in question.lower() for t in
            ('python','fastapi','gradio','javascript','java','元组','列表','字典','装饰器','生成器'))

def is_followup(question):
    q=question.strip()
    if len(q)>100 or new_general_topic(q): return False
    if re.match(r'^(那|那么|它|他们|它们|这个|这种|这样|还有|具体|继续|再解释)',q): return True
    if re.fullmatch(r'(为什么|为何)(呢)?[？?。!！]*',q): return True
    return bool(re.match(r'^(为什么|为何)',q) and
                (needs_topology(q) or any(t in q for t in
                 ('这','它','业务','恢复','切换','接管','降权','故障','健康','选举','队列','接口','通告','服务'))))

def topic_groups(question):
    q=question.lower()
    groups=[]
    if '局端' in q: groups.append('local')
    if '远端' in q: groups.append('remote')
    if 'router' in q or '路由器' in q: groups.append('router')
    return groups

def query_group(question):
    groups=topic_groups(question)
    return groups[0] if len(groups)==1 else 'unknown'

def retrieval_group(query):
    group=query_group(query.rsplit('\n追问：',1)[-1])
    return group if group!='unknown' else query_group(query)

def retarget_topic(text, group):
    # 单一设备话题转向另一端；多端关系问题保留原范围。
    previous=query_group(text)
    if group=='unknown' or previous in ('unknown',group): return text
    label={'local':'局端','remote':'远端','router':'Router'}[group]
    return re.sub({'local':'局端','remote':'远端','router':r'router|路由器'}[previous],label,text,flags=re.I)

def prefetch_query(question, history):
    if new_general_topic(question): return None
    if not is_followup(question): return question if needs_topology(question) else None
    previous=[];anchor=None;fallback=None
    for message in reversed(history):
        if message['role']!='user': continue
        text=message['content']
        if new_general_topic(text): break
        if needs_topology(text) and not is_followup(text): anchor=text;break
        if is_followup(text):
            previous.append(text)
            if needs_topology(text) and fallback is None: fallback=text
            continue
        break  # 遇到无关完整问题，不向前翻旧话题。
    anchor=anchor or fallback
    if not anchor: return question if needs_topology(question) else None
    group=query_group(question)
    if not topic_groups(question):
        for text in previous:
            if topic_groups(text):
                group=query_group(text);break
    parts=[retarget_topic(anchor,group)[:600]]
    parts += [retarget_topic(text,group)[:100] for text in reversed(previous[:2]) if text!=anchor]
    return '\n追问：'.join(parts+[question])[:1000]

def validate_history(history):
    if not isinstance(history,list) or len(history)>8: raise ValueError('Invalid history')
    cleaned=[]
    for message in history:
        if (not isinstance(message,dict) or set(message)!={'role','content'} or
            message['role'] not in ('user','assistant') or not isinstance(message['content'],str) or
            len(message['content'])>3000): raise ValueError('Invalid history')
        cleaned.append(dict(message))
    if sum(len(m['content']) for m in cleaned)>16000: raise ValueError('History too large')
    return cleaned

def search(query):
    fields=('id','title','kind','operating_modes','device_groups','symptom',
            'historical_finding','historical_outcome','verification_limits','evidence_date',
            'curation_status','live_verified')
    hits=qa.retrieve(query,group=retrieval_group(query))
    selected=hits[:4]
    # 排名截断不能丢掉旧故障的冲突/恢复说明。
    if any(h['case']['id'] in ('HA-001','HA-002','HA-007') for h in selected):
        conflict=next((h for h in hits if h['case']['id']=='KB-011'),None)
        if conflict and not any(h['case']['id']=='KB-011' for h in selected):
            selected.append(conflict)
    # 模式和资料性质也必须到达模型，不能只保留编号与正文。
    return [dict({k:h['case'][k] for k in fields if k in h['case']},
                 kind=h['case'].get('kind','historical_case')) for h in selected]

def evidence_digest(observations):
    """只概述实际成功/失败项目；不解析原始文本来猜故障根因。"""
    if not observations:return ''
    good=[];bad=[]
    for item in observations:
        label=DEVICE_LABELS[item['device']]+'的'+CHECK_LABELS[item['check']]
        (good if item.get('ok') is True else bad).append(label+('（'+item['id']+'）' if item.get('ok') is True else ''))
    text=''
    if good:text+='\n\n已取得：'+'、'.join(good)+'。上述为局部观测，尚未完成原因判断。'
    if bad:text+='\n\n未取得：'+'、'.join(bad)+'。读取失败不能证明设备损坏；其他未查节点和端到端业务仍未知。'
    return text

def run(question, history, planner, lookup, probe, audit, clock=time.monotonic):
    task=uuid.uuid4().hex
    sources, observations, events=[],[],[]
    model_trace=[]
    seen=set(); searches=reads=tokens=0; deadline=clock()+180
    tokens_complete=True
    corrections=0; feedback=None
    audit.add(task,0,'start',{'kind':'unified_chat'})
    # 实验室专有问题先由宿主补证据，不赌模型是否主动检索。仍允许继续改写查询。
    query=prefetch_query(question, history)
    scope_group=retrieval_group(query) if query else query_group(question)
    if query:
        try:
            sources=lookup(query)
            searches=1;seen.add(('search',query))
            event={'action':'search','count':len(sources),'ids':[s['id'] for s in sources],'trigger':'host_prefetch'}
            events.append(event);audit.add(task,0,'tool',event)
        except Exception:
            events.append({'action':'search','count':0,'ids':[],'error':'retrieval_failed'})
    reason='step_budget_reached'
    for step in range(7):
        now=clock()
        if now>=deadline: reason='time_budget_reached'; break
        context={'remaining_seconds':max(0.001,round(deadline-now,3)),'question':question,'history':history,'sources':sources,'observations':observations,
                 'tool_events':events,'tool_catalog':tool_catalog(),
                 'remaining_searches':2-searches,'remaining_reads':4-reads,
                 'correction_feedback':feedback,'remaining_corrections':1-corrections,
                 'evidence_scope':build_scope(observations),
                 'observation_constraints':observation_constraints(observations),
                 'mechanism_constraints':mechanism_constraints(sources,scope_group)}
        try:
            action, usage=traced_plan(planner,json.loads(json.dumps(context)),model_trace,audit,task,step+1)
            if type(usage) is int and usage>=0: tokens+=usage
            else: tokens_complete=False
            if clock()>=deadline: reason='time_budget_reached'; break
            if not isinstance(action,dict): raise ProtocolFailure('invalid_action_format')
            kind=action.get('action')
            if kind not in ('answer','search','read','read_overview'): raise ProtocolFailure('invalid_action_format')
            if kind=='answer':
                answer=action.get('answer'); refs=action.get('refs'); basis=action.get('basis')
                valid={s['id'] for s in sources}|{o['id'] for o in observations if o.get('ok') is True}
                if (set(action)!={'action','answer','refs','basis'} or not isinstance(answer,str) or
                    not 1<=len(answer.strip())<=3000 or basis not in ('general','evidence','limited') or
                    not isinstance(refs,list)): raise ProtocolFailure('invalid_answer_format')
                if answer.count('\\n')>=3 and '\\n\\n' in answer and '`' not in answer:
                    raise ProtocolFailure('escaped_answer_layout')
                inline_refs=re.findall(r'\[((?:HA|KB|OBS)-\d+)\]',answer)
                if (
                    any(not isinstance(r,str) or r not in valid for r in refs) or
                    any(r not in refs for r in inline_refs) or
                    (basis=='evidence' and not refs)): raise ProtocolFailure('invalid_citation')
                refs=list(dict.fromkeys(refs))
                issues=contradicted_constraints(answer,context['mechanism_constraints'])
                if issues: raise MechanismFailure(issues)
                issues=contradicted_constraints(answer,context['observation_constraints'])
                if issues: raise MechanismFailure(issues,'unsupported_observation_claim')
                scope=build_scope(observations)
                answer=scope_prefix(scope)+answer
                if len(answer)>3000: raise ProtocolFailure('invalid_answer_format')
                audit.add(task,step+1,'finish',{'refs':refs,'basis':basis,'total_tokens':tokens,
                                             'total_tokens_complete':tokens_complete})
                return {'status':'answered','answer':answer,'refs':refs,'basis':basis,'sources':sources,
                        'evidence':observations,'events':events,'total_tokens':tokens,
                        'total_tokens_complete':tokens_complete,'task':task,'evidence_scope':scope,'model_trace':model_trace}
            if kind=='search' and set(action)=={'action','query'}:
                query=action['query']
                if not isinstance(query,str) or not 1<=len(query)<=1000:
                    raise ToolRejection('invalid_search_request')
                if searches>=2: raise ToolRejection('search_budget_reached')
                key=('search',query)
                if key in seen:
                    if len(events)==1 and events[0].get('trigger')=='host_prefetch':
                        events.append({'action':'search','count':len(sources),'ids':[s['id'] for s in sources],'trigger':'reuse_prefetch'})
                        continue
                    raise ToolRejection('duplicate_search')
                seen.add(key); searches+=1
                found=lookup(query)
                existing={s['id'] for s in sources}
                sources.extend(s for s in found if s['id'] not in existing)
                event={'action':'search','count':len(found),'ids':[s['id'] for s in found]}
            elif kind=='read' and set(action)=={'action','device','check'}:
                device,check=action['device'],action['check']
                if not isinstance(device,str) or device not in DEVICES:
                    raise ToolRejection('unregistered_device')
                if not isinstance(check,str) or check not in CHECKS:
                    raise ToolRejection('unsupported_check')
                if reads>=4: raise ToolRejection('read_budget_reached')
                key=('read',device,check)
                if key in seen: raise ToolRejection('duplicate_read')
                checks=[check]
            elif kind=='read_overview' and set(action)=={'action','device'}:
                device=action['device']
                if not isinstance(device,str) or device not in DEVICES:
                    raise ToolRejection('unregistered_device')
                checks=['services','interfaces']
                # 整批先校验；一项越界、重复或超限时，整批都不执行。
                if reads+len(checks)>4: raise ToolRejection('read_budget_reached')
                if any(('read',device,c) in seen for c in checks): raise ToolRejection('duplicate_read')
            elif kind in ('read','read_overview'): raise ToolRejection('invalid_read_request')
            elif kind=='search': raise ToolRejection('invalid_search_request')
            else: raise ValueError()
            feedback=None
            if kind in ('read','read_overview'):
                for check in checks:
                    if clock()>=deadline:
                        reason='time_budget_reached';break
                    seen.add(('read',device,check));reads+=1
                    try: item=dict(probe(device,check))
                    except Exception: item={'ok':False,'output':'','error':'tool_unavailable'}
                    item=mark_observation(item,device,check)
                    item.update(device=device,check=check,id='OBS-'+str(reads))
                    observations.append(item)
                    event={'action':'read','device':device,'check':check,'ok':item.get('ok',False)}
                    if kind=='read_overview': event['group']='overview'
                    audit.add(task,step+1,'observation',item)
                    events.append(event);audit.add(task,step+1,'tool',event)
                if reason=='time_budget_reached':break
                recent=observations[-2:]
                if (len(recent)==2 and not any(o.get('ok') is True for o in observations) and
                    all(o['device']==device and o.get('ok') is not True for o in recent) and
                    {o['check'] for o in recent}=={'services','interfaces'}):
                    reason='device_evidence_unavailable';break
            else:
                events.append(event); audit.add(task,step+1,'tool',event)
        except (ProtocolFailure, ModelFailure) as error:
            if isinstance(error,ModelFailure):
                if error.usage is None: tokens_complete=False
                else: tokens+=error.usage
                reason=error.code
            else: reason=error.code if isinstance(error,MechanismFailure) else 'invalid_action_or_citation'
            recoverable=isinstance(error,ProtocolFailure) or error.code in ('model_invalid_json','model_invalid_response')
            # 一轮最多纠正一次，仍在原7步/180秒预算内；不重试超时、限流或越界工具。
            if recoverable and corrections==0 and step<6 and clock()<deadline:
                corrections+=1; feedback=correction_feedback(error.code,sources,observations,
                                                             getattr(error,'issues',None))
                event={'action':'correction','reason':error.code}
                if isinstance(error,MechanismFailure):
                    event['constraints']=[issue['id'] for issue in error.issues]
                events.append(event); audit.add(task,step+1,'correction',event)
                continue
            break
        except ToolRejection as error:
            event={'action':'rejected','reason':error.code}
            events.append(event);audit.add(task,step+1,'rejected',event)
            reason='invalid_action_or_citation';break
        except (ValueError, TypeError, KeyError):
            reason='invalid_action_or_citation'; break
        except Exception:
            reason='planner_or_tool_unavailable'; break
    audit.add(task,8,'stop',{'reason':reason,'total_tokens':tokens,
                          'total_tokens_complete':tokens_complete})
    messages={'model_timeout':'模型响应超时','model_output_truncated':'模型输出被截断',
        'model_invalid_json':'模型返回格式无法解析','model_rate_limited':'模型接口限流',
        'model_connection_failed':'模型连接失败','model_http_error':'模型接口请求失败',
        'model_invalid_response':'模型响应结构不完整',
        'invalid_action_or_citation':'工具选择或引用未通过校验','time_budget_reached':'本次处理达到时间上限',
        'unsupported_mechanism_claim':'回答仍包含与已提供规则冲突的结论',
        'unsupported_observation_claim':'回答仍超出本轮局部观测支持的范围，尚不能完成诊断',
        'device_evidence_unavailable':'该设备服务与接口两项基础检查均未成功，本轮状态无法核实',
        'step_budget_reached':'本次处理达到步骤上限','planner_or_tool_unavailable':'模型或工具服务暂不可用'}
    scope=build_scope(observations)
    text=messages.get(reason,'本次未能完成回答')+'；已取得的依据和检查过程保留在下方。'
    if observations and not any(item.get('ok') is True for item in observations):
        text+='本轮设备状态未核实，不能据此判断设备或业务故障。'
    text+=evidence_digest(observations)
    if observations and all(item.get('origin')=='synthetic' for item in observations):
        text='以下为合成状态回放，不代表真实设备现状。\n\n'+text
    else: text=scope_prefix(scope)+text
    return {'status':'incomplete','answer':text,
            'reason':reason,'basis':'limited','refs':[o['id'] for o in observations if o.get('ok') is True],
            'sources':sources,'evidence':observations,'events':events,'total_tokens':tokens,
            'total_tokens_complete':tokens_complete,'task':task,'evidence_scope':scope,'model_trace':model_trace}

def chat(question, history, consent):
    if consent is not True:
        return {'status':'consent_required','answer':'首次使用请先确认下方本次会话的数据使用范围。','events':[]}
    if not SLOT.acquire(False):
        return {'status':'busy','answer':'已有请求正在处理，请稍后再试。','events':[]}
    try:
        return run(question,validate_history(history),make_model(qa.model_config()),search,remote_probe,
                   AuditStore(ROOT/'data/diagnostics.sqlite3'))
    except Exception:
        return {'status':'incomplete','answer':'模型或审计服务暂不可用，请稍后重试。','events':[]}
    finally: SLOT.release()
