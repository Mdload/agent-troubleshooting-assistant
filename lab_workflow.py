"""问答分流与审批编排；规划器、检索和读取均从可信调用方注入。"""
import time
import re
import threading
from datetime import datetime
from snapshot_comparison import compare_snapshots
import sqlite3
from approval_store import ANY_TOPIC,TaskError,SINGLE_APPROVAL,LOCAL_READ_USE,AUTO_READ_ACTOR,PASSIVE_CHECKS,passive_scope
from diagnostic_core import AuditStore,audit_text,request_trace
from diagnostic_logs import filter_log_output
from question_routing import classify,target_devices
from learning_intent import public_followup_question,forbids_inspection,read_method_question
from unified_chat import run,search,validate_history,prefetch_query,is_followup
from conversation_topic import diagnostic_clarification,join_diagnostic_question,validate_conversation_id,resolve_topic
from context_history import MAX_TOTAL
import unified_chat
from model_availability import ModelAvailability
from interaction_mode import allows_scope,SCENES
from device_tools import DEVICE_LABELS,tool_catalog
from device_registry import PING_TARGETS,profile,TUNNEL_CHECKS,tunnel_endpoints
from knowledge_catalog import query_scope
from diagnostic_intake import (inspection_profiles,bounded_request,bidirectional_ping,LINK_GOALS,REPLACE,
    NO_PROBE,LINK_STATE,update_brief,question_key,route_followup,ROUTE_CHECK,UNBOUND_REQUEST,CHECK_METHOD,
    RESUME_PROBE,resume_probe,connectivity_measurement)
from diagnostic_goal import recent_vip_holders
from ha_croc_case import selected as case_selected, prepare_scope, needs_extended_checks
from ha_runbooks import RUNBOOKS, compile_runbook, local_intent
from link_inspection import wants_ppp_connections
from investigation_learning import historical_answer, learned_plan
from topology_observations import answer as topology_answer,related_fact
from diagnostic_continuity import task_context, continuation_plan, historical_result, recall as recall_diagnostic
from investigation_context import (EARLIEST, InvestigationChanged, assert_active as assert_investigation_active,
    build as build_investigation, observations as investigation_observations, recall_history, typed_recap, with_evidence)


def isolate_general_history(history,domain):
    """转到通用学习后不继续携带更早的现场话题，保留之后的通用追问。"""
    if domain not in ('general','basics'): return history
    start=0
    for index,message in enumerate(history):
        content=message['content'].strip()
        previous=classify(content[:1000]) if message['role']=='user' and content else {}
        if previous.get('domain') in ('lab','topology','libraries') or previous.get('intent')=='live':
            start=index+1
            while start<len(history) and history[start]['role']=='assistant':start+=1
    return history[start:]


def investigation_changed_result():
    return {'status':'incomplete','route':'diagnostic','reason':'investigation_context_changed',
            'answer':'检查结果刚有更新，请重新问一次，以免引用已被替换的记录。','evidence':[],'refs':[]}


def proposal_options(question,target_hint,scene='ha_topology'):
    """统一聊天仅提供登记设备的申请选项；真实工具目录仍为空。"""
    requested=query_scope(question)
    if (forbids_inspection(question) or read_method_question(question) or unified_chat.new_general_topic(question)
            or requested['device_types'] or requested['deployment_environments']
            or re.search(r'router|路由器',question,re.I)):
        return {}
    allowed=SCENES[scene]['devices']
    requested_devices=target_devices(target_hint or question) or allowed
    devices=[device for device in requested_devices if device in allowed]
    sides=set(re.findall(r'(?:只|仅)(?:想|要)?(?:检查|查|查看|看|读取|核对|排查)(?:一下)?\s*(局端|远端)',question))
    if sides:
        devices=[device for device in devices if profile(device)['side_label'] in sides]
    no_probe=bool(NO_PROBE.search(question))
    options={}
    for device,item in tool_catalog(devices).items():
        checks={check:label for check,label in item['checks'].items() if not no_probe or check in PASSIVE_CHECKS}
        options[device]={'label':item['label'],'business_role':profile(device)['business_role'],
                         'checks':checks,'profiles':inspection_profiles(checks)}
    return options


class WorkflowService:
    def __init__(self,store,planner=None,probe=None,lookup=search,model_factory=None,prefetch_overview=False,
                 auto_read_only=False, concurrency=4):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('Invalid workflow concurrency')
        self.store=store;self.planner=planner;self.probe=probe;self.lookup=lookup
        self.model_factory=model_factory
        self.model_availability=ModelAvailability()
        self.prefetch_overview=prefetch_overview
        self.auto_read_only=auto_read_only
        self.experiences=None
        self.repairs=None
        self.audit = AuditStore(store.path)
        # 新工作流可并发处理独立请求，旧诊断入口仍使用自己的单槽。
        self.slot = threading.BoundedSemaphore(concurrency)
        self.background_slots=threading.BoundedSemaphore(4)
        self.input_changed=threading.Condition()

    def get_planner(self):
        # 每轮再加载配置；申请和审批阶段不初始化模型或读取模型凭据。
        if self.model_factory is not None: return self.model_factory()
        if self.planner is not None: return self.planner
        return unified_chat.make_model(unified_chat.qa.model_config(),availability=self.model_availability)

    def get_probe(self):
        return self.probe if self.probe is not None else unified_chat.remote_probe

    def get_lookup(self):
        # 夹具/自定义lookup保持原固定输入；正式检索合入已审核发布资料。
        if self.experiences is None or self.lookup is not search:return self.lookup
        def lookup(question):
            # 可选经验库损坏/不可用不应阻断原资料问答或设备审批。
            try:
                store=self.experiences() if callable(self.experiences) else self.experiences
                records=store.records(question)
            except (OSError,ValueError,sqlite3.Error):records=[]
            return search(question,extra_records=records)
        return lookup

    def guard_sources(self,sources):
        if not any(s.get('kind')=='reviewed_experience' for s in sources):return
        if self.experiences is None or self.lookup is not search:return
        try:
            store=self.experiences() if callable(self.experiences) else self.experiences
            store.assert_active_sources(sources)
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError):
            raise unified_chat.ToolRejection('knowledge_source_inactive') from None

    def request_diagnostic(self,owner,question,history,scope,conversation,revision,approval_flow,goal=None,brief=None,
                           expected_topic=ANY_TOPIC,replace_active=False,required_checks=None,runbook=None):
        task=self.store.create(owner,question,history,scope,conversation,revision,approval_flow,goal,brief,
                               expected_topic=expected_topic,replace_active=replace_active,required_checks=required_checks,
                               runbook=runbook)
        self.trace_intake('task_created',task=task['id'],goal=goal,scope=scope)
        message=('是否同意进行本次只读排障？下方列出了设备、检查和数据用途，批准后自动开始；当前尚未读取设备。'
                 if approval_flow==SINGLE_APPROVAL else '此请求使用旧确认流程。请在统一聊天中重新提问并确认本次检查；当前尚未读取设备。')
        if goal:
            message='我理解你想核对'+unified_chat.DIAGNOSTIC_GOALS[goal]+'。'+message
        if goal=='icmp_reachability':
            directions=[DEVICE_LABELS[source]+' → '+('对端 tun16 '+tunnel_endpoints(source,check)[1] if check in TUNNEL_CHECKS
                        else DEVICE_LABELS[PING_TARGETS[check]]+'管理地址')
                        for source,checks in scope.items() for check in checks if check in PING_TARGETS or check in TUNNEL_CHECKS]
            if directions:
                message='、'.join(directions)+'。确认后先查到目标的路由，再由系统选源地址和出口。有可用路由时每个方向发 3 个 ping 包。'
        elif goal=='connectivity_overview':
            message+='先核对登记设备的接口、VIP 和隧道线索。管理地址互通和业务收发需要各自的证据。'
        if runbook:
            message='按下面的范围检查'+RUNBOOKS[runbook][0]+'。确认后开始。'
        return {'status':'approval_required','route':'diagnostic','answer':message,'task':task}

    def bounded_intake(self,owner,conversation,revision,question,history,topic,plan):
        previous=plan.get('anchor') or (topic or {}).get('question')
        anchor=join_diagnostic_question(previous,question) if previous and plan['continues'] else question
        brief=plan['brief']
        if plan['scope']:
            result=self.request_diagnostic(owner,anchor,history,plan['scope'],conversation,revision,SINGLE_APPROVAL,
                brief['goal'],brief,expected_topic=topic,replace_active=bool(REPLACE.match(question)),
                required_checks=None if plan.get('runbook') else plan['scope'],runbook=plan.get('runbook'))
            focus='、'.join(DEVICE_LABELS[device] for device in brief['devices'])
            self.store.remember_topic(owner,conversation,anchor,'topology','live',focus,revision,expected=topic,
                                      intake={'brief':brief,'phase':'scope_proposed','task_id':result['task']['id']})
            return result
        last=(topic or {}).get('intake') or {}
        prompt=plan.get('question')
        repeated=prompt and question_key(prompt)==question_key(last.get('last_question') or '')
        if prompt and not repeated:
            answer='先核对设备管理地址。'+prompt
            status,phase,intent='clarify','awaiting_input','clarifying'
        else:
            answer=plan.get('unavailable') or '已经确认要查设备管理地址。还缺具体源和目标，例如“局端主机到远端备机”。目前没有读取设备。'
            status,phase,intent='current_unverified','unavailable','live'
        intake={'brief':brief,'phase':phase,'last_question':prompt}
        if plan.get('retry_task'):
            intake.update(phase='awaiting_probe_retry',task_id=plan['retry_task'])
            answer+=' 可说“继续”按刚才的方向重新申请探测。'
        self.store.remember_topic(owner,conversation,anchor,'topology',intent,revision=revision,expected=topic,intake=intake)
        return {'status':status,'route':status,'answer':answer,'diagnostic_goal':brief['goal']}

    def diagnostic_handoff(self):
        return {'status':'current_unverified','route':'current_unverified','diagnostic_suggested':True,
                'answer':'这问的是当前现场情况。可以带着问题进入双机热备，确认检查范围后再核实。'}

    def trace_intake(self, stage, **fields):
        trace_id=request_trace.get()
        if trace_id:
            self.audit.record(trace_id,0,stage,fields)

    def start_passive_request(self, owner, result, access_guard, targets, start_message=None):
        if result['status']!='approval_required':
            return result
        task=result['task']
        reason=None
        if not self.auto_read_only:
            reason='automatic_read_disabled'
        elif not passive_scope(task['scope']):
            reason='active_probe_requires_confirmation'
        elif not targets or access_guard is None:
            reason='no_host_authorized_scope'
        elif any(not set(checks)<=set(targets.get(device,())) for device,checks in task['scope'].items()):
            reason='outside_host_authorized_scope'
        self.trace_intake('approval_policy',task=task['id'],scope=task['scope'],eligible_scope=targets,
                          decision='confirmation_required' if reason else 'automatic_local_read',reason=reason)
        if reason:
            return result
        access_guard()
        try:
            task=self.approve_in_background(owner,task['id'],LOCAL_READ_USE,access_guard,automatic=True)
        except TaskError as error:
            if str(error)!='busy':raise
            self.store.cancel(task['id'],owner)
            return dict(result,status='busy',task=None,answer='只读检查暂时排不上执行位置，请稍后重试。')
        return dict(result,status='investigation_started',task=task,
                    answer=start_message or (
                        '正在检查局端和远端的接口、VIP 和隧道状态。'
                        if task.get('diagnostic_goal')=='connectivity_overview' else
                        '正在按下方范围检查设备，结果会显示在这条消息中。'))

    def explain_investigation(self, owner, conversation, interaction, question, context, access_guard):
        selected = None
        observations = []
        projected = False

        def guard():
            if access_guard is not None:access_guard()
            assert_investigation_active(self.store,owner,conversation,interaction,context,question)

        def resolve(action):
            nonlocal selected, observations
            records=recall_history(context,action)
            guard()
            selected=records
            observations=investigation_observations(records)
            return observations,with_evidence(context['model'],observations)

        def local_answer(records):
            focus=context['model']['focus']
            if not focus or not records:
                return {'status':'answered','route':'diagnostic','evidence':[],'refs':[],
                        'answer':'没有找到本次问题可引用的历史检查结果，本轮未重新读取设备。'}
            return historical_result({'id':focus['task_id'],'status':focus['state'],
                                      'records':records},question,records_selected=True)

        def summarize_recall():
            nonlocal projected
            # 单纯回顾事实时直接渲染已选记录；解释性追问仍交给模型。
            if not observations:
                summary = local_answer(selected)
            elif re.search(r'分别查到了?什么|分别得到了?什么结果', question):
                local = investigation_observations(selected, LOCAL_READ_USE, len(observations))
                evidence = observations + local
                recap = typed_recap(evidence, local)
                if recap is None:
                    return None
                summary = {'status': 'answered', 'route': 'diagnostic', 'answer': recap,
                           'refs': [row['id'] for row in evidence if row['ok']], 'evidence': evidence,
                           'evidence_scope': unified_chat.chat_evidence_scope(evidence, True)}
            else:
                return None
            projected = summary is not None
            return summary

        try:
            guard()
            if not self.slot.acquire(False):
                return {'status':'busy','answer':'已有请求正在处理，请稍后再试。','events':[]}
            try:
                default_task=(context['records'][0]['task_id']
                              if context['exact_earliest'] and context['records'] else 'latest')
                result=run(question,context['history'],self.get_planner(),self.get_lookup(),self.get_probe(),self.audit,
                    permitted_checks={},question_policy={'mode':'knowledge','domain':'investigation','intent':'explain',
                        'chat_space':'ha','historical_explanation':True},
                    run_guard=guard,sources_guard=self.guard_sources,investigation_context=context['model'],
                    history_recall={'tasks':context['directory'],'default_task_id':default_task,'resolve':resolve},
                    history_summary=summarize_recall)
                guard()
                if projected:
                    return result
                if selected is None:
                    result['route']='general'
                    return result
                result['route']='diagnostic'
                # Allocate local refs only after the model result is validated.
                # The model never receives either these summaries or their IDs.
                local=investigation_observations(selected,LOCAL_READ_USE,len(observations))
                if local:
                    lines=[]
                    for row in local:
                        label=DEVICE_LABELS[row['device']]
                        line=label+' '+row['output']
                        if row['ok']:
                            line+='（'+row['collected_at'][:19].replace('T',' ')+' UTC） ['+row['id']+']'
                            result['refs'].append(row['id'])
                        lines.append(line)
                    result['answer']+='\n\n本地历史检查\n'+'\n'.join(lines)
                    result['evidence'].extend(local)
                    result['evidence_scope']=unified_chat.chat_evidence_scope(result['evidence'],True)
                if local and result['status']=='answered':
                    # 模型未见本地记录，混合回顾仍由完整的本地投影确定。
                    recap=typed_recap(result['evidence'],local)
                    if recap is not None:
                        result['answer']=recap
                        result['refs']=[row['id'] for row in result['evidence'] if row['ok']]
                return result
            finally:self.slot.release()
        except InvestigationChanged:
            return investigation_changed_result()

    def ask(self,owner,question,history,consent,conversation_id='default',interaction_mode='qa',interaction_revision=None,
            material_context=None,access_guard=None,task_memory=None,approval_flow=None,rolling_context=None,public_background=None,
            chat_space=None,learn_from_history=False):
        if approval_flow not in (None,SINGLE_APPROVAL):raise ValueError('Invalid approval flow')
        if chat_space not in (None,'general','ha'):raise ValueError('Invalid chat space')
        if consent is not True and approval_flow!=SINGLE_APPROVAL:
            return {'status':'consent_required','answer':'请先确认本次会话的数据使用范围。'}
        history=validate_history(history)
        conversation_id=validate_conversation_id(conversation_id)
        if not isinstance(question,str) or not 1<=len(question.strip())<=1000: raise ValueError('Invalid question')
        if not isinstance(interaction_mode,str) or interaction_mode not in ('qa','diagnostic','assistant'): raise ValueError('Invalid mode')
        if interaction_revision is not None and (type(interaction_revision) is not int or interaction_revision<0):
            raise ValueError('Invalid interaction revision')
        interaction=self.store.interaction(owner,conversation_id)
        if (interaction_mode != interaction['mode'] or
                interaction_revision is not None and interaction_revision != interaction['revision'] or
                interaction_mode in ('diagnostic','assistant') and interaction_revision is None):
            raise TaskError('mode_changed')
        if chat_space is not None and (chat_space=='ha')!=(interaction_mode=='diagnostic'):
            raise TaskError('mode_changed')
        if material_context is not None:
            if consent is not True:return {'status':'consent_required','answer':'请先确认所选材料的数据使用范围。'}
            if not self.slot.acquire(False):return {'status':'busy','answer':'已有请求正在处理，请稍后再试。','events':[]}
            try:
                self.store.forget_topic(owner,conversation_id)
                result=run(question,[],self.get_planner(),lambda q:[],self.get_probe(),self.audit,
                    permitted_checks={},question_policy={'mode':'material_analysis','domain':'material',
                    'material_selection':{k:v for k,v in material_context.items() if k!='sources'}},
                    run_guard=access_guard,material_sources=material_context['sources'])
                from material_analysis import NOTICE
                result.update(route='material_analysis',device_executed=False,current_state_verified=False,
                              material_selection={k:v for k,v in material_context.items() if k!='sources'})
                if result['status']!='answered':result['answer']=NOTICE+'\n\n'+result['answer']
                return result
            finally:self.slot.release()
        raw_question=question
        ha_chat=chat_space=='ha'
        investigation=None
        stored_topic=self.store.topic(owner,conversation_id)
        if stored_topic:
            intake=stored_topic.get('intake') or {}
            self.trace_intake('prior_topic',domain=stored_topic['domain'],intent=stored_topic['intent'],
                              phase=intake.get('phase'),brief=intake.get('brief'),task=intake.get('task_id'))
        if interaction_mode=='diagnostic':
            # Browser history may contain tool-derived assistant text with local-only data use.
            clarification=((stored_topic or {}).get('intake') or {})
            last_assistant=next((index for index in range(len(history)-1,-1,-1)
                                 if history[index]['role']=='assistant'),None)
            # Keep only a retransmitted clarification that matches the host's
            # unanswered question. Omitted browser history stays omitted.
            history=[message for index,message in enumerate(history)
                     if message['role']=='user' or (
                        index==last_assistant and clarification.get('phase')=='awaiting_input' and
                        message['content']==clarification.get('last_question'))]
        if ha_chat:
            if consent is True and approval_flow==SINGLE_APPROVAL:
                if access_guard is not None:access_guard()
                investigation=build_investigation(self.store,owner,conversation_id,interaction,raw_question)
            context=(task_context(self.store,owner,conversation_id,interaction)
                     if consent is True and approval_flow==SINGLE_APPROVAL else None)
            resumed=continuation_plan(context,raw_question,stored_topic)
            if resumed:
                if access_guard is not None:access_guard()
                self.trace_intake('task_context_restored',source_task=context['id'],scope=resumed['scope'],approval_restored=False)
                result=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,stored_topic,resumed)
                with self.store.connect() as db:
                    self.store.event(db,result['task']['id'],'context_continuation',{'parent':context['id'],'approval_restored':False})
                return self.start_passive_request(owner,result,access_guard,resumed['scope'])
            historical_turn=investigation and investigation['recap']
            topology_args=(self.store,owner,conversation_id,interaction,stored_topic,raw_question,learn_from_history)
            topology_recall=topology_answer(*topology_args) if consent is True and approval_flow==SINGLE_APPROVAL else None
            recalled=topology_recall
            if recalled is None:
                recalled=recall_diagnostic(self.store,owner,conversation_id,interaction,stored_topic,raw_question,context)
            if recalled is None and consent is True and approval_flow==SINGLE_APPROVAL:
                recalled=historical_answer(self.store,owner,conversation_id,interaction,stored_topic,raw_question)
                if recalled:
                    source=recalled['historical_investigation_id']
                    for entry in recalled['evidence']:
                        entry.update(task_id=source,source_ref=entry['id'],conversation_id=conversation_id)
            if recalled and historical_turn:
                eligible={(row['task_id'],row['device'],row['check']) for row in investigation['records']
                          if row['source_ref']}
                recalled_keys={(row.get('task_id'),row['device'],row['check']) for row in recalled.get('evidence',[])}
                if EARLIEST.search(raw_question) or 'evidence' in recalled and recalled_keys!=eligible:
                    recalled=None
            if recalled:
                if access_guard is not None:access_guard()
                try:
                    assert_investigation_active(self.store,owner,conversation_id,interaction,
                                                investigation if historical_turn else None,raw_question)
                except InvestigationChanged:
                    return investigation_changed_result()
                # Personal topology history can span same-owner chats and
                # arrive outside the single-conversation investigation view.
                # Recheck its actual selected answer before returning it.
                if recalled is topology_recall and topology_answer(*topology_args)!=recalled:
                    return investigation_changed_result()
                if recalled.get('diagnostic_context'):
                    self.trace_intake('task_context_recalled',source_task=context['id'],approval_restored=False)
                return recalled
            if historical_turn and investigation['explicit']:
                result=self.explain_investigation(owner,conversation_id,interaction,raw_question,investigation,access_guard)
                if result is not None:return result
        bounded=None
        if consent is True and approval_flow==SINGLE_APPROVAL and chat_space is not None:
            holders={}
            intake=(stored_topic or {}).get('intake') or {}
            if ha_chat and intake.get('phase')=='awaiting_probe_retry' and question_key(raw_question) in ('继续','继续测','继续探测','重新测','重测','再测一次','接着测'):
                last=self.store.last_probe(owner,conversation_id,interaction['revision'])
                if last and last['id']==intake.get('task_id') and last['scope']:
                    bounded={'brief':intake['brief'],'scope':last['scope'],'continues':True}
            if ha_chat and RESUME_PROBE.search(raw_question):
                bounded=resume_probe(raw_question,self.store.last_probe(owner,conversation_id,interaction['revision']))
            if not bounded and ha_chat and (ROUTE_CHECK.search(raw_question) or UNBOUND_REQUEST.search(raw_question) or CHECK_METHOD.search(raw_question)):
                last=self.store.last_probe(owner,conversation_id,interaction['revision'])
                context=stored_topic
                if last and (not intake.get('brief',{}).get('devices') and
                             (not stored_topic or stored_topic['domain'] in ('topology','lab'))):
                    context={'intake':{'brief':update_brief({'goal':'icmp_reachability','devices':last['devices'],'path':'management'})}}
                bounded=route_followup(raw_question,context)
                if (bounded and bounded.get('method_correction') and last and last['scope']
                        and last['id']==intake.get('task_id') and not target_devices(raw_question)
                        and not re.search(r'\d+\.\d+|业务|隧道|换|改测|改成',raw_question)):
                    bounded['retry_task']=last['id']
                if not bounded and last and CHECK_METHOD.search(raw_question):
                    if access_guard is not None:access_guard()
                    bound=all('ICMP_MODE=kernel_route' not in item['output'] and
                              'ICMP_ROUTE_UNAVAILABLE=1' not in item['output'] for item in last['evidence'])
                    mode=('那次 ping 固定了源管理地址（-I），可能与你手工 ping 的选路不同。' if bound else
                          '那次 ping 先查询了目标路由，由设备系统选择源地址和出口。')
                    return {'status':'answered','route':'diagnostic','answer':
                        '助手从 140 跳板 SSH 到设备。远端直达，局端再经过局端管理入口。SSH 走的管理链路和设备之间的探测是两条路径。'+mode+
                        '如果没收到应答，仅凭这次统计还不能定位原因。'}
            if (ha_chat and intake.get('phase')=='scope_proposed' and intake['brief']['goal'] in LINK_GOALS
                    and (bidirectional_ping(raw_question) or connectivity_measurement(raw_question))):
                scope,evidence=self.store.topic_observations(owner,conversation_id,interaction['revision'],stored_topic)
                holders=recent_vip_holders(evidence,scope,datetime.utcfromtimestamp(self.store.clock()))
            if not bounded:
                bounded=bounded_request(raw_question,stored_topic if ha_chat else None,holders)
        if bounded:
            self.trace_intake('bounded_plan',brief=bounded['brief'],scope=bounded['scope'],
                              disposition='scope_proposed' if bounded['scope'] else 'needs_input')
            if access_guard is not None:access_guard()
            if chat_space=='general':return self.diagnostic_handoff()
            if not bounded.get('prefer_model') or self.auto_read_only and passive_scope(bounded['scope']):
                result=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,stored_topic,bounded)
                return self.start_passive_request(owner,result,access_guard,bounded['scope'])
        if ha_chat and (self.auto_read_only or wants_ppp_connections(raw_question)) and approval_flow==SINGLE_APPROVAL and consent is True:
            prior=((stored_topic or {}).get('intake') or {}).get('brief') or {}
            selection=local_intent(raw_question,target_devices(raw_question),prior)
            if selection and (self.auto_read_only or selection[0]=='interfaces'):
                name,devices,continued=selection
                try:goal,scope=compile_runbook(name,devices,proposal_options(raw_question,'',interaction['scene']))
                except ValueError:pass
                else:
                    path={'croc_fault':'business','routing':'business'}.get(name)
                    plan={'brief':update_brief({'goal':goal,'path':path,'devices':devices}),
                          'scope':scope,'continues':continued,'runbook':name}
                    if access_guard is not None:access_guard()
                    result=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,stored_topic,plan)
                    labels='、'.join(DEVICE_LABELS[device] for device in devices)
                    return self.start_passive_request(owner,result,access_guard,scope,
                                                      '正在检查'+labels+'的'+RUNBOOKS[name][0]+'。')
            if self.auto_read_only and learn_from_history and access_guard is not None:
                options=proposal_options(raw_question,'',interaction['scene'])
                learned=learned_plan(self.store,owner,conversation_id,interaction['scene'],raw_question,options)
                if learned:
                    access_guard()
                    scope=learned['scope']
                    brief=update_brief({'goal':learned['goal'],'devices':sorted(scope)})
                    result=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,
                        stored_topic,{'scope':scope,'brief':brief,'continues':False})
                    return self.start_passive_request(owner,result,access_guard,scope,
                        '已参考你之前同类调查取得证据的检查方案，正在重新读取本次设备状态。')
        if ha_chat and historical_turn and not bounded and not investigation['explicit']:
            result=self.explain_investigation(owner,conversation_id,interaction,raw_question,investigation,access_guard)
            if result is not None:return result
        topic=stored_topic
        # 切换入口后第一问不从浏览器的旧历史恢复上一轮调查意图。
        if not topic and interaction['revision']>0 and interaction_mode!='assistant' and not ha_chat: history=[]
        if not topic and history:
            # 旧会话升级后没有服务端话题；只用最近用户问题恢复，不采纳旧模型的状态断言。
            previous=next((m['content'] for m in reversed(history) if m['role']=='user'),'')
            if 1<=len(previous.strip())<=1000:
                previous_route=classify(previous)
                topic={'question':previous,'domain':previous_route['domain'],'intent':previous_route['intent'],'target_hint':''}
        continuation=resolve_topic(question,topic)
        if (topic and topic['domain']=='basics' and topic['intent']=='explain'
                and public_followup_question(question,topic['question'])):
            continuation={'anchor':topic['question'],'target_hint':'','needs_question':False,
                'question':(topic['question'][:600]+'\n追问：'+question)[:1000],'inherited_live':False}
        anchor=question;target_hint=''
        if continuation:
            anchor=continuation['anchor'];target_hint=continuation['target_hint']
            if continuation['needs_question']:
                route={'route':'clarify','domain':'topology','intent':'unknown',
                    'answer':continuation.get('clarify_answer') or '已收到设备补充。请说明想了解它的原理、确认当前状态，还是遇到了什么现象；当前尚未读取设备。'}
            else:
                question=continuation['question']
                route=classify(question,history,target_hint,continuation['inherited_live'])
        else:
            # 机制后的省略对象追问可以接最近用户话题，不能接旧模型结论。
            # 只继承解释话题；新现状问法仍由classify创建新的审批申请。
            if (topic and topic['domain'] in ('lab','topology','libraries') and topic['intent']=='explain'
                    and is_followup(question)):
                combined=prefetch_query(question,[{'role':'user','content':topic['question']}])
                if combined:
                    question=combined;anchor=topic['question']
            route_history=history+([{'role':'user','content':topic['question']}] if topic else [])
            route=classify(raw_question,route_history)
            if topic and topic['domain'] in ('lab','topology','libraries') and is_followup(raw_question) and route['intent']!='live':
                anchor=topic['question']
                question=prefetch_query(raw_question,route_history) or raw_question
            if (topic and topic['domain'] in ('general','basics') and is_followup(question)
                    and not unified_chat.needs_topology(question)
                    and route['domain'] in ('general','basics','unknown') and route['intent']!='live'):
                # 通用追问不必每轮重复Python/SSH等关键词，也不能借此带回设备权限。
                if topic['domain']=='basics':
                    question=(topic['question'][:600]+'\n追问：'+question)[:1000]
                    anchor=topic['question'];route=classify(question,[])
                else:route={'route':'general','domain':'general','intent':'explain'}
        if consent is not True and route['route'] in ('general','lab_knowledge'):
            return {'status':'consent_required','answer':'请先确认本次问题与近期对话的数据使用范围。'}
        turn_topic={'question':anchor,'domain':route['domain'],'intent':route['intent'],'target_hint':target_hint}
        kind=route['route']
        semantic_intake=(approval_flow==SINGLE_APPROVAL and consent is True and
                         (chat_space is not None or interaction_mode=='assistant' and
                          not (continuation and continuation['needs_question'])))
        stateful_intake=ha_chat and semantic_intake
        if not stateful_intake:
            self.store.remember_topic(owner,conversation_id,revision=interaction['revision'],**turn_topic)
        expected_topic=stored_topic if stateful_intake else turn_topic
        general_topic=unified_chat.new_general_topic(raw_question)
        if not ha_chat or general_topic:
            history=isolate_general_history(history,route['domain'])
        if chat_space=='general' and route.get('intent')=='live':
            return dict(route,status=kind) if kind in ('unsupported_device','current_unverified') else self.diagnostic_handoff()
        if interaction_mode=='qa' and route.get('intent')=='live':
            return {'status':'current_unverified','route':'current_unverified',
                    'interaction_mode':'qa','diagnostic_suggested':kind!='current_unverified',
                    'answer':'当前是问答模式，尚未读取设备，当前状态未核实。可以继续询问原理和排查方法，或主动进入“只读排障”申请检查设备。'}
        if interaction_mode in ('diagnostic','assistant') and route.get('intent')=='live':
            requested=query_scope(question)
            if requested['device_types'] or requested['deployment_environments']:
                return {'status':'unsupported_device','route':'unsupported_device',
                        'answer':'当前尚未登记三卡/六卡或明确物理机/云环境的设备映射，不能借用已有主备设备代查。请先接入并核实目标设备；当前尚未读取设备。'}
        if kind in ('out_of_scope','unsupported_device','current_unverified') or (kind=='clarify' and not semantic_intake):
            return dict(route,status=kind)
        if kind=='diagnostic' and (chat_space is None or not semantic_intake):
            effective_scene='ha_topology' if interaction_mode=='assistant' else interaction['scene']
            if not allows_scope(effective_scene,route['scope']):
                return {'status':'scene_scope_mismatch','route':'clarify',
                        'answer':'这项设备不在所选场景的检查范围。请选择相应场景后重新说明设备与现象；当前尚未读取设备。'}
            if (case_selected(question,{'mode':'diagnostic','scene':effective_scene})
                    and needs_extended_checks(question)):
                route['scope']=prepare_scope(route['scope'])
            return self.request_diagnostic(owner,question,history,route['scope'],conversation_id,interaction['revision'],approval_flow)
        if consent is not True:
            return {'status':'consent_required','answer':'请先确认本次问题与近期对话的数据使用范围。'}
        # 知识问答始终禁读；模型返回read也无法越过空批准范围。
        if not self.slot.acquire(False):
            if kind=='clarify':return dict(route,status=kind)
            return {'status':'busy','answer':'已有请求正在处理，请稍后再试。','events':[]}
        try:
            try:
                retrieval_hint=question if question!=raw_question else prefetch_query(raw_question,history+([{'role':'user','content':topic['question']}] if topic else []))
                policy=dict(route,mode='knowledge')
                policy.pop('answer',None)
                # 只按当前用户原话触发；历史、省略对象和模型改写不能授权公开外发。
                from public_lookup import plan as public_plan
                public_request=public_plan(raw_question) if consent is True else None
                if public_background is not None:
                    public_request=None;retrieval_hint=None;policy=dict(mode='knowledge',domain='general',intent='explain')
                if chat_space is not None:policy['chat_space']=chat_space
                if retrieval_hint:
                    policy['retrieval_query']=retrieval_hint[:1000]
                    policy['reference_entities']=[item['name'] for item in unified_chat.qa.entities.matches(anchor)]
                options=(proposal_options(raw_question if stateful_intake else question,target_hint,
                                          interaction['scene'] or 'ha_topology')
                         if semantic_intake and not (public_request or public_background)
                         and not read_method_question(raw_question) else None)
                if stateful_intake and options and not bounded and not re.search(r'只|仅|别|不要|顺便|同时',raw_question):
                    policy['allow_runbooks']=True
                if bounded and options is not None:policy['bounded_scope']=bounded['scope']
                if ha_chat and options is not None and topic and not general_topic:
                    policy['recent_topic']={key:value for key,value in topic.items() if key!='intake'}
                    if 'intake' in topic:policy['diagnostic_intake']=topic['intake']
                if options is not None and topic and topic['intent']=='clarifying' and not general_topic:
                    policy['clarification_context']=topic['question']
                background=investigation['model'] if investigation and not (general_topic or public_request or public_background) else None

                def plan_guard():
                    if access_guard is not None:access_guard()
                    if background is not None:
                        assert_investigation_active(self.store,owner,conversation_id,interaction)

                result=run(raw_question,history,self.get_planner(),self.get_lookup(),self.get_probe(),self.audit,
                           permitted_checks={},question_policy=policy,run_guard=plan_guard,
                           task_memory=task_memory,rolling_context=rolling_context,sources_guard=self.guard_sources,
                           public_request=public_request,public_background=public_background,diagnostic_options=options,
                           investigation_context=background)
                if background is not None:plan_guard()
            except TaskError:
                raise
            except Exception:
                if bounded:
                    if access_guard is not None:access_guard()
                    recovered=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,stored_topic,bounded)
                    return self.start_passive_request(owner,recovered,access_guard,bounded['scope'])
                if kind=='clarify':return dict(route,status=kind)
                return {'status':'incomplete','route':kind,'answer':'模型或审计服务暂不可用，请检查配置后重试。','events':[]}
            if bounded and (result['status']=='answered' or result['status']=='incomplete' and
                    result.get('reason') in ('invalid_action_or_citation','model_invalid_json','model_invalid_response','model_timeout','model_connection_failed','model_circuit_open','model_rate_limited','planner_or_tool_unavailable')):
                if access_guard is not None:access_guard()
                recovered=self.bounded_intake(owner,conversation_id,interaction['revision'],raw_question,history,stored_topic,bounded)
                result.pop('reason',None)
                return self.start_passive_request(owner,dict(result,**recovered),access_guard,bounded['scope'])
            result=self.finish_intake(owner,conversation_id,interaction['revision'],approval_flow,
                                      question,raw_question,history,anchor,target_hint,route,options,policy,
                                      expected_topic,stateful_intake,result)
            targets=bounded['scope'] if bounded else route.get('scope')
            if not bounded and (forbids_inspection(raw_question) or result.get('diagnostic_goal')=='live_configuration'):
                targets=None
            task=result.get('task')
            if (not bounded and targets and route['intent']=='live'
                    and isinstance(task,dict) and task.get('diagnostic_goal')=='routing'):
                # The user names one device and explicitly asks about its outbound route.
                # A proposal can use the policy table for that read, but cannot add devices or checks.
                explicit=target_devices(raw_question)
                if (len(explicit)==1 and set(explicit)==set(targets)
                        and re.search(r'出站选路|策略路由|路由规则', raw_question)
                        and not re.search(r'只|仅|别|不要|不用|顺便|同时|发包|ping|icmp|改|重启',raw_question,re.I)):
                    targets={explicit[0]:['policy_routes']}
            if (stateful_intake and result['status']=='answered' and not result.get('evidence')
                    and route['intent']!='live' and route['domain'] in ('lab','topology','libraries')):
                fact=related_fact(self.store,owner,interaction['scene'],None if learn_from_history else conversation_id,raw_question)
                if fact:
                    if access_guard is not None:access_guard()
                    if self.store.interaction(owner,conversation_id)['revision']!=interaction['revision']:
                        raise TaskError('mode_changed')
                    result['topology_observation']=fact
                    result['answer']+='\n\n最近一次现场观测（'+fact['collected_at'][:19].replace('T',' ')+' UTC）：'+fact['summary']+'。'
            return self.start_passive_request(owner,result,access_guard,targets if stateful_intake else None)
        finally:self.slot.release()

    def finish_intake(self,owner,conversation_id,revision,approval_flow,question,raw_question,history,
                      anchor,target_hint,route,options,policy,expected_topic,stateful_intake,result):
        kind=route['route']
        chat_space=policy.get('chat_space')
        ha_chat=chat_space=='ha'
        continued=result.pop('continues_topic',False)
        brief=result.pop('intake_brief',None)
        runbook=result.pop('runbook',None)
        prior=policy.get('recent_topic')
        scope_failed=(result['status']=='incomplete' and result.get('reason') in
                      ('invalid_action_or_citation','invalid_intake_decision','model_invalid_json','model_invalid_response')
                      and not read_method_question(raw_question) and any(
                          step.get('action') in ('prepare_diagnostic','investigate')
                          for step in result.get('model_trace',[])))
        if continued and prior:
            question=join_diagnostic_question(prior['question'],raw_question)
            anchor=question
        if result['status']=='diagnostic_proposed':
            scope=result.pop('scope')
            goal=result.pop('goal',None)
            if chat_space=='general':return dict(result,**self.diagnostic_handoff())
            previous=policy.get('clarification_context')
            if previous and question==raw_question and not any(m['role']=='user' and m['content']==previous for m in history):
                history=(history+[{'role':'user','content':previous}])[-8:]
                while sum(len(message['content']) for message in history)>MAX_TOTAL:
                    history=history[1:]
            result.update(self.request_diagnostic(owner,question,history,scope,conversation_id,revision,approval_flow,goal,brief,
                expected_topic=expected_topic if stateful_intake else ANY_TOPIC,
                replace_active=ha_chat and bool(REPLACE.match(raw_question)),required_checks=policy.get('bounded_scope'),
                runbook=runbook))
            focus='、'.join(DEVICE_LABELS[device] for device in scope)
            self.store.remember_topic(owner,conversation_id,anchor,'topology','live',focus,
                                      revision,expected=expected_topic,
                                      intake={'brief':brief,'phase':'scope_proposed','task_id':result['task']['id']} if brief else None)
        elif stateful_intake and (result['status']=='current_unverified' or result.get('reason')=='intake_no_progress'):
            prior_intake=policy.get('diagnostic_intake')
            next_intake={'brief':brief,'phase':'unavailable'} if brief else prior_intake
            if not brief and prior:
                anchor=join_diagnostic_question(prior['question'],raw_question)
            result.update(status='current_unverified',route='current_unverified')
            self.store.remember_topic(owner,conversation_id,anchor,'topology','live',target_hint,
                                      revision,expected=expected_topic,intake=next_intake)
        elif result['status']=='clarify' or (ha_chat and options and (
                scope_failed or result['status']=='answered' and route['intent']=='live')):
            if result['status']!='clarify':
                pending=policy.get('clarification_context')
                if pending:
                    anchor=join_diagnostic_question(pending,raw_question)
                answer=diagnostic_clarification(anchor)
                named=target_devices(raw_question)
                if scope_failed and named:
                    answer='你指定的'+ '、'.join(DEVICE_LABELS[d] for d in named)+'已保留，本次生成的检查范围未通过校验，尚未创建申请。可以重试同一项检查。'
                if LINK_STATE.search(anchor) and ('局端' in anchor or '远端' in anchor):
                    answer='你想核对管理地址、Croc 隧道，还是实际业务收发？本轮还未检查设备。'
                result.update(status='clarify',answer=answer,refs=[],sources=[])
            previous_intake=policy.get('diagnostic_intake') or {}
            prior_question=previous_intake.get('last_question') or ''
            if prior_question and question_key(result['answer'])==question_key(prior_question):
                result.update(status='current_unverified',route='current_unverified',refs=[],sources=[],
                              answer='这次没有生成可执行的检查，现场状态还未核实。已保留你补充的信息。')
                self.store.remember_topic(owner,conversation_id,join_diagnostic_question(prior['question'],raw_question),
                    'topology','live',target_hint,revision,expected=expected_topic,
                    intake=dict(previous_intake,phase='unavailable'))
                return result
            result['route']='clarify'
            self.store.remember_topic(owner,conversation_id,anchor,route['domain'],'clarifying',target_hint,
                                      revision,expected=expected_topic,
                                      intake={'brief':brief or previous_intake.get('brief') or update_brief({}),
                                              'phase':'awaiting_input','last_question':result['answer']}
                                      if stateful_intake else None)
        elif kind=='clarify' and (result.get('reason','').startswith('model_') or
                                 chat_space is None and route['intent']=='live' and result['status']=='answered'):
            result.update(route,status=kind)
        else:
            result['route']='general' if kind=='clarify' and result['status']=='answered' else kind
            if stateful_intake and result['status']=='answered':
                self.store.remember_topic(owner,conversation_id,anchor,route['domain'],route['intent'],target_hint,
                                          revision,expected=expected_topic)
        return result

    def compare(self,owner,before_id,after_id):
        if any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value) for value in (before_id,after_id)):
            raise TaskError('comparison_invalid_task')
        # 两条都必须先由服务端检查归属；前端提供的owner、报告或原始输出均不接受。
        before=self.store.get(before_id,owner)
        after=self.store.get(after_id,owner)
        try:return compare_snapshots(before,after)
        except ValueError as error:raise TaskError(str(error)) from None

    def start_approved(self,owner,task_id,access_guard=None):
        # 权限来自本任务实际保存的范围及数据用途批准；网页不能把布尔值当成长期许可。
        permission=self.store.require_automatic_start(task_id,owner)
        return self.resume(owner,task_id,consent=permission,access_guard=access_guard)

    def approve_in_background(self,owner,task_id,data_use,access_guard,automatic=False):
        if not self.background_slots.acquire(False):raise TaskError('busy')
        try:
            task=self.store.decide(task_id,True,AUTO_READ_ACTOR if automatic else 'task-owner',data_use,owner,server_owned=True)
            worker=threading.Thread(target=self.run_background,args=(owner,task_id,access_guard),daemon=True)
            try:worker.start()
            except RuntimeError:
                self.store.cancel(task_id,owner)
                raise TaskError('busy') from None
            return task
        except BaseException:
            self.background_slots.release()
            raise

    def run_background(self,owner,task_id,access_guard):
        try:
            self.resume(owner,task_id,access_guard=access_guard,server_owned=True)
        except (TaskError,unified_chat.ToolRejection):
            try:self.store.cancel(task_id,owner)
            except TaskError:pass
        finally:
            self.background_slots.release()

    def add_task_input(self,owner,task_id,message_id,text):
        with self.input_changed:
            self.store.add_task_input(task_id,owner,message_id,text)
            self.input_changed.notify_all()
        return self.store.get(task_id,owner)

    def cancel_task(self,owner,task_id):
        task=self.store.cancel(task_id,owner)
        with self.input_changed:self.input_changed.notify_all()
        return task

    def resume(self,owner,task_id,consent=True,access_guard=None,server_owned=False):
        if consent is not True: raise TaskError('consent_required')
        if server_owned:
            while not self.slot.acquire(timeout=1):
                access_guard()
                self.store.require_automatic_start(task_id,owner,server_owned=True)
        elif not self.slot.acquire(False): raise TaskError('busy')
        slot_held=True
        try:
            started=time.monotonic()
            if server_owned:access_guard()
            task=self.store.claim(task_id,owner,server_owned=server_owned)

            def active():
                if access_guard is not None:access_guard()
                self.store.assert_active(task_id,owner)

            def read_allowed(device,check):
                if access_guard is not None:access_guard()
                self.store.authorize_read(task_id,owner,device,check)

            def wait_for_input(question,step,seen,deadline):
                nonlocal slot_held
                if not self.store.update_progress(task_id,owner,'awaiting_input',step=step,question=question):
                    raise unified_chat.ToolRejection('investigation_inactive')
                self.slot.release()
                slot_held=False
                with self.input_changed:
                    while time.monotonic()<deadline:
                        active()
                        if len(self.store.task_inputs(task_id))>seen:break
                        self.input_changed.wait(timeout=max(0,min(1,deadline-time.monotonic())))
                    else:return False
                while time.monotonic()<deadline:
                    active()
                    if self.slot.acquire(timeout=max(0,min(1,deadline-time.monotonic()))):
                        slot_held=True
                        return True
                return False

            inbox={}
            if server_owned:
                options=proposal_options('','',task['scene'])
                covered=task['covered_scope']
                for device in options:
                    options[device]['checks']={check:label for check,label in options[device]['checks'].items()
                                               if check not in covered.get(device,())}
                    options[device]['profiles']=inspection_profiles(options[device]['checks'])
                inbox={'scope_options':{device:item for device,item in options.items() if item['checks']},
                       'task_inputs':lambda:self.store.task_inputs(task_id),
                       'wait_for_task_input':wait_for_input,
                       'seal_task_inputs':lambda count:self.store.seal_task_inputs(task_id,owner,count)}
            try:
                policy={'domain':'topology','mode':'diagnostic','scene':task['scene'],'require_current_evidence':True}
                if task['diagnostic_goal']:policy['diagnostic_goal']=task['diagnostic_goal']
                if task['brief'] is not None:policy['diagnostic_brief']=task['brief']
                if task['required_checks']:policy['required_checks']=task['required_checks']
                if task['automatic_read']:policy['automatic_read']=True
                if task['runbook']:policy['runbook']=task['runbook']
                direct_probe=(task['diagnostic_goal']=='icmp_reachability' and task['required_checks']==task['scope']
                              and not re.search(r'定位|排障|排查|查原因|找原因|为什么|继续查',task['question']))
                if direct_probe:
                    policy['direct_probe']=True
                planner=None if direct_probe or task['automatic_read'] or task['runbook'] else self.get_planner()
                result=run(task['question'],task['history'],planner,self.get_lookup(),self.get_probe(),
                    self.audit,permitted_checks=task['scope'],
                    read_guard=read_allowed,
                    task_id=task_id,question_policy=policy,
                    progress=lambda stage,**fields:self.store.update_progress(task_id,owner,stage,**fields),
                    execution_limits=task['execution_limits'],
                    initial_overview=self.prefetch_overview and not server_owned,
                    run_guard=active,sources_guard=self.guard_sources,
                    repair_catalog=self.repairs.catalog(owner,task) if self.repairs else None,**inbox)
                if self.repairs and result.get('repair_recommendation'):
                    try:result['repair_plan']=self.repairs.propose(owner,task,result)
                    except (TaskError,ValueError):result['repair_rejected']=True
                from repair_control import service_assessment
                assessment=service_assessment(result.get('evidence',[]))
                if assessment:result['ha_service_assessment']=assessment
            except Exception:
                result={'status':'incomplete','answer':'诊断未完成，请检查模型和执行服务；已经记录的检查保留，不自动重试。'}
            result['ui_elapsed_seconds']=round(time.monotonic()-started,3)
            result['timing']={'total_seconds':result['ui_elapsed_seconds'],
                'model_seconds':round(sum(row.get('elapsed_ms',0) for row in result.get('model_trace',[]))/1000,3),
                'device_seconds':round(sum(row.get('host_duration_ms',row.get('duration_ms',0)) for row in result.get('evidence',[]))/1000,3)}
            finished=self.store.finish(task_id,owner,result)
            _, credentials=filter_log_output(task['question'])
            self.audit.record(task_id,0,'investigation_result',{
                'status':finished['status'],'reason':result.get('reason'),
                'answer':audit_text(result.get('answer'),credentials),'refs':result.get('refs',[]),
                'observations':[{'id':item['id'],'device':item['device'],'check':item['check'],
                                 'ok':item.get('ok'),'freshness':item.get('freshness')}
                                for item in result.get('evidence',[])]})
            return finished
        finally:
            if slot_held:self.slot.release()
