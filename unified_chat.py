"""统一聊天工具循环；权限、预算和引用由宿主校验。"""
import json
import datetime
import re
import time
import uuid
import qa
from device_tools import DEVICES, CHECKS, tool_catalog
from diagnostic_core import AuditStore, audit_text, request_trace
from diagnostic_web import SLOT, ROOT, remote_probe
from chat_model import make_model, ModelFailure
from model_trace import traced_plan
from evidence_scope import mark_observation, build_scope, scope_prefix, CHECK_LABELS
from device_tools import DEVICE_LABELS
from mechanism_guard import mechanism_constraints, observation_constraints, contradicted_constraints
from knowledge_coverage import assess, notice, answer_issue, assessment_query
from lab_basics import is_basics_query
from knowledge_catalog import query_scope, filter_sources, describe_scope, METADATA_FIELDS
from ha_croc_case import selected as case_selected, assess_case, answer_issues as case_answer_issues
from diagnostic_report import build_report, investigation_plan, check_purpose
from investigation_limits import legacy_limits, validate_limits, stage_budget
from business_path import selected as business_path_selected, assess_path, attach as attach_path
from diagnostic_context import causal_issues
from diagnostic_goal import assess_goal, assess_icmp
from diagnostic_logs import echoes_log_credential, filter_log_output
from ha_runbooks import compile_runbook, menu as runbook_menu, next_check as runbook_next_check, summarize as runbook_summary
from diagnostic_intake import DIAGNOSTIC_GOALS, GOALS, UNAVAILABLE, expand_scope, question_key, update_brief
from device_registry import PING_TARGETS, PROBE_CHECKS, profile
from topic_scope import topic_groups, query_group, retarget_topic
from material_analysis import answer_issue as material_answer_issue,NOTICE as MATERIAL_NOTICE
from public_lookup import retrieve as retrieve_public, POLICY as PUBLIC_POLICY, answer_issue as public_answer_issue

from response_contract import response_policy, remote_rules, reference_link_rules, answer_issues as response_issues, plain_language_answer
from learning_intent import general_agent_question, general_ha_question, source_question, answer_discussion, documented_path_question
from claim_text import prose, qualified
from measurement_evidence import measurement_claim_issues

class ProtocolFailure(ValueError):
    """可纠正的回答格式错误；工具权限与预算错误仍立即停止。"""
    def __init__(self, code):
        self.code=code
        super().__init__(code)

class MechanismFailure(ProtocolFailure):
    def __init__(self, issues, code='unsupported_mechanism_claim'):
        super().__init__(code)
        self.issues=issues

class CurrentEvidenceFailure(ProtocolFailure):
    def __init__(self):
        super().__init__('current_evidence_required')

class KnowledgeFailure(ProtocolFailure):
    """相关命中不能替代直接支持，仍复用一次纠正上限。"""
    pass

class ToolRejection(ValueError):
    """只记录拒绝类别，不存模型给出的任意目标、命令或查询。"""
    def __init__(self, code):
        self.code=code
        super().__init__(code)

def observation_refs(observations,require_current=False):
    return {o['id'] for o in observations if o.get('ok') is True and
            (not require_current or o.get('freshness')=='current')}


def chat_evidence_scope(observations, historical):
    scope = build_scope(observations)
    if scope and historical:
        scope.update(kind='historical',
            note='仅引用已留存的检查结果，本轮没有重新读取。当前状态、实际业务收发和根因仍待验证。')
    return scope


def historical_claim_issues(answer):
    text = prose(answer)
    # Strictly nominal subjects may share a negative predicate. Completed
    # assertions (已恢复) and independent clauses cannot match this grammar.
    nominal=(r'(?:当前状态|(?:(?:当前|端到端|实际)(?:的)?)?业务(?:可用(?:与否)?|恢复)|'
             r'根因(?:是否(?:确定|确认))?)')
    enumeration=nominal+r'(?:(?:、|和|与)'+nominal+r')+(?:都|均)(?:还未|尚未|仍待|还没)(?:核实|确认)'
    text=re.sub(enumeration,'尚未核实',text)
    clauses = re.split(r'[。；;！!\n，,]|但是|不过|然而|但', text)
    current = (r'(?:当前|此刻|现在|目前|本轮|眼下|依旧|仍然|依然|仍旧)[^。；\n，,]{0,36}?'
               r'(?:恢复|中断|掉线|已通|没通|不通|畅通|正常|异常|可用|可达|'
               r'失联|未接管|在运行|已停止|已启动|没有应答|[没未]收到(?:回包|应答|回应)|'
               r'无法访问|状态良好|已核实)')
    uncertainty = (r'(?:无法|不能|不足以|尚未|未能|还没)(?:据此|用这些结果)?'
                   r'(?:证明|判断|确定|验证|确认|核实|检查)|没有证据|不代表|不等于|'
                   r'是否|能否|可否|可能|疑似|推测|不确定|尚不清楚|待核实|如果|假设|'
                   r'仅能说明当时|只知道当时')
    asserted_current = any(not re.search(uncertainty, phrase[:match.end()])
                           for phrase in clauses
                           for match in re.finditer(current,phrase))
    confirmed_cause = any(not qualified(phrase,match,uncertain=True)
                          for phrase in clauses for match in re.finditer(
                              r'根因(?:已经|已)?(?:确认|确定)|根因(?:就是|是)',phrase))
    # 限定只作用于同一个独立断言；前句“尚未确认根因”不能豁免后句恢复宣称。
    # “业务恢复通常需要多久”是概念/时长描述，没有完成体，不作为已恢复事实。
    completion = (r'故障[^。；;\n，,]{0,12}?(?:已经|已)(?:彻底|完全)?(?:排除|修复|解决)|'
                  r'业务[^。；;\n，,]{0,12}?(?:已经|已)(?:完全)?(?:恢复|畅通|可用|正常收发)|'
                  r'故障(?:排除|修复|解决)(?:完成|了)|'
                  r'业务(?:完全)?(?:恢复|畅通|可用|正常收发)(?:完成|了)|'
                  r'业务(?:完全)?(?:恢复(?:正常|通信|收发)?|畅通|可用|正常收发)'
                  r'(?=\s*(?:\[[A-Za-z0-9-]+\]\s*)*$)|'
                  r'(?:已经|已)恢复(?:业务|端到端收发)|'
                  r'(?:两端|端到端|实际)(?:的)?(?:业务)?[^。；;\n，,]{0,12}'
                  r'(?:完全畅通|正常收发|正常传输)')
    nominal_suffix = (r'后|前|时|与否|仍?(?:待核实|待确认)|'
                      r'(?:(?:通常|一般|往往)\s*)?(?:需要|取决于|所需|多久|条件|过程|方法|方案)')
    asserted_completion = any(not qualified(phrase,match,uncertain=True)
                              and not re.match(nominal_suffix,phrase[match.end():])
                              for phrase in clauses for match in re.finditer(completion,phrase))
    if asserted_current or confirmed_cause or asserted_completion:
        return [{'id':'historical_state_only',
                 'statement':'这些检查只说明当时的局部结果。当前状态、业务收发和根因仍待核实。'}]
    return []

def correction_feedback(code, sources, observations, issues=None,require_current=False):
    valid={s['id'] for s in sources}|observation_refs(observations,require_current)
    feedback={'code':code,'valid_refs':sorted(valid),
            'message':'上一响应未通过格式或引用校验。请重新输出规定的JSON；只引用valid_refs，'
                      '不要重复已完成的工具，也不要声称未成功执行的检查。'}
    if code=='escaped_answer_layout':
        feedback['message']='正文把换行写成了文字转义。请用合法JSON转义形成实际换行，保留原结论与引用，不再调用工具。代码字符串和Windows路径中的反斜杠不得改写，相关例子放在代码块或行内代码中。'
    if code=='current_evidence_required':
        feedback['message']='这是当前状态问题，历史资料或聊天不能代替本轮现场证据。请在批准范围内取得与问题相关的读取，并以freshness=current的成功OBS支持局部结论；未查部分必须标明未知。无法取证时用limited说明当前状态未核实，不得声称设备正常。'
    if issues:
        feedback.update(constraints=issues,
                        message='上一回答包含与本轮规则或观测范围冲突的明确结论。请按constraints纠正，'
                                '保留成立条件与未核验项；只引用valid_refs，不重复工具。')
    if code == 'answer_contract_violation':
        feedback['message'] = '按constraints重写短答并补对应来源，仍只输出规定的完整JSON对象，不直接输出普通正文；不截断正文或改成无依据的绝对结论。保持原工具与一次纠正上限，不重复读取。'
    return feedback

def needs_topology(question):
    q=qa.normalize_query(question).lower()
    return documented_path_question(question) or bool(qa.entities.source_ids(q)) or qa.health_counter_context(q) or any(t in q for t in ('实验室','局端','远端','router','路由器','croc','watchdog','recv-q',
                               'max_owner_failures','preempt_delay','9965','主备','备机',
                               '拓扑','keepalived','tun16','ppp','owner-fault','vrrp','主机','recv-q',
                               '六卡','三卡','6卡','3卡'))

def new_general_topic(question):
    if general_agent_question(question) or general_ha_question(question):return True
    q = question.lower()
    basic = is_basics_query(question)
    if re.match(r'^(那|那么|它|这个|具体|还有|继续|再解释)', q):
        basic = basic and any(t in q for t in ('ssh','dns','tcp','udp','json','sql','venv','pip','git','systemd','journalctl'))
    return not needs_topology(question) and (basic or any(t in q for t in
            ('python','fastapi','gradio','javascript','java','元组','列表','字典','装饰器','生成器')))

def is_followup(question):
    q=question.strip()
    if len(q)>100 or new_general_topic(q): return False
    if answer_discussion(q): return True
    if re.search(r'那(?:台|个|条|种)|另一(?:台|个|条|种)',q): return True
    if source_question(q) or re.search(r'来源(?:是|在|呢|[？?])|原文在哪',q):return True
    if qa.entities.source_ids(q) and not re.match(r'^(那|那么|它|这个|和|跟)',q):return False
    if re.search(r'它|他们|它们|这个|该(?:服务|脚本|程序|单元)|上述|上面',q):return True
    if re.match(r'^(单凭|仅凭|只凭).*(候选|检查|链路|业务|这些|这项|这个)',q):return True
    if (re.match(r'^(候选检查|candidate(?:检查)?)(成功|通过|合格|失败)',q,re.I)
            and re.search(r'能否|能.{0,4}(说明|证明)|是否|意味着|代表',q)):
        return True
    if re.match(r'^(那|那么|它|他们|它们|这个|这种|这样|还有|具体|继续|再解释)',q): return True
    if re.fullmatch(r'(为什么|为何)(呢)?[？?。!！]*',q): return True
    if re.search(r'是干什么的|什么意思|有什么用|有啥用|谁负责|谁启动|谁停止|为什么需要|为什么要|也是这样吗|这有什么用',q):return True
    return bool(re.match(r'^(为什么|为何)',q) and
                (needs_topology(q) or any(t in q for t in
                 ('这','它','业务','恢复','切换','接管','降权','故障','健康','选举','队列','接口','通告','服务'))))

def retrieval_group(query):
    group=query_group(query.rsplit('\n追问：',1)[-1])
    return group if group!='unknown' else query_group(query) if query_group(query)!='unknown' else qa.entities.group(query)

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
    terms=[] if qa.entities.source_ids(question) or qa.entities.source_ids(anchor or '') else qa.entities.reference_terms(history)
    if not anchor:return (question+'\n所指名称：'+' '.join(terms))[:1000] if terms else question if needs_topology(question) else None
    group=query_group(question)
    if not topic_groups(question):
        for text in previous:
            if topic_groups(text):
                group=query_group(text);break
    parts=[retarget_topic(anchor,group)[:600]]
    parts += [retarget_topic(text,group)[:100] for text in reversed(previous[:2]) if text!=anchor]
    query='\n追问：'.join(parts+[question])
    if terms:query+='\n所指名称：'+' '.join(terms)
    return query[:1000]

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

def search(query,extra_records=None):
    fields=('id','title','kind','operating_modes','device_groups','symptom',
            'historical_finding','historical_outcome','verification_limits','evidence_date',
            'curation_status','live_verified','documentation_date','knowledge_coverage','reference_urls',
            'reference_id','verification_sources','module_id','module_path','provenance_notes','sources','teaching_summary') + METADATA_FIELDS
    hits=qa.retrieve(query,group=retrieval_group(query),**({'extra_records':extra_records} if extra_records else {}))
    selected=hits[:4]
    selected += [h for h in hits[4:] if h.get('reason')=='模块入门概览']
    experience=next((h for h in hits if h['case'].get('kind')=='reviewed_experience'),None)
    if experience:
        if experience not in selected:selected=selected[:3]+[experience]
        anchor=next((r for r in qa.KNOWLEDGE if r['id']==experience['case']['reference_id']),None)
        if anchor and not any(h['case']['id']==anchor['id'] for h in selected):
            selected.append({'case':anchor})
    # 排名截断不能丢掉旧故障的冲突/恢复说明。
    if any(h['case']['id'] in ('HA-001','HA-002','HA-007') for h in selected):
        conflict=next((h for h in hits if h['case']['id']=='KB-011'),None)
        if conflict and not any(h['case']['id']=='KB-011' for h in selected):
            selected.append(conflict)
    # 模式和资料性质也必须到达模型，不能只保留编号与正文。
    return [dict({k:h['case'][k] for k in fields if k in h['case']},
                 kind=h['case'].get('kind','historical_case')) for h in selected]

def observed_facts(observations, question=''):
    from execution_workflow import result_summary
    facts = [item for item in observations if item.get('ok') is True and item.get('freshness') == 'current']
    answer = '\n\n'.join(DEVICE_LABELS[item['device']] + '：' + result_summary(item, question) + ' [' + item['id'] + ']'
                         for item in facts)
    return answer, [item['id'] for item in facts]


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

def escaped_answer_layout(answer):
    """只识别正文布局错误，不解码代码字符串或路径中的反斜杠。"""
    if re.search(r'(?m)^(?:`{3,}|~{3,})[a-zA-Z0-9_+-]*\\n', answer):
        return True
    prose=re.sub(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[^\n]*(?:\n|$)', '', answer)
    prose=re.sub(r'(`+)[^`\n]*?\1', '', prose)
    return prose.count('\\n')>=3 and '\\n\\n' in prose


def available_catalog(catalog, observations, remaining_reads):
    """规划视图仅列未尝试且预算可用的项；实际权限仍由原目录校验。"""
    result=json.loads(json.dumps(catalog))
    if remaining_reads<=0: return {}
    attempted={(item['device'],item['check']) for item in observations}
    for device in list(result):
        item=result[device]
        item['checks']={check:label for check,label in item['checks'].items()
                        if (device,check) not in attempted}
        if remaining_reads<2 or not {'services','interfaces'}<=set(item['checks']):
            item.pop('overview',None)
        if not item['checks']: result.pop(device)
    return result


def intake_decision(action, options, ha_intake=False, previous=None):
    """接收追问或待审批范围。这里返回用户决策所需的信息，不创建执行权限。"""
    fields = set(action)
    followup = {}
    if ha_intake and 'continues_topic' in fields and type(action['continues_topic']) is bool:
        followup = {'continues_topic': action['continues_topic']}
        fields.remove('continues_topic')
    kind=action['action']
    previous=previous or {}
    continued=followup.get('continues_topic')
    prior_brief=previous.get('brief') if continued else None
    delta={}
    if ha_intake and 'brief' in fields:
        if not isinstance(action['brief'],dict):
            raise ProtocolFailure('invalid_intake_brief')
        delta=dict(action['brief'])
        if (kind=='clarify' and 'devices' in delta and delta['devices'] is None
                and delta.get('missing') in ('device','source','destination')):
            delta.pop('devices')
        fields.remove('brief')
    if ha_intake and 'goal' in fields:
        goal=action['goal']
        if not isinstance(goal,str) or goal not in GOALS or delta.get('goal',goal)!=goal:
            raise ProtocolFailure('invalid_intake_brief')
        delta['goal']=goal
        fields.remove('goal')
    try:
        brief=update_brief(delta,prior_brief)
    except ValueError as error:
        raise ProtocolFailure('invalid_intake_brief') from error
    goal=brief['goal']
    if kind=='investigate' and ha_intake and fields=={'action','runbook','devices'}:
        try:
            goal,scope=compile_runbook(action['runbook'],action['devices'],options)
        except ValueError as error:
            raise ProtocolFailure(str(error)) from None
        return {'status':'diagnostic_proposed','scope':scope,'goal':goal,'runbook':action['runbook'],
                'intake_brief':dict(brief,goal=goal,devices=sorted(scope),missing=None),**followup}
    if (kind=='unavailable' and ha_intake and goal
            or kind in ('clarify','prepare_diagnostic') and goal in UNAVAILABLE):
        # Exact proof has no registered tool. Rephrasing the request cannot grant access.
        valid_fields={'unavailable':{'action'},'clarify':{'action','question'},
                      'prepare_diagnostic':{'action','scope'}}
        if fields!=valid_fields[kind]:
            raise ProtocolFailure('invalid_diagnostic_scope')
        message=UNAVAILABLE.get(goal,'现有登记检查无法直接核实这项内容，本轮状态待核实。')
        return {'status':'current_unverified','answer':message,'diagnostic_goal':goal,
                'intake_brief':dict(brief,missing=None),**followup}
    if kind=='clarify' and fields=={'action','question'}:
        if 'brief' in action and brief['missing'] is None:
            raise ProtocolFailure('invalid_intake_brief')
        question=action['question']
        if isinstance(question,str):
            question=question.strip()
            if 2<=len(question)<=120 and re.fullmatch(r'[^\r\n？?]+[？?]',question):
                same_question=previous.get('last_question') and question_key(question)==question_key(previous['last_question'])
                unchanged=prior_brief and brief==prior_brief and brief['missing']
                if continued is not False and (unchanged or same_question and (not delta or brief==prior_brief)):
                    raise ProtocolFailure('intake_no_progress')
                return {'status':'clarify','answer':question,
                        **({'intake_brief':brief} if ha_intake else {}),**followup}
        raise ProtocolFailure('invalid_clarification')
    if action['action']=='prepare_diagnostic' and fields=={'action','scope'}:
        scope=action['scope']
        if ha_intake:
            try:scope=expand_scope(scope,options)
            except (ValueError,TypeError):raise ProtocolFailure('invalid_diagnostic_scope') from None
        if isinstance(scope,dict) and scope and all(
                device in options and isinstance(checks,list) and checks
                and all(isinstance(check,str) and check in options[device]['checks'] for check in checks)
                and len(checks)==len(set(checks)) for device,checks in scope.items()):
            selected={check for checks in scope.values() for check in checks}
            if goal=='icmp_reachability' and not selected.intersection(PROBE_CHECKS) or (
                    goal=='ha_configuration' and 'ha_config' not in selected):
                raise ProtocolFailure('missing_goal_tool')
            if delta.get('missing') or delta.get('devices') and not set(scope)<=set(delta['devices']):
                raise ProtocolFailure('invalid_intake_brief')
            structured={'goal':goal,'intake_brief':dict(brief,devices=sorted(scope),missing=None)} if ha_intake else {}
            return {'status':'diagnostic_proposed','scope':scope,**structured,**followup}
    if ha_intake:
        raise ProtocolFailure('invalid_diagnostic_scope')
    raise ToolRejection('invalid_intake_decision')


def run(question, history, planner, lookup, probe, audit, clock=time.monotonic,
        permitted_checks=None, read_guard=None, task_id=None, question_policy=None, progress=None,
        initial_overview=False,run_guard=None,material_sources=None,task_memory=None,sources_guard=None,
        execution_limits=None,rolling_context=None,public_request=None,public_background=None,repair_catalog=None,
        diagnostic_options=None,task_inputs=None,wait_for_task_input=None,seal_task_inputs=None,
        scope_options=None,investigation_context=None,history_recall=None,history_summary=None):
    def notify(stage, **fields):
        # 展示失败不能改变工具权限或让已取得的诊断证据丢失。
        if progress is not None:
            try: progress(stage, **fields)
            except Exception: pass
    # 旧入口保持原协议；新审批入口必须显式传入批准范围，空字典表示禁读。
    material_mode=bool(question_policy and question_policy.get('mode')=='material_analysis')
    if material_mode and permitted_checks:raise ValueError('Materials cannot grant device access')
    if material_mode:permitted_checks={}
    if diagnostic_options is not None and (permitted_checks!={} or
            (question_policy or {}).get('mode')!='knowledge' or (question_policy or {}).get('require_current_evidence')):
        raise ValueError('Intake decisions require a non-executing knowledge turn')
    if history_recall is not None and (permitted_checks!={} or diagnostic_options is not None or
            (question_policy or {}).get('mode')!='knowledge' or
            (question_policy or {}).get('require_current_evidence')):
        raise ValueError('History recall requires a non-executing explanation turn')
    recall_requested=False
    historical=False
    catalog=tool_catalog()
    if permitted_checks is not None:
        if (not isinstance(permitted_checks,dict) or
            any(d not in DEVICES or not isinstance(cs,(list,tuple)) or
                not cs or any(c not in CHECKS for c in cs)
                for d,cs in permitted_checks.items())):
            raise ValueError('Invalid approved scope')
        catalog=tool_catalog(list(permitted_checks))
        for device,item in catalog.items():
            item['checks']={c:text for c,text in item['checks'].items() if c in permitted_checks[device]}
            if not {'services','interfaces'}<=set(permitted_checks[device]): item.pop('overview',None)
    limits=legacy_limits() if execution_limits is None else validate_limits(execution_limits,permitted_checks or {})
    max_reads,max_steps=limits['max_reads'],limits['max_steps']
    staged=limits['version']=='bounded_stages_v1'
    started_stages=set()
    task=task_id or uuid.uuid4().hex
    require_current=bool(question_policy and
                         (question_policy.get('require_current_evidence') or question_policy.get('mode')=='diagnostic'))
    assess_knowledge=bool(question_policy and question_policy.get('mode')=='knowledge'
                         and question_policy.get('domain') in ('lab','topology','basics','libraries'))
    coverage=None
    sources, observations, events=[],[],[]
    task_questions=[]
    scope_extension=None
    if material_mode:sources=list(material_sources or [])
    model_trace=[]
    seen=set(); searches=reads=tokens=0; deadline=clock()+limits['max_seconds']
    tokens_complete=True
    corrections=0; feedback=None
    public_sources=[]; public_result=None
    public_allowed=bool(public_request is not None and (question_policy or {}).get('mode')=='knowledge'
                        and not material_mode and not catalog)
    if (public_background is not None and not public_allowed and (question_policy or {}).get('mode')=='knowledge'
            and not material_mode and not catalog):
        public_result=public_background
        public_sources=public_result['sources']
    audit.add(task,0,'start',{'kind':'unified_chat','request_trace':request_trace.get(),
                            'mode':(question_policy or {}).get('mode'),
                            'intent':(question_policy or {}).get('intent'),
                            'goal':(question_policy or {}).get('diagnostic_goal'),
                            'limits':limits})
    # 实验室专有问题先由宿主补证据，不赌模型是否主动检索。仍允许继续改写查询。
    retrieval_hint=(question_policy or {}).get('retrieval_query')
    if retrieval_hint is not None and (not isinstance(retrieval_hint,str) or not 1<=len(retrieval_hint)<=1000):raise ValueError('Invalid retrieval context')
    public_only=bool((public_allowed or public_background is not None) and (question_policy or {}).get('domain')=='general')
    query=None if material_mode or public_only else retrieval_hint or prefetch_query(question, history)
    reference_entities=((question_policy or {}).get('reference_entities',[])
                        if is_followup(question) and not new_general_topic(question)
                        and not qa.croc_concept_question(question) else [])
    assessment_question=assessment_query(question,retrieval_hint,basics=(question_policy or {}).get('domain')=='basics',
                                          reference_entities=reference_entities)
    if not query and question_policy and question_policy.get('domain') in ('lab','topology','basics','libraries'):
        query=question
    knowledge_scope=query_scope(query or question)
    scope_group=retrieval_group(query) if query else query_group(question)
    response_contract=response_policy(question)
    direct_probe=bool(require_current and (question_policy or {}).get('direct_probe'))
    local_read_only=bool(require_current and (question_policy or {}).get('automatic_read'))
    runbook=(question_policy or {}).get('runbook') if require_current else None
    host_planned=local_read_only or bool(runbook)
    if query and not direct_probe and not host_planned:
        notify('retrieving',step=0)
        try:
            sources=filter_sources(lookup(query), knowledge_scope)
            searches=1;seen.add(('search',query))
            event={'action':'search','count':len(sources),'ids':[s['id'] for s in sources],'trigger':'host_prefetch'}
            events.append(event);audit.add(task,0,'tool',event)
        except Exception:
            events.append({'action':'search','count':0,'ids':[],'error':'retrieval_failed'})
    reason='step_budget_reached'
    log_credentials=set()
    case_enabled=runbook in ('croc_fault','topology_health') or case_selected(question,question_policy)
    path_enabled=runbook=='croc_fault' or (not runbook and business_path_selected(question,question_policy))
    goal=(question_policy or {}).get('diagnostic_goal') if require_current else None
    required_checks=(question_policy or {}).get('required_checks',{}) if require_current else {}
    if path_enabled:
        goal='business_path'
    proof_checks = {'vip_ownership': ('interfaces',), 'ha_configuration': ('ha_config',),
                    'icmp_reachability': PROBE_CHECKS, 'management_path': PING_TARGETS}.get(goal, ())
    include_routine_logs=not path_enabled or '日志' in question
    # 单目标HA Croc判断必需服务与VIP。批准范围齐全时先按固定路线取证，省去初始模型往返。
    initial_device=next(iter(catalog)) if initial_overview and case_enabled and len(catalog)==1 else None
    if initial_device and 'overview' not in catalog[initial_device]: initial_device=None
    for step in range(max_steps):
        now=clock()
        if now>=deadline: reason='time_budget_reached'; break
        # 判断针对用户原问题，模型改写检索词不能改变所需依据。
        coverage=assess(assessment_question,sources,basics=question_policy.get('domain')=='basics',scope=knowledge_scope,
                        conceptual_context=response_contract.get('prefer_plain_explanation',False),
                        allow_unassessed_context=True) if assess_knowledge else None
        if coverage and any(n['aspect']!='mechanism' for n in coverage['requirements']):
            response_contract['prefer_plain_explanation']=False
        phase=stage_budget(limits,reads)
        available_reads=phase['remaining_reads']
        context={'remaining_seconds':max(0.001,round(deadline-now,3)),'question':question,'history':history,'sources':sources,'observations':observations,
                 'tool_events':events,'tool_catalog':available_catalog(catalog,observations,available_reads),
                 'attempted_checks':[{'device':o['device'],'check':o['check'],'ref':o['id'],
                                       'ok':o.get('ok') is True,'freshness':o.get('freshness')}
                                      for o in observations],
                 'workflow_policy':question_policy or {},
                 'knowledge_scope':describe_scope(knowledge_scope,sources),
                 'remaining_searches':0 if material_mode else 2-searches,'remaining_reads':available_reads if catalog else 0,
                 'correction_feedback':feedback,'remaining_corrections':1-corrections,
                 'evidence_scope':chat_evidence_scope(observations,historical),
                 'observation_constraints':observation_constraints(observations),
                 'mechanism_constraints':mechanism_constraints(sources,scope_group)}
        if required_checks:
            context['required_checks']=required_checks
        context['response_contract']=response_contract
        if task_inputs is not None:
            inputs=task_inputs()
            thread=[{'role':'user','content':text} for text in inputs]
            for index,(offset,prompt) in enumerate(task_questions):
                thread.insert(offset+index,{'role':'assistant','content':prompt})
            context['task_thread']=thread
            scope_refs=sorted(observation_refs(observations,True))
            if scope_options and (inputs or scope_refs):
                context['scope_options']=scope_options
                context['scope_evidence_refs']=scope_refs
        runbook_options=diagnostic_options
        if diagnostic_options is not None:
            context['diagnostic_options']=diagnostic_options
            if (question_policy or {}).get('allow_runbooks'):
                sides={side for side in ('局端','远端') if side in question}
                if sides:
                    runbook_options={device:item for device,item in diagnostic_options.items()
                                     if profile(device)['side_label'] in sides}
                context['investigation_runbooks']=runbook_menu(runbook_options)
                context['investigation_devices']=list(runbook_options)
        if repair_catalog and require_current:context['repair_catalog']=repair_catalog
        if require_current:
            from repair_control import service_assessment
            assessment=service_assessment(observations)
            if assessment:context['ha_service_assessment']=assessment
        if staged:context.update(execution_limits=dict(limits),investigation_stage=phase)
        path=assess_path(observations,permitted_checks or {}) if path_enabled else None
        if path is not None:context['business_path']=path
        goal_result=assess_goal(goal,observations,permitted_checks or {},path,passive_overview=local_read_only)
        if goal_result is not None:context['goal_assessment']=goal_result
        rule_sources=sources if coverage is None else [s for s in sources if s['id'] in coverage['supporting_refs']+coverage.get('unassessed_refs',[])]
        context['answer_constraints']=remote_rules(rule_sources,scope_group)+reference_link_rules(rule_sources,response_contract)
        if task_memory is not None:context['task_memory']=task_memory
        if investigation_context is not None:context['investigation_context']=investigation_context
        if history_recall is not None and not recall_requested:
            context['history_recall']={'action':'recall_history','tasks':history_recall['tasks'],
                                       'default_task_id':history_recall['default_task_id'],'remaining':1}
        if rolling_context is not None and (question_policy or {}).get('mode')=='knowledge':
            context['rolling_context']=rolling_context
        if coverage is not None: context['knowledge_sufficiency']=coverage
        case_report=assess_case(observations,permitted_checks or {},available_reads,include_routine_logs) if case_enabled else None
        if case_report is not None:
            context['diagnostic_case']=case_report
            context['investigation_plan']=investigation_plan(case_report,observations,permitted_checks or {},available_reads)
        try:
            if run_guard is not None:run_guard()
            if step==0 and public_allowed:
                notify('retrieving',step=0)
                selection=dict(public_request)
                selection['requests']=selection['requests'][:max(0,2-searches)]
                selection['topic_limit_reached']=public_request['topic_limit_reached'] or len(selection['requests'])<len(public_request['requests'])
                public_result=retrieve_public(selection,guard=run_guard or (lambda:None),deadline=deadline)
                public_sources=public_result['sources']
                searches+=len(public_result['events'])
                for public_event in public_result['events']:
                    events.append(public_event);audit.add(task,0,'tool',public_event)
                if public_result['topic_limit_reached'] and public_sources:public_result['status']='partial'
                if clock()>=deadline:reason='time_budget_reached';break
                context['remaining_seconds']=max(0.001,round(deadline-clock(),3))
                context['remaining_searches']=max(0,2-searches)
            if public_result is not None:
                context['public_lookup']={k:v for k,v in public_result.items() if k not in ('sources','events')}
                context['public_lookup']['policy']=PUBLIC_POLICY
                context['public_sources']=public_sources
            if sources_guard is not None:sources_guard(sources)
            attempted = {(row['device'], row['check']) for row in observations}
            required_complete = all((device, check) in attempted
                                    for device, checks in required_checks.items() for check in checks)
            pending_proof = any(check in proof_checks and (device, check) not in attempted
                                for device, checks in (permitted_checks or {}).items() for check in checks)
            # 可接续的任务要处理补充和继续定位请求，单次范围结束则直接收尾。
            needs_followup = bool(task_inputs is not None and
                                  (inputs or re.search(r'定位|排障|排查|查原因|找原因|为什么|继续查', question)))
            goal_complete = (goal_result is not None and required_complete and bool(observations)
                             and not pending_proof and not context['tool_catalog']
                             and not needs_followup and not repair_catalog)
            ping_result = None
            host_answer = False
            if runbook:
                candidate=runbook_next_check(runbook,context['tool_catalog'],observations,case_report)
                if candidate:
                    action={'action':'read','device':candidate[0],'check':candidate[1]}
                else:
                    answer,refs=runbook_summary(runbook,observations,permitted_checks,case_report,goal_result,question)
                    if not refs:
                        reason='required_state_unavailable'
                        break
                    action={'action':'answer','answer':answer,'refs':refs,'basis':'limited'}
                usage=0
            elif direct_probe or local_read_only:
                # Only the catalog for this task, after authorization and deduplication.
                candidate=next(((device,check) for device,item in context['tool_catalog'].items()
                                for check in item['checks'] if local_read_only or check in PROBE_CHECKS),None)
                if candidate:
                    action={'action':'read','device':candidate[0],'check':candidate[1]}
                else:
                    if local_read_only and goal_result is None:
                        answer, refs = observed_facts(observations, question)
                        if not refs:
                            reason='required_state_unavailable'
                            break
                        action={'action':'answer','answer':answer,'refs':refs,'basis':'limited'}
                    else:
                        action={'action':'answer','answer':goal_result['answer'],'refs':goal_result['refs'],'basis':'limited'}
                usage=0
            elif goal_complete:
                # 目标检查都已结束，直接验证并返回宿主的事实投影。
                action = {'action': 'answer', 'answer': goal_result['answer'], 'refs': goal_result['refs'],
                          'basis': 'evidence' if goal_result['status'] == 'observed' else 'limited'}
                host_answer = True
                usage = 0
            elif (require_current and not repair_catalog and not needs_followup
                  and required_complete and not context['tool_catalog']
                  and any(row['check'] in PROBE_CHECKS for row in observations)):
                ping_result = assess_icmp('icmp_reachability', observations, permitted_checks or {})
                facts, refs = observed_facts([row for row in observations if row['check'] not in PROBE_CHECKS])
                action = {'action': 'answer', 'answer': '\n\n'.join(filter(None, (ping_result['answer'], facts))),
                          'refs': ping_result['refs'] + refs, 'basis': 'limited'}
                host_answer = True
                usage = 0
            elif step==0 and initial_device:
                action,usage={'action':'read_overview','device':initial_device},0
                event={'action':'host_plan','device':initial_device,'checks':['services','interfaces']}
                events.append(event);audit.add(task,step+1,'strategy',event)
            else:
                notify('analyzing' if observations else 'thinking',step=step+1)
                if run_guard is not None:run_guard()
                action, usage=traced_plan(planner,json.loads(json.dumps(context)),model_trace,audit,task,step+1)
            if type(usage) is int and usage>=0: tokens+=usage
            else: tokens_complete=False
            if run_guard is not None:run_guard()
            if sources_guard is not None:sources_guard(sources)
            if clock()>=deadline: reason='time_budget_reached'; break
            if not isinstance(action,dict): raise ProtocolFailure('invalid_action_format')
            kind=action.get('action')
            if kind=='recall_history':
                if history_recall is None or recall_requested:
                    raise ToolRejection('history_scope_not_allowed')
                recall_requested=True
                try:
                    rows,investigation_context=history_recall['resolve'](action)
                except ValueError:
                    raise ToolRejection('history_scope_not_allowed') from None
                if any(row.get('freshness')!='historical' for row in rows):
                    raise ToolRejection('history_scope_not_allowed')
                historical=True
                observations.extend(rows)
                event={'action':'recall_history','count':len(observations)}
                events.append(event);audit.add(task,step+1,'tool',event)
                # 历史选择已校验；宿主可以直接完成明确的事实回顾。
                if history_summary is not None:
                    summary = history_summary()
                    if summary is not None:
                        audit.add(task, step+1, 'finish', {'refs': summary['refs'], 'basis': 'limited',
                                  'total_tokens': tokens, 'total_tokens_complete': tokens_complete})
                        return dict(summary, task=task, basis='limited', sources=[], events=events,
                                    source_display=response_contract['source_display'],
                                    total_tokens=tokens, total_tokens_complete=tokens_complete,
                                    model_trace=model_trace)
                continue
            if task_inputs is not None and inputs!=task_inputs():
                # 丢弃补充到达前生成的动作，保留本次模型开销和原任务预算。
                reason='step_budget_reached'
                feedback=None
                continue
            if wait_for_task_input is not None and kind=='clarify' and set(action)=={'action','question'}:
                clarification=intake_decision(action,{})['answer']
                if step+1>=max_steps:
                    reason='step_budget_reached';break
                if not wait_for_task_input(clarification,step+1,len(inputs),deadline):
                    reason='task_input_timeout';break
                task_questions.append((len(inputs),clarification))
                feedback=None
                continue
            if kind=='request_scope' and context.get('scope_options'):
                if set(action)-{'action','scope','refs','goal'}:
                    raise ProtocolFailure('invalid_diagnostic_scope')
                refs=action.get('refs',[])
                if (not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in scope_refs for ref in refs)
                        or len(refs)!=len(set(refs)) or not inputs and (not refs or not action.get('goal'))):
                    raise ProtocolFailure('scope_needs_evidence')
                requested={key:action[key] for key in ('scope','goal') if key in action}
                decision=intake_decision(dict(requested,action='prepare_diagnostic'),
                                         context['scope_options'],ha_intake=True)
                if decision['status']!='diagnostic_proposed':
                    raise ProtocolFailure('invalid_diagnostic_scope')
                if not seal_task_inputs(len(inputs)):
                    feedback=None
                    continue
                scope_extension={'scope':decision['scope'],'goal':decision['goal'],'refs':refs}
                reason='additional_scope_required'
                break
            if diagnostic_options is not None and kind in ('clarify','prepare_diagnostic','investigate','unavailable'):
                intake_policy=question_policy or {}
                if kind=='investigate' and not context.get('investigation_runbooks'):
                    raise ProtocolFailure('invalid_runbook_scope')
                options=runbook_options if kind=='investigate' else diagnostic_options
                decision=intake_decision(action,options,intake_policy.get('chat_space')=='ha',
                                         intake_policy.get('diagnostic_intake'))
                bounded=intake_policy.get('bounded_scope')
                if bounded and decision['status']=='diagnostic_proposed':
                    proposed=decision['scope']
                    if set(proposed)!=set(bounded) or any(
                            not set(checks)<=set(proposed[device])<=set(checks)|{'interfaces'}
                            for device,checks in bounded.items()):
                        raise ProtocolFailure('invalid_diagnostic_scope')
                audit.add(task,step+1,'finish',{'intake_decision':kind,'total_tokens':tokens,
                                              'total_tokens_complete':tokens_complete})
                return dict(decision,task=task,events=events,model_trace=model_trace,
                            total_tokens=tokens,total_tokens_complete=tokens_complete)
            if kind not in ('answer','search','read','read_overview'): raise ProtocolFailure('invalid_action_format')
            if material_mode and kind!='answer':raise ProtocolFailure('material_answer_required')
            if kind=='answer' and (proof_checks or required_checks) and step+1<max_steps:
                # An approved direct observation takes precedence over a premature summary.
                candidate=next(((device,check) for device,item in context['tool_catalog'].items()
                                for check in item['checks'] if check in proof_checks or check in required_checks.get(device,())),None)
                if candidate is not None:
                    device,check=candidate
                    action={'action':'read','device':device,'check':check}
                    kind='read'
            if kind=='answer':
                if not required_complete:
                    reason='required_state_unavailable'
                    break
                answer=action.get('answer'); refs=action.get('refs'); basis=action.get('basis')
                current_refs=observation_refs(observations,require_current)
                valid={s['id'] for s in sources+public_sources}|current_refs
                if (set(action) not in ({'action','answer','refs','basis'},{'action','answer','refs','basis','repair'})
                    or 'repair' in action and not repair_catalog or not isinstance(answer,str) or
                    not 1<=len(answer.strip())<=3000 or basis not in ('general','evidence','limited') or
                    not isinstance(refs,list)): raise ProtocolFailure('invalid_answer_format')
                if response_contract.get('avoid_internal_meta') and not material_mode:
                    answer=plain_language_answer(answer)
                if echoes_log_credential(answer,log_credentials):
                    raise ProtocolFailure('log_credential_echo')
                if (require_current and not host_planned and not host_answer and goal_result is None and
                        ping_result is None and
                        any(item['check'] == 'logs' and re.search(r'(?m)^LOG_MESSAGES_REDACTED=[1-9]\d*$',
                            item.get('output') or '') for item in observations)):
                    answer, refs = observed_facts(observations)
                    basis = 'limited'
                if escaped_answer_layout(answer):
                    raise ProtocolFailure('escaped_answer_layout')
                inline_refs=re.findall(r'\[((?:HA|KB|OBS|MAT|WEB)-\d+|EXP-[a-f0-9]{32})\]',answer)
                # 不让模型用KB引用、旧OBS或一句“当前正常”绕过本轮取证要求。
                state_refs={o['id'] for o in observations if o.get('check')!='logs' and o['id'] in current_refs}
                # limited表示局部依据/根因仍有限，不等于没有本轮证据。
                # 两种有据回答均须引用成功的非日志状态OBS，并继续通过全部语义守卫。
                if require_current and not (host_planned or host_answer) and (
                        basis not in ('evidence','limited') or not state_refs.intersection(r for r in refs if isinstance(r,str))):
                    raise CurrentEvidenceFailure()
                if (
                    any(not isinstance(r,str) or r not in valid for r in refs) or
                    any(r not in refs for r in inline_refs) or
                    (basis=='evidence' and not refs)): raise ProtocolFailure('invalid_citation')
                refs=list(dict.fromkeys(refs))
                if public_result is not None:
                    if public_answer_issue(answer,public_result):raise ProtocolFailure('public_lookup_unverified')
                    if public_sources and not set(refs).intersection(s['id'] for s in public_sources):
                        raise ProtocolFailure('public_citation_required')
                    if not public_sources:
                        basis='limited'
                response_problems=[] if host_planned or host_answer else response_issues(answer,refs,response_contract,context['answer_constraints'])
                if response_problems:raise MechanismFailure(response_problems,'answer_contract_violation')
                if material_mode:
                    issue=material_answer_issue(answer,refs,sources,basis)
                    if issue:raise MechanismFailure([{'id':issue,'statement':'只分析所提供MAT片段并引用，不声称现场核验、当前状态或根因已确认。'}],issue)
                if coverage is not None:
                    issue=answer_issue(coverage,basis,refs,sources)
                    if issue: raise KnowledgeFailure(issue)
                    if coverage['status']!='supported': basis='limited'
                issues=contradicted_constraints(answer,context['mechanism_constraints'])
                if case_report is not None and not (host_planned or host_answer):
                    case_issues=case_answer_issues(case_report,answer,refs)
                    if case_issues: raise MechanismFailure(case_issues,'unsupported_case_claim')
                if issues: raise MechanismFailure(issues)
                issues=contradicted_constraints(answer,context['observation_constraints'])
                if issues: raise MechanismFailure(issues,'unsupported_observation_claim')
                if require_current:
                    issues=causal_issues(answer,observations)
                    if issues: raise MechanismFailure(issues,'unsupported_diagnostic_claim')
                if historical or history_recall is not None:
                    issues=(historical_claim_issues(answer)+causal_issues(answer,observations,check_recovery=False)
                            +measurement_claim_issues(answer,observations,refs))
                    if issues:raise MechanismFailure(issues,'unsupported_historical_claim')
                scope=chat_evidence_scope(observations,historical)
                answer=('以下根据此前采集的结果，本轮未重新读取。\n\n' if scope and scope['kind']=='historical'
                        else scope_prefix(scope))+answer
                if material_mode:answer=MATERIAL_NOTICE+'\n\n'+answer
                if public_result is not None and public_result['status']!='succeeded':
                    answer=('以下出处来自上一轮公开获取，本轮未重新联网。' if public_result['status']=='historical' else
                            '公开查证仅完成部分主题。' if public_sources else
                            '公开查证未完成：该主题尚未接入公开来源。' if public_result['status']=='unsupported' else
                            '公开查证未成功，最新信息尚未核实。')+'\n\n'+answer
                if coverage is not None and coverage['gaps'] and coverage['status']!='supported' and question_policy.get('domain')!='basics':
                    answer=notice(coverage)+'\n\n'+answer
                if len(answer)>3000: raise ProtocolFailure('invalid_answer_format')
                if seal_task_inputs is not None and not seal_task_inputs(len(inputs)):
                    reason='step_budget_reached'
                    feedback=None
                    continue
                audit.add(task,step+1,'finish',{'refs':refs,'basis':basis,'total_tokens':tokens,
                                             'total_tokens_complete':tokens_complete})
                result={'status':'answered','answer':answer,'refs':refs,'basis':basis,'sources':sources+public_sources,
                        'source_display':response_contract['source_display'],
                        'evidence':observations,'events':events,'total_tokens':tokens,
                        'total_tokens_complete':tokens_complete,'task':task,'evidence_scope':scope,'model_trace':model_trace}
                if 'repair' in action:
                    from repair_control import check_recommendation
                    try:result['repair_recommendation']=check_recommendation(action['repair'],repair_catalog,observations)
                    except Exception:
                        result['repair_rejected']=True
                if public_result is not None:result['public_lookup']={k:v for k,v in public_result.items() if k not in ('sources','events')}
                if require_current:result['execution_limits']=dict(limits)
                if coverage is not None:
                    result.update(knowledge_status=coverage['status'],knowledge_sufficiency=coverage)
                if knowledge_scope['active']:
                    result['knowledge_scope']=describe_scope(knowledge_scope,sources)
                if case_report is not None:
                    result['diagnostic_case']=case_report
                    result['diagnostic_report']=build_report(case_report,observations,permitted_checks or {},max_reads-reads,'answer_returned')
                if path is not None:attach_path(result,path)
                if goal_result is not None:result['goal_assessment']=goal_result
                if ping_result is not None:result['icmp_assessment']=ping_result
                return result
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
                found=filter_sources(lookup(query),knowledge_scope)
                existing={s['id'] for s in sources}
                sources.extend(s for s in found if s['id'] not in existing)
                event={'action':'search','count':len(found),'ids':[s['id'] for s in found]}
            elif kind=='read' and set(action)=={'action','device','check'}:
                device,check=action['device'],action['check']
                if not isinstance(device,str) or device not in DEVICES:
                    raise ToolRejection('unregistered_device')
                if not isinstance(check,str) or check not in CHECKS:
                    raise ToolRejection('unsupported_check')
                if reads>=max_reads: raise ToolRejection('read_budget_reached')
                key=('read',device,check)
                if key in seen: raise ToolRejection('duplicate_read')
                checks=[check]
            elif kind=='read_overview' and set(action)=={'action','device'}:
                device=action['device']
                if not isinstance(device,str) or device not in DEVICES:
                    raise ToolRejection('unregistered_device')
                checks=['services','interfaces']
                # 整批先校验；一项越界、重复或超限时，整批都不执行。
                if reads+len(checks)>max_reads: raise ToolRejection('read_budget_reached')
                if len(checks)>available_reads: raise ToolRejection('stage_read_budget_reached')
                if any(('read',device,c) in seen for c in checks): raise ToolRejection('duplicate_read')
            elif kind in ('read','read_overview'): raise ToolRejection('invalid_read_request')
            elif kind=='search': raise ToolRejection('invalid_search_request')
            else: raise ValueError()
            feedback=None
            if kind in ('read','read_overview'):
                # 先核对整批范围；组合读取不能先执行允许项再发现另一项越界。
                if permitted_checks is not None and any(c not in permitted_checks.get(device,[]) for c in checks):
                    raise ToolRejection('check_not_approved')
                for check in checks:
                    if clock()>=deadline:
                        reason='time_budget_reached';break
                    if read_guard is not None: read_guard(device,check)
                    number=stage_budget(limits,reads)['number']
                    if staged and number not in started_stages:
                        started_stages.add(number)
                        boundary={'action':'investigation_stage','number':number,'max_stages':limits['max_stages'],
                                  'trigger':'host_read_partition','parent_reads_used':reads}
                        events.append(boundary);audit.add(task,step+1,'stage_started',boundary)
                    notify('reading',step=step+1,device=device,check=check)
                    seen.add(('read',device,check));reads+=1
                    began=datetime.datetime.utcnow()
                    probe_began=time.monotonic()
                    try: item=dict(probe(device,check))
                    except Exception: item={'ok':False,'output':'','error':'tool_unavailable'}
                    if check=='logs' and isinstance(item.get('output'),str):
                        item['output'],credentials=filter_log_output(item['output'])
                        log_credentials.update(credentials)
                    item['host_duration_ms']=round((time.monotonic()-probe_began)*1000,3)
                    finished=datetime.datetime.utcnow()
                    item=mark_observation(item,device,check,(began,finished) if require_current else None)
                    item.update(device=device,check=check,id='OBS-'+str(reads))
                    observations.append(item)
                    event={'action':'read','device':device,'check':check,'ok':item.get('ok',False)}
                    if staged:event['investigation_stage']=number
                    if case_enabled: event.update(check_purpose(check))
                    if kind=='read_overview': event['group']='overview'
                    audit.add(task,step+1,'observation',dict(item,output=audit_text(item.get('output'))))
                    events.append(event);audit.add(task,step+1,'tool',event)
                    notify('read_complete',step=step+1,device=device,check=check,observation=item)
                if reason=='time_budget_reached':break
                recent=observations[-2:]
                if (step==0 and initial_device and kind=='read_overview' and
                    any(o.get('ok') is not True or require_current and o.get('freshness')!='current' for o in recent)):
                    reason='required_state_unavailable';break
                if (not runbook and len(recent)==2 and not any(o.get('ok') is True for o in observations) and
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
            else: reason=error.code if (isinstance(error,(MechanismFailure,CurrentEvidenceFailure,KnowledgeFailure))
                                         or error.code in ('intake_no_progress','log_credential_echo')) else 'invalid_action_or_citation'
            recoverable=isinstance(error,ProtocolFailure) or error.code in ('model_invalid_json','model_invalid_response')
            # 一轮最多纠正一次，仍在原7步/180秒预算内；不重试超时、限流或越界工具。
            if recoverable and corrections==0 and step<max_steps-1 and clock()<deadline:
                corrections+=1; feedback=correction_feedback(error.code,sources+public_sources,observations,
                                                             getattr(error,'issues',None),require_current)
                if coverage is not None:
                    # JSON/排版纠正也必须沿用正式引用守卫，不能把背景命中放进白名单。
                    feedback.update(valid_refs=coverage['supporting_refs']+coverage.get('unassessed_refs',[]),knowledge_sufficiency=coverage)
                    feedback['message']+=' 只用supporting_refs支持已覆盖部分，资料不足时用limited说明具体缺项。'
                if isinstance(error,KnowledgeFailure):
                    feedback.update(valid_refs=coverage['supporting_refs']+coverage.get('unassessed_refs',[]),knowledge_sufficiency=coverage,
                        message='命中相关资料不等于直接支持所问。只用supporting_refs支持已覆盖部分；'
                                '不足时basis用limited，说明gaps，补充内容明确为一般原理。'
                                '不得猜测端口、配置或操作步骤，不得引用背景编号伪装成实验室事实。'
                                '可按剩余检索次数补充直接资料，不能请求设备读取。')
                if public_sources:
                    feedback['valid_refs']=list(dict.fromkeys(feedback['valid_refs']+[s['id'] for s in public_sources]))
                if error.code=='public_citation_required':feedback['message']='已获取公开资料，请引用支持本题的WEB编号；发布信息不能证明本实验室版本或现场状态。只纠正回答，不重复工具。'
                if error.code=='public_lookup_unverified':feedback['message']='公开查证没有成功，不得声称已联网核实或断言最新版本；说明当前未核实，可补充标明的一般知识，basis=limited。'
                if error.code=='invalid_clarification':feedback['message']='只问一句能帮助确定目标的简短问题，并以问号结束。若信息足够，可以直接回答。'
                if error.code=='invalid_diagnostic_scope':
                    feedback['message']='只用 diagnostic_options 中的设备和检查项。目标不清时先问一句。'
                if error.code=='scope_needs_evidence':
                    feedback['message']='根据本轮证据申请新增范围时，请引用 scope_evidence_refs 中的观测并说明本次检查目标。'
                if error.code=='missing_goal_tool':
                    feedback['message']='当前目标需要目录中对应的直接检查。请选择准确的源设备和检查项；不支持时返回 unavailable。'
                if error.code=='invalid_intake_brief':
                    feedback['message']='请使用允许的 goal、path、devices、missing 字段，检查范围与当前目标应一致。'
                if error.code=='intake_no_progress':
                    feedback['message']='上一轮已经问过这项信息。先处理用户这次补充，更新 brief 后准备检查；工具不支持时说明缺项。'
                if error.code=='log_credential_echo':
                    feedback['message']='回答包含日志中的敏感值。仅概述与问题有关的状态和事件，不复述敏感字段或值。'
                event={'action':'correction','reason':error.code}
                if isinstance(error,MechanismFailure):
                    event['constraints']=[issue['id'] for issue in error.issues]
                events.append(event); audit.add(task,step+1,'correction',event)
                continue
            break
        except ToolRejection as error:
            event={'action':'rejected','reason':error.code}
            events.append(event);audit.add(task,step+1,'rejected',event)
            if task_inputs is not None and error.code=='check_not_approved':
                reason='task_scope_required'
                if corrections==0 and step+1<max_steps and clock()<deadline:
                    corrections+=1
                    feedback=correction_feedback(reason,sources,observations,require_current=require_current)
                    feedback['message']='上一项超出本任务授权。只选目录内仍有必要的检查，或依据已得证据给出局部判断。需要范围外的检查时请说明具体缺项。'
                    continue
                break
            reason=('investigation_inactive' if error.code in ('investigation_inactive','approval_not_active','diagnostic_required')
                    else 'account_session_expired' if error.code=='account_session_expired'
                    else 'material_not_active' if error.code=='material_not_active'
                    else 'memory_not_active' if error.code=='memory_not_active'
                    else 'rolling_context_not_active' if error.code=='rolling_context_not_active'
                    else 'knowledge_source_inactive' if error.code=='knowledge_source_inactive'
                    else 'invalid_intake_decision' if error.code=='invalid_intake_decision'
                    else 'invalid_action_or_citation');break
        except (ValueError, TypeError, KeyError):
            reason='invalid_action_or_citation'; break
        except Exception:
            reason='planner_or_tool_unavailable'; break
    audit.add(task,max_steps+1,'stop',{'reason':reason,'total_tokens':tokens,
                          'total_tokens_complete':tokens_complete})
    messages={'model_timeout':'模型响应超时','model_output_truncated':'模型输出被截断',
        'model_circuit_open':'模型接口近期连续不可用，已临时暂停新调用；请稍后重试',
        'model_invalid_json':'模型返回格式无法解析','model_rate_limited':'模型接口限流',
        'model_connection_failed':'模型连接失败','model_http_error':'模型接口请求失败',
        'model_invalid_response':'模型响应结构不完整',
        'invalid_intake_decision':'未能生成符合范围的检查申请，请说明要检查的设备和现象',
        'intake_no_progress':'这次补充后仍未确定可执行的检查范围，当前状态待核实',
        'additional_scope_required':'补充的问题需要额外检查，新增范围须单独确认',
        'invalid_action_or_citation':'工具选择或引用未通过校验','time_budget_reached':'本次处理达到时间上限',
        'unsupported_mechanism_claim':'回答仍包含与已提供规则冲突的结论',
        'answer_contract_violation':'回答仍未满足长度、程序职责或对应来源要求',
        'log_credential_echo':'候选回答包含已隐藏的日志敏感信息',
        'unsupported_observation_claim':'回答仍超出本轮局部观测支持的范围，尚不能完成诊断',
        'unsupported_case_claim':'回答仍未保留服务与VIP的依据或超出了案例证据范围',
        'unsupported_diagnostic_claim':'回答仍把停止状态或调用栈线索当成已确认原因，诊断未完成',
        'material_not_active':'材料已删除、过期或不再可用，已停止后续分析；已在途请求的用量仍保留',
        'memory_not_active':'任务记忆已撤回、到期或来源改变，已停止后续回答；已在途请求的用量仍保留',
        'rolling_context_not_active':'自动接续已关闭、到期或原对话改变，已停止本轮回答；已在途请求的用量保留',
        'knowledge_source_inactive':'已检索的经验已撤回或来源发生变化，已停止本轮回答；请核对新资料后重新提问。已发生的用量保留。',
        'material_answer_required':'材料分析只接受有原文引用的回答，不执行搜索或设备工具',
        'material_citation_required':'回答未正确引用本次材料片段，分析未完成',
        'unsupported_material_claim':'回答超出了上传材料支持的范围，不能确认当前设备状态或根因',
        'current_evidence_required':'缺少可引用的本轮现场证据，不能确认当前设备状态',
        'unsupported_historical_claim':'此次回答超出了历史观测能支持的范围',
        'knowledge_insufficient':'现有资料不足以完整支持所问，不能给出确定的实验室结论',
        'unsupported_knowledge_citation':'所引用资料不能直接支持所问',
        'knowledge_citation_gap':'引用缺少支持部分所问内容的直接资料',
        'device_evidence_unavailable':'该设备服务与接口两项基础检查均未成功，本轮状态无法核实',
        'required_state_unavailable':'必要的服务或VIP检查未完整取得有效的本轮证据，已停止后续检查；本轮不能判断两者关系或故障根因',
        'investigation_inactive':'本次调查已取消、过期或入口改变，已停止后续模型调用、检索与设备检查',
        'account_session_expired':'个人账号会话已失效，已停止后续模型调用、检索与设备检查，请重新登录并重新申请',
        'step_budget_reached':'本次处理达到步骤上限','planner_or_tool_unavailable':'模型或工具服务暂不可用',
        'task_input_timeout':'等待补充期间，本次调查的处理时间已用完',
        'task_scope_required':'下一项检查超出本次授权范围，执行前需要单独授权'}
    scope=chat_evidence_scope(observations,historical)
    message=messages.get(reason,'本次未能完成回答')
    retry_after=None
    limit_kind=None
    if reason in ('model_rate_limited','model_circuit_open') and model_trace:
        delay=model_trace[-1].get('transport',{}).get('retry_after_seconds')
        if type(delay) in (int,float) and delay>=0:
            retry_after=max(1,int(delay)+bool(delay%1))
            message='模型接口暂时受限，已暂停新调用；约%d秒后可重新发起'%retry_after
        limit_kind=model_trace[-1].get('transport',{}).get('rate_limit_kind')
        if limit_kind=='quota':
            message='模型服务返回额度限制，需待额度恢复后继续；当前回答未完成'
            retry_after=None  # 本地冷却不是额度恢复时间，不给用户显示虚假的恢复倒计时。
        elif limit_kind=='provider_capacity':message='模型上游容量暂时受限，请稍后再试；当前回答未完成'
    if (reason=='model_connection_failed' and model_trace
            and model_trace[-1].get('transport',{}).get('failure_kind')=='dns'):
        message='连接模型服务时域名解析失败，暂未取得本轮回答；可稍后重试'
    text=message+'；已取得的依据和检查过程保留在下方。'
    if require_current and not observation_refs(observations,True):
        text+='本轮设备状态未核实；历史资料、旧快照和合成回放均不能代替现场读取。'
    elif observations and not any(item.get('ok') is True for item in observations):
        text+='本轮设备状态未核实，不能据此判断设备或业务故障。'
    text+=evidence_digest(observations)
    if observations and all(item.get('origin')=='synthetic' for item in observations):
        text='以下为合成状态回放，不代表真实设备现状。\n\n'+text
    else: text=scope_prefix(scope)+text
    if assess_knowledge:
        coverage=assess(assessment_question,sources,basics=question_policy.get('domain')=='basics',scope=knowledge_scope,
                        conceptual_context=response_contract.get('prefer_plain_explanation',False),allow_unassessed_context=True)
        if (coverage['status']!='supported' and coverage['gaps'] and question_policy.get('domain') not in ('basics','general')
                and not reason.startswith('model_')):
            text=notice(coverage)+'\n\n'+text
    result={'status':'incomplete','answer':text,
            'reason':reason,'basis':'limited','refs':sorted(observation_refs(observations,require_current)),
            'sources':sources+public_sources,'evidence':observations,'events':events,'total_tokens':tokens,
            'total_tokens_complete':tokens_complete,'task':task,'evidence_scope':scope,'model_trace':model_trace}
    if public_result is not None:result['public_lookup']={k:v for k,v in public_result.items() if k not in ('sources','events')}
    if require_current:result['execution_limits']=dict(limits)
    if retry_after is not None:result['retry_after_seconds']=retry_after
    if reason in ('model_rate_limited','model_circuit_open') and model_trace:
        result['rate_limit_kind']=model_trace[-1].get('transport',{}).get('rate_limit_kind','unknown')
    if coverage is not None:
        result.update(knowledge_status=coverage['status'],knowledge_sufficiency=coverage)
    if knowledge_scope['active']:
        result['knowledge_scope']=describe_scope(knowledge_scope,sources)
    if case_enabled:
        result['diagnostic_case']=assess_case(observations,permitted_checks or {},max_reads-reads,include_routine_logs)
        result['diagnostic_report']=build_report(result['diagnostic_case'],observations,permitted_checks or {},max_reads-reads,reason)
    path=assess_path(observations,permitted_checks or {}) if path_enabled else None
    if path is not None:attach_path(result,path)
    goal_result=assess_goal(goal,observations,permitted_checks or {},path,passive_overview=local_read_only)
    if goal_result is not None:result['goal_assessment']=goal_result
    if scope_extension is not None:result['scope_extension']=scope_extension
    return result

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
