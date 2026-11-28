"""第一阶段的保守规则分流；开发样例通过不代表自然语言分类准确率。"""
import re
from retrieval.knowledge_freshness import historical_request
from legacy.unified_chat import prefetch_query, new_general_topic
from retrieval.lab_basics import is_basics_query, is_wiring_query
from retrieval.knowledge_catalog import query_scope
from devices.device_registry import DEVICES as REGISTRY, profile
from retrieval.qa import health_counter_context
from retrieval.topology_entities import source_ids
from legacy.learning_intent import general_agent_question, general_ha_question, read_method_question, public_learning_topic, answer_discussion, documented_path_question, direct_inspection_request, forbids_inspection

TECH = ('python','linux','fastapi','gradio','javascript','java','sql','mysql',
        'tcp','udp','http','ssh','json','rag','agent','llm','大模型','函数','列表','字典','元组','默认路由','网络','数据库',
        '交换机','网线','光纤','物理端口')
TOPOLOGY = ('局端','远端','router','路由器','croc','keepalived','主备','备机','备用机',
            '拓扑','tun16','ppp','vrrp','watchdog','业务vip','六卡','三卡','6卡','3卡')
LAB = ('实验室','我们','本项目','这套','这台','这边','设备')
LIVE = ('现在','当前','此刻','今天','刚才','刚刚','突然','仍然')
FAULT = ('故障','异常','不通','断了','中断','连不上','掉线','报错','失败','没有业务','没业务','无法启动','没运行','不运行','未运行',
         '起不来','启动不了','访问不了','打不开','卡住','断断续续','丢包','延迟很高','不工作','排障')
INSTRUCTION = ('如何','怎么','怎样','步骤','操作说明')
GENERAL = ('一般','通用','概念','是什么','原理','区别','作用')
STATUS = (r'情况(怎么样|怎样|如何|呢)|怎么样了[？?]?$|怎么样[？?]?$|还(?:能|可以)(?:用|工作|访问|传输)吗|(?:正在|还在|在)运行(?:着)?(?:哪些|什么)(?:程序|服务)|(?:哪些|什么)(?:程序|服务)(?:正在|还在|在)运行|(?:主机|备机|设备)(?:还)?能用吗|'
          r'在线吗|正常吗|状态(怎么样|怎样|如何|是什么|如何了)?[？?]?$|'
          r'有没有(vip|ip|业务|连接|故障)|有(vip|ip|业务|连接|故障)(了吗|了没|吗|没)|'
          r'vip(还)?在吗|还有vip吗|是否(在线|正常|运行|启动|有vip)|'
          r'运行(吗|着吗|了吗|了没)|启动(了吗|了没)|拨(上号|通)(了吗|了没|没)|'
          r'拨号(成功|完成)(了吗|了没有|了没|没|没有)|'
          r'(ppp|接口|隧道).*(up|down|正常)(吗|了吗)|'
          r'能否(连通|访问)|通了吗|恢复了吗|谁(是|在).*主|哪台.*(主|vip)')


def target_devices(text):
    """只有明确节点或明确两台时才给目标，不把一个端猜成主机。"""
    result = [
        device for device in REGISTRY
        if device.startswith('router-') and re.search(
            r'(?<![a-z0-9])(?:router|路由器)[\s-]*' + device[-1] + r'(?!\d)',
            text, re.I)
    ]
    text=text.replace('备用机','备机').replace('主节点','主机').replace('备用节点','备机')
    groups={profile(device)['side_label'] for device in REGISTRY}
    for label in sorted(groups):
        devices={info['inventory_role']:device for device,info in REGISTRY.items() if profile(device)['side_label']==label}
        if re.search(label+r'.{0,3}(主备|主机和备机|主机与备机|两台)',text):
            result.extend(devices[role] for role in ('primary','backup') if role in devices)
        else:
            if re.search(label+r'.{0,3}主机',text) and 'primary' in devices: result.append(devices['primary'])
            if re.search(label+r'.{0,3}备机',text) and 'backup' in devices: result.append(devices['backup'])
    return list(dict.fromkeys(result))


def classify(question,history=None,target_hint=None,inherited_live=False):
    if not isinstance(question,str) or not 1<=len(question.strip())<=1000:
        raise ValueError('Invalid question')
    q=question.strip().lower()
    from retrieval.public_lookup import requested as public_requested
    if (public_requested(question) and not inherited_live and not target_hint and
            not re.search(r'实验室|我们|本项目|这套|这台|这边|设备|局端|远端|router|路由器|主机|备机|主备|拓扑|ppp|tun\d|vip|现场|安装|运行|部署|故障|异常|不通',q)):
        return {'route':'general','domain':'general','intent':'explain'}
    discussing=answer_discussion(question.rsplit('\n追问：',1)[-1])
    if discussing:
        q=re.sub(r'刚才|刚刚', '', q)
        inherited_live=False
    if public_learning_topic(question):
        return {'route':'general','domain':'basics','intent':'explain'}
    if general_agent_question(question) or general_ha_question(question):
        return {'route':'general','domain':'general','intent':'explain'}
    # 用户描述自己的学习阶段，不是在报告设备当前状态；其他当前/检查信号仍保留。
    q=re.sub(r'(我|我们)现在(?=(?:还)?(?:不了解|不熟悉|不懂|想了解|想学习|在学习))',r'\1',q)
    # 省略对象的追问只接最近相关话题；明确转到Python等话题时不沿用拓扑。
    context=prefetch_query(question,history or [])
    if not context and history and re.match(r'^(那|那么|它|这个)',q) and not new_general_topic(q):
        previous=next((m['content'] for m in reversed(history) if m['role']=='user'),'')
        if any(t in previous.lower() for t in TOPOLOGY): context=previous+'\n追问：'+question
    text=context or question
    if context and not any(t in q for t in ('局端','远端')):
        if '主机' in q:text=re.sub(r'(局端|远端).{0,3}备机',r'\1主机',text)
        elif '备机' in q:text=re.sub(r'(局端|远端).{0,3}主机',r'\1备机',text)
    topo=(documented_path_question(text) or bool(source_ids(text)) or any(t in text.lower() for t in TOPOLOGY) or health_counter_context(text) or is_wiring_query(text)
          or (bool(re.search(r'(?<![a-z0-9_])vip(?![a-z0-9_])', text.lower())) and
              any(t in text.lower() for t in ('接口','服务','地址','active','持有','主机','备机'))))
    # 明确排除设备话题的附句不能把Python等通用问题重新拉回现场历史。
    domain_text=re.sub(r'(不讨论|不涉及|无需讨论)(实验室|设备)','',q)
    lab=topo or any(t in domain_text for t in LAB)
    generic=any(t in q for t in GENERAL) and not any(t in q for t in LAB+LIVE)
    instruction=read_method_question(q) or bool(re.search(r'(如何|怎么|怎样).*(检查|排查|排障|诊断|查看|读取|判断|确认|重启|操作)|步骤|操作说明',q))
    # “怎么检查”是说明步骤；“请检查”或明确当前现象才申请实际读取。
    direct_check=direct_inspection_request(q)
    # “为什么业务不通”仍可能是故障；只有明确原理/条件或典型备机停Croc机制问法走解释。
    mechanism=discussing or bool(re.search(r'为什么.*备机.*不运行.*croc|备机.*为什么.*不运行.*croc',q))
    if (health_counter_context(q) and any(t in q for t in ('计数','相乘','阈值','规则','含义','配置'))
            and not direct_check and not any(t in q for t in LIVE)):
        mechanism=True
    explanation=bool(re.search(r'是什么|什么意思|是干什么|有什么用|有啥用|作用|原理|区别|为什么需要|为什么要|谁负责|谁启动|谁停止|也是这样吗|这有什么用',q))
    if explanation and not direct_check and not re.search(STATUS,q) and not re.search(r'现在|当前|此刻|是否.*运行|运行了吗|启动了吗',q):
        inherited_live=False;mechanism=True
    history_only=historical_request(q) and not direct_check and not any(t in q for t in LIVE)
    concept_only=bool(re.search(r'不查现场|不检查现场|不用查现场|不要查现场',q)
                      and re.search(r'只讲平时|只讨论一般|只讲原理|平时的情况',q) and not direct_check)
    if concept_only:mechanism=True;inherited_live=False
    conditional=any(t in q for t in ('如果','假如','规则','原理')) or bool(re.search(r'就一定|不一定|是否意味着|能(?:直接)?(?:说明|证明)|单凭|是不是.*就|只要.*就',q))
    hypothetical=bool(re.match(r'^(?:那|那么)?(?:如果|假如|假设)',q))
    status_question=bool(re.search(STATUS,q)) and (not generic or '状态是什么' in q) and not conditional and not mechanism
    diagnostic=not history_only and not concept_only and lab and (inherited_live or not instruction) and (inherited_live or direct_check or (any(t in q for t in LIVE) and not hypothetical) or
                                            status_question or
                                            (any(t in q for t in FAULT) and not generic and not mechanism and not conditional))
    if diagnostic and forbids_inspection(q):
        return {'route':'current_unverified','domain':'topology' if topo else 'lab','intent':'live',
                'answer':'还没有检查设备，当前状态未核实。需要确认时，我会先请你同意，再查看相关设备。'}
    if diagnostic:
        device_text=target_hint if target_hint is not None else text
        devices=target_devices(device_text)
        if not devices:
            return {'route':'clarify','domain':'topology','intent':'live',
                    'answer':'当前状态未核实。请说明是局端还是远端、主机还是备机，以及要确认的状态；明确目标后再申请只读检查。'}
        checks=['services','interfaces']
        if any(t in q for t in ('路由','业务','不通','tun16')): checks.append('routes')
        if ('日志' in q or ('croc' in q and any(t in q for t in FAULT+('没运行',)))):
            checks.append('logs')
        return {'route':'diagnostic','domain':'topology','intent':'live',
                'scope':{d:list(checks) for d in devices},'answer':'需要核对本轮状态，已准备只读诊断申请。'}
    if query_scope(text)['active']:
        return {'route':'lab_knowledge','domain':'libraries','intent':'explain'}
    if not topo and is_basics_query(question):
        return {'route':'lab_knowledge','domain':'basics','intent':'explain'}
    if lab:
        return {'route':'lab_knowledge','domain':'topology' if topo else 'lab','intent':'explain'}
    if any(t in q for t in TECH+TOPOLOGY):
        return {'route':'general','domain':'general','intent':'explain'}
    if any(t in q for t in FAULT) or re.match(r'^(那|那么|它|这个)',q):
        return {'route':'clarify','domain':'unknown','intent':'unknown',
                'answer':'请补充这是实验室里的哪项设备或服务问题，以及具体表现。'}
    # 通用问答不依赖术语白名单；工具仍由上面的现场路径与空批准范围约束。
    return {'route':'general','domain':'general','intent':'explain'}
