"""Validated conversation state and bounded requests for HA intake."""
import re
from devices.device_registry import DEVICES, PING_TARGETS, TUNNEL_CHECK, TUNNEL_BACKUP_CHECK, profile
from legacy.learning_intent import direct_inspection_request, forbids_inspection, probe_explanation

DIAGNOSTIC_GOALS = {
    'business_path': '两端实际业务传输',
    'management_path': '设备管理地址互通',
    'connectivity_overview': '远端和局端的当前链路状态',
    'icmp_reachability': '所选路径和方向的 ICMP 应答',
    'ha_configuration': 'Keepalived 文件中的接口和选举字段',
    'tunnel': 'Croc 隧道状态',
    'vip_ownership': '主备 VIP 归属',
    'routing': '指定业务的路由与转发路径',
    'device_state': '指定设备的服务或接口状态',
    'fault': '你描述的故障',
}

UNAVAILABLE = {
    'live_configuration': '当前工具可读 Keepalived 文件中的有限字段，无法核实所问的运行中加载配置。',
}
GOALS = {**DIAGNOSTIC_GOALS, **UNAVAILABLE}
READ_ONLY_PROFILE = ('services', 'interfaces', 'policy_routes', 'logs', 'ha_config')


def inspection_profiles(checks):
    return {'read_only': list(READ_ONLY_PROFILE)} if set(READ_ONLY_PROFILE)<=set(checks) else {}


def expand_scope(scope, options):
    if not isinstance(scope, dict) or not scope:
        raise ValueError('invalid_diagnostic_scope')
    result = {}
    for device, checks in scope.items():
        item = options.get(device)
        if (not item or not isinstance(checks, list) or not checks
                or any(not isinstance(check, str) for check in checks) or len(checks)!=len(set(checks))):
            raise ValueError('invalid_diagnostic_scope')
        expanded = []
        profiles = item.get('profiles', {})
        for check in checks:
            expanded.extend(profiles[check] if check in profiles else [check])
        if any(check not in item['checks'] for check in expanded):
            raise ValueError('invalid_diagnostic_scope')
        result[device] = list(dict.fromkeys(expanded))
    return result


PATHS = ('management', 'tunnel', 'business', 'heartbeat')
MISSING_FIELDS = ('goal', 'path', 'device', 'source', 'destination', 'symptom', 'time_window', 'operating_mode')
EMPTY = {'goal': None, 'path': None, 'devices': [], 'missing': None}


def update_brief(update, previous=None):
    if not isinstance(update, dict) or set(update)-set(EMPTY):
        raise ValueError('invalid_intake_brief')
    brief = {**(previous or EMPTY), **update}
    for key, allowed in (('goal', GOALS), ('path', PATHS), ('missing', MISSING_FIELDS)):
        value = brief[key]
        if value is not None and (not isinstance(value, str) or value not in allowed):
            raise ValueError('invalid_intake_brief')
    devices = brief['devices']
    if (not isinstance(devices, list) or len(devices) > len(DEVICES)
            or any(not isinstance(device, str) or device not in DEVICES for device in devices)
            or len(devices) != len(set(devices))):
        raise ValueError('invalid_intake_brief')
    return dict(brief, devices=sorted(devices))


def question_key(text):
    return ''.join(char.lower() for char in text if char.isalnum())


NODE = r'(?:局端|远端)(?:的)?(?:主机|备机)'
NODE_IDS = {device['label']: ident for ident, device in DEVICES.items()}
DIRECTED = re.compile(rf'({NODE})(?:的管理(?:地址|口))?\s*(?:向|到|至|(?:上)?\s*ping\s*(?:一下)?|->|→)\s*({NODE})', re.I)
REACHES = re.compile(rf'({NODE})\s*(?:(?:这会儿|现在|当前)\s*)?(?:能不能|能否|可以)\s*(?:连上|访问到?|摸到|ping\s*(?:通|到))\s*({NODE})',re.I)
PROBE_TOWARD = re.compile(rf'({NODE})\s*对\s*({NODE})(?:的管理(?:地址|口))?[^。；，]{{0,12}}\bping\b', re.I)
ICMP = r'(?<![a-z0-9_])(?:ping|icmp)(?![a-z0-9_])'
MANAGEMENT = re.compile(ICMP + r'|\bssh\b|管理(?:网|地址|口|面)|登录(?:用的)?地址|互相访问|互访', re.I)
EXPLICIT = re.compile(r'^\s*(?:(?:请|帮我|麻烦|替我)\s*)?(?:(?:先)?(?:只|仅)\s*)?(?:查|看|核对|检查|测|验证|探测)')
REPLACE = re.compile(r'^\s*(?:请\s*)?(?:改(?:成|为|查|看|测)|换(?:成|到|为|查)|(?:别|不用|不要)(?:再)?查[^。！？；]{1,35}[，,；]\s*(?:改|换|查|看))')
EXPLICIT_BOTH = re.compile(r'双向|(?:两个|两边)方向|来回|和反向|各\s*(?:ping|测)', re.I)
ONE_WAY = re.compile(r'(?:只|仅)(?:要|做|看|测|查|测试)?\s*(?:单向|正向|一个方向)|'
                     r'(?:不要|别|不用|无需|不必|禁止|不)(?:做|测|查|要|再|测试)*\s*(?:双向|反向|来回|往返)')
PRONOUN_PING = re.compile(rf'从\s*({NODE})\s*(?:上)?\s*ping\s*(?:一下)?\s*它(?:的管理(?:地址|口))?', re.I)
NO_PROBE = re.compile(r'(?:不要|(?<![分特识辨区个告离差])别|不用|无需|不必|禁止|不允许|不想|不许|避免)'
                      r'[^，,。；！？]{0,36}(?:'+ICMP+r'|发包|主动探测)|(?:先不|不)\s*(?:'+ICMP+r'|发包|做主动探测)', re.I)
LINK_STATE = re.compile(r'(?:还|都|是|能)?(?:通不通|互通|连通|可达|互访|互相访问|连得上|连不上|接得上|能连上|能访问到?|能通信|断开|断了|连着|通(?:的|了)?[吗么没呢]|有连接)')
CURRENT = re.compile(r'现在|当前|目前|此刻|这会儿|眼下|今天|刚刚|刚才|还|已经')
LINK_EXPLANATION = re.compile(r'为什么|为啥|原因|怎么会|怎么知道|以前|历史|上次|平时|通常|正常情况下|意味着|代表|证明|区别|(?:怎么|怎样|如何)(?:看|查|测|验证|判断)')
LINK_GOALS = ('connectivity_overview','management_path','tunnel','business_path')
OTHER_PROBE_TARGET = re.compile(r'业务(?:地址|网段|端口|终端|VIP)|隧道(?:地址|网段|口|接口)|虚拟地址|\bvip\b|心跳|下挂|\b(?:tcp|udp|ssh)\b', re.I)
RECIPROCAL = re.compile(r'互相|相互|互发|互\s*ping|(?:两端|两边|双方)\s*(?:都|分别|各)', re.I)
ROUTE_CHECK = re.compile(r'(?:查|看|核对|检查|读).{0,8}(?:路由表|策略路由|路由规则|选路)')
PREVIOUS = r'(?:原先|原来|原|刚才|上次|之前)的?'
PREVIOUS_TASK = r'(?:连通性)?(?:排障|检查|探测|任务)|(?P<pair>(?:那|这)?两台(?:设备)?)'
PREVIOUS_DIRECTION = r'(?:按(?:照)?|照着?|沿(?:用)?)' + PREVIOUS + r'(?:方向|去向)'
REPEAT_PROBE = r'(?:再\s*)?(?:ping|(?:发起\s*)?ICMP\s*探测|探测|复测|重测|测)\s*(?:一下|一次|一遍)?'
REPLY_ONLY = r'(?:只|仅)(?:想)?(?:看|确认)(?:有无|有没有|是否有)(?:回包|回应|应答)'
RESUME_PROBE = re.compile(
    rf'^\s*(?:请|帮我|麻烦)?\s*(?:(?:回到|继续|恢复|接着){PREVIOUS}(?:{PREVIOUS_TASK})[，,、\s]*)?'
    rf'(?:继续|接着|再)?\s*{PREVIOUS_DIRECTION}\s*{REPEAT_PROBE}'
    rf'(?:[，,、\s]*{REPLY_ONLY})?[。！!？?\s]*$', re.I)
UNBOUND_REQUEST = re.compile(r'(?:不要|不用|别|不)(?:再)?(?:指定|绑定|绑|固定)(?:源|网口|接口|出口)|(?:按|用)(?:系统|默认)(?:路由|选路)')
CHECK_METHOD = re.compile(r'(?:(?:你|助手)(?:是|刚才)?怎么|你(?:是)?通过什么).{0,8}(?:进的?设备|登录|执行|发(?:的)?包|ping)',re.I)


def bidirectional_ping(question):
    return bool(not ONE_WAY.search(question) and re.search(ICMP,question,re.I)
                and (EXPLICIT_BOTH.search(question) or RECIPROCAL.search(question)))


# A request for a current reciprocal measurement. Additional clauses, named
# protocols, negative instructions and hypothetical questions take other paths.
COMMUNICATION = re.compile(
    r'(?:(?:我)?(?:就|只)?(?:想知道|想看|想确认|问的是)|请|帮我|麻烦|实测|测一下|看看|确认一下|那|所以|\s)*'
    r'(?:(?:局端(?:和|跟|与)远端|远端(?:和|跟|与)局端|两端|两边|双方)(?:之间)?)?'
    r'(?:现在|当前|到底)?(?:能不能|能否|是否|可以)?(?:互相|相互|双向)(?:通信|通讯|连通)|'
    r'(?:请|帮我|实测|测一下|看看)?(?:两端|两边)(?:现在)?(?:能否|能不能|是否)?互通')
TUNNEL_PROBE = re.compile(r'(?:请|帮我|实测|测一下|检查一下|核对)?(?:两端|局端和远端|远端和局端)?'
                          r'(?:的)?(?:tun16|Croc\s*隧道|隧道)(?:地址)?(?:两端)?'
                          r'(?:双向|互相|相互|互)?\s*(?:ping|探测|实测)(?:一下|一次)?',re.I)


def connectivity_measurement(question):
    text=question.strip(' 。.！？?!')
    return bool(COMMUNICATION.fullmatch(text) or TUNNEL_PROBE.fullmatch(text))


def named_direction(question):
    match = DIRECTED.search(question) or REACHES.search(question) or PROBE_TOWARD.search(question)
    if match:
        return tuple(NODE_IDS[label.replace('的', '')] for label in match.groups())
    match = PRONOUN_PING.search(question)
    if match:
        source = NODE_IDS[match[1].replace('的', '')]
        prefix = re.split(r'[。；;！？!?\n]', question[:match.start()])[-1]
        targets = {NODE_IDS[label.replace('的', '')] for label in re.findall(NODE, prefix)}
        if len(targets) == 1 and source not in targets:
            return source, targets.pop()
    return None


def resume_probe(question, previous):
    intent = RESUME_PROBE.fullmatch(question)
    if (not intent or forbids_inspection(question) or NO_PROBE.search(question)
            or re.search(NODE,question) or OTHER_PROBE_TARGET.search(question)
            or re.search(r'\d+\.\d+\.\d+\.\d+|http|改|反向|双向|同时|顺便|重启',question,re.I)):
        return None
    scope = previous['scope'] if previous else {}
    devices = set(scope)
    devices.update(PING_TARGETS[check] for checks in scope.values() for check in checks if check in PING_TARGETS)
    if intent['pair'] and len(devices) != 2:
        scope = {}
        devices.clear()
    brief = update_brief({'goal':'icmp_reachability','path':'management','devices':sorted(devices),
                          'missing':None if scope else 'source'})
    return {'brief':brief,'scope':scope,'continues':False,
            'question':None if scope else '这个对话里没有可恢复的已完成探测。请说一下从哪台设备测到哪台。'}


def route_followup(question, topic):
    """A correction of the test method is not a request to repeat the old probe."""
    if forbids_inspection(question) or re.search(r'如果|假设|怎么查|怎么看|如何|举例|历史|重启|改路由|删路由|加路由|写路由|取消|先别查',question):
        return None
    explicit=re.findall(NODE,question)
    devices={NODE_IDS[label.replace('的','')] for label in explicit}
    prior=((topic or {}).get('intake') or {}).get('brief') or {}
    if not devices:
        sides={side for side in ('局端','远端') if side in question}
        devices={device for device in DEVICES if profile(device)['side_label'] in sides}
    if not devices:
        devices=set(prior.get('devices',[]))
    correcting=bool(re.search(ICMP,question,re.I) or prior.get('goal')=='icmp_reachability')
    if correcting and ROUTE_CHECK.search(question) and not re.search(r'router|路由器|三卡|六卡|物理机|云服务器|\b\d{1,3}(?:\.\d{1,3}){3}\b',question,re.I):
        if devices:
            return {'brief':update_brief({'goal':'routing','devices':list(devices),'path':prior.get('path')}),
                    'scope':{device:['policy_routes'] for device in sorted(devices)},'continues':not explicit}
    if UNBOUND_REQUEST.search(question) and re.search(ICMP,question,re.I):
        return {'brief':update_brief({'goal':'icmp_reachability','devices':list(devices),'path':'management'}),
                'scope':{},'continues':True,'method_correction':True,'unavailable':
                '后续 ping 让系统选源地址和出口。需要复测时先查到目标的路由，确认后再发包。当前没有发包。'}


def side_ping_request(question, brief, scoped, observed_holders):
    """Resolve reciprocal probes from topology, with concrete endpoints for approval."""
    named_sides = '局端' in question and '远端' in question
    mentions_endpoint = re.search(r'局端|远端|主机|备机|主备|这台|那台',question)
    devices = brief.get('devices', []) if scoped and not REPLACE.match(question) else []
    groups = {profile(device)['group'] for device in devices}
    inherited_link = not mentions_endpoint and groups=={'local','remote'} and brief.get('goal') in LINK_GOALS
    inherited_pair = (not mentions_endpoint and len(devices)==2
                      and brief.get('goal') in ('icmp_reachability','management_path','device_state'))
    if OTHER_PROBE_TARGET.search(question) or (inherited_link and not named_sides
            and brief.get('path') in ('tunnel','business') and '管理' not in question):
        return {'brief':update_brief({'goal':'icmp_reachability'}),'scope':{},'continues':inherited_link,
                'unavailable':'请明确要测哪条路径。登记的 tun16 可说“两端的隧道互相 ping 一下”；业务终端需要另行确认收发。本轮未发包。'}
    if named_sides and re.search(r'主备|四台|每台|所有设备',question):
        return {'brief':update_brief({'goal':'icmp_reachability','path':'management','missing':'device'}),
                'scope':{},'continues':False,'question':'要测主机之间、备机之间，还是指定一对设备？'}
    if inherited_pair or named_sides or inherited_link:
        scope = {}
        plan = {'brief':update_brief({'goal':'icmp_reachability','path':'management'}),
                'scope':scope,'continues':not named_sides}
        if inherited_pair:
            source,target = devices
        else:
            # 最近两组 VIP 观测能选出资源持有者时用对应设备，否则展示固定主机作为待确认目标。
            primary = {profile(device)['group']:device for device,item in DEVICES.items()
                       if item['inventory_role']=='primary'}
            source,target = (observed_holders.get(group,primary[group]) for group in ('local','remote'))
        targets = {target:check for check,target in PING_TARGETS.items()}
        scope.update({source:[targets[target]],target:[targets[source]]})
        plan['brief']['devices']=sorted(scope)
        return plan
    return {'brief':update_brief({'goal':'icmp_reachability','path':'management','missing':'device'}),
            'scope':{},'continues':False,'question':'要测局端和远端之间，还是同一端的主备之间？'}


def side_link_request(question):
    """Map a current side-to-side question to registered passive evidence."""
    if not ('局端' in question and '远端' in question and LINK_STATE.search(question)
            and (CURRENT.search(question) or EXPLICIT.search(question) or re.search(r'[吗么？?]',question))):
        return None
    if (re.search(NODE,question) or LINK_EXPLANATION.search(question)
            or re.search(ICMP+r'|\b(?:ssh|tcp|udp|https?)\b|发包|探测|端口|心跳|\d|改配置|重启|停服务',question,re.I)):
        return None
    business=bool(re.search(r'业务|端到端|实际收发|应用|传(?:到|过去|输)',question))
    management=bool(MANAGEMENT.search(question) and not re.search(r'隧道|croc',question,re.I))
    if management and business:
        goal,path='connectivity_overview',None
    elif business:
        goal,path='business_path','business'
    elif management:
        goal,path='management_path','management'
    elif re.search(r'隧道|croc',question,re.I):
        goal,path='tunnel','tunnel'
    else:
        goal,path='connectivity_overview',None
    scope={device:['interfaces'] for device in DEVICES if profile(device)['group'] in ('local','remote')}
    if not scope:
        return None
    return {'brief':update_brief({'goal':goal,'path':path,'devices':list(scope)}),
            'scope':scope,'continues':False}


def bounded_request(question, topic=None, observed_holders=None):
    """Resolve explicitly scoped checks. Other requests still use semantic intake."""
    q = question.strip().replace('备用机', '备机').replace('主节点', '主机').replace('备用节点', '备机')
    # A restriction on other items is not a ban on the named check.
    q = re.sub(r'[，,；。]\s*(?:其他|其余)(?:项|项目|检查)?(?:先)?(?:不用|不要|不必|无需|别)(?:再)?查[。！!]*$', '', q)
    if REPLACE.match(q) and re.match(r'^(?:别|不用|不要)', q):
        q = re.split(r'[，,；]', q, maxsplit=1)[-1].strip()
    if NO_PROBE.search(q) and re.search(NODE,q):
        positive=[part for part in re.split(r'[，,。；！？]',q) if not NO_PROBE.search(part)]
        q='，'.join(positive).strip('， ')
        if not q:
            return {'brief':update_brief({'goal':'icmp_reachability','path':'management'}),'scope':{},
                    'continues':False,'unavailable':'已记录本次不发起主动探测。'}
    if (forbids_inspection(q) or probe_explanation(q) or re.search(r'先别查|先不查|不做探测', q, re.I)
            or re.search(r'如果|假如|假设|原理|怎么理解|举例|怎么(?:查|测|判断|使用|ping)|如何', q, re.I)
            or re.search(r'router|路由器|六卡|三卡|云服务器|物理机', q, re.I)):
        return None

    prior = (topic or {}).get('intake') or {}
    brief = prior.get('brief') or {}
    waiting = prior.get('phase') in ('awaiting_input', 'unavailable')
    if not NO_PROBE.search(question) and not connectivity_measurement(q):
        side_request=side_link_request(q)
        if side_request:
            return side_request
    scoped=prior.get('phase')=='scope_proposed'
    link_followup=scoped and brief.get('goal') in LINK_GOALS
    if connectivity_measurement(q) and not NO_PROBE.search(question):
        named_sides='局端' in q and '远端' in q
        if named_sides or scoped and brief.get('goal') in (*LINK_GOALS,'icmp_reachability'):
            tunnel_only=bool(TUNNEL_PROBE.fullmatch(q.strip(' 。.！？?!')))
            pair=side_ping_request('局端和远端互相 ping' if named_sides else '互相 ping',
                                  dict(brief,path=None) if tunnel_only else brief,scoped,observed_holders or {})
            if pair['scope']:
                for device,checks in pair['scope'].items():
                    tunnel_check=(TUNNEL_BACKUP_CHECK if profile(device)['group']=='local'
                                  and 'remote-backup' in pair['scope'] else TUNNEL_CHECK)
                    pair['scope'][device]=[tunnel_check] if tunnel_only else checks+[tunnel_check]
                pair['brief']['path']='tunnel' if tunnel_only else None
                return pair
    continued_management = waiting and brief.get('path') == 'management'
    direction = named_direction(q)
    explicit_nodes = {NODE_IDS[label.replace('的', '')] for label in re.findall(NODE, q)}
    both_ping = bidirectional_ping(q)
    both_directions = both_ping or bool(EXPLICIT_BOTH.search(q) and not ONE_WAY.search(q))
    if both_ping:
        if NO_PROBE.search(question) or LINK_EXPLANATION.search(q) or re.search(r'是什么|什么意思|用法',q):
            return None
        if re.search(r'\b\d{1,3}(?:\.\d{1,3}){3}\b|https?://|/\d{1,2}\b', q):
            return {'brief':update_brief({'goal':'icmp_reachability','path':'management'}),'scope':{},
                    'continues':False,'unavailable':'这项探测使用拓扑设备的固定管理地址。请用设备名指定两端，本轮未发包。'}
        if not explicit_nodes and not re.search(r'\d|https?://|重启|停服务|改配置|顺便|同时查|再查',q,re.I):
            return side_ping_request(q,brief,scoped,observed_holders or {})
    selected_management = bool(MANAGEMENT.search(q) and (waiting or link_followup or direction or
        re.search(NODE, q) or re.search(r'局端.*远端|远端.*局端', q)))
    if direction and not selected_management and not continued_management:
        return None
    if selected_management or continued_management and (direction or both_directions):
        if OTHER_PROBE_TARGET.search(q) or re.search(r'登录(?:成功|失败|不上|进去)',q):
            return {'brief':update_brief({'goal':'management_path','path':'management'}),'scope':{},
                    'continues':False,'unavailable':'当前工具只支持登记管理地址的定向 ICMP。这个地址或协议需要单独验证，当前未读取设备。'}
        if re.search(r'\b\d{1,3}(?:\.\d{1,3}){3}\b|https?://|/\d{1,2}\b', q):
            return {'brief':update_brief({'goal':'icmp_reachability','path':'management'}),'scope':{},
                    'continues':False,'unavailable':'当前只支持固定登记管理地址。请用设备名称指定源和目标，当前未读取设备。'}
        if direction is None and waiting and selected_management and not explicit_nodes:
            previous = (topic or {}).get('question', '')
            directions = {match.groups() for match in DIRECTED.finditer(previous)}
            if len(directions)==1 and not re.search(r'\b\d{1,3}(?:\.\d{1,3}){3}\b|业务地址|隧道地址|双向',previous):
                direction = named_direction(previous)
        state = update_brief({'goal':'icmp_reachability', 'path':'management', 'missing':'source'})
        if both_directions and len(explicit_nodes)==2:
            source,target=sorted(explicit_nodes)
            pairs=[(source,target),(target,source)]
        elif direction and not re.search(r'反过来|(?<!和)反向',ONE_WAY.sub('', q)):
            source, target = direction
            pairs=[(source,target)] if source!=target and (not explicit_nodes or explicit_nodes=={source,target}) else []
        else:
            pairs=[]
        if pairs:
            scope={source:[next(check for check,device in PING_TARGETS.items() if device==target)] for source,target in pairs}
            # A directed request can explicitly name both paths. Do not silently
            # accept only the management half, and do not infer a reverse direction.
            dual_path=bool(re.search(r'管理(?:地址|路径|网)?.{0,5}(?:和|与|及|以及).{0,5}(?:隧道|tun16)|'
                                     r'(?:隧道|tun16).{0,5}(?:和|与|及|以及).{0,5}管理(?:地址|路径|网)?',q,re.I))
            if dual_path:
                if re.search(r'只|仅|不要|不用|不测|别测|排除|除了',q):
                    return None
                if any(profile(source)['group']==profile(target)['group'] for source,target in pairs):
                    return None
                for source,target in pairs:
                    scope[source].append(TUNNEL_BACKUP_CHECK if profile(source)['group']=='local'
                                         and target=='remote-backup' else TUNNEL_CHECK)
                state['path']=None
            for clause in re.split(r'[，,；。]|顺便|同时|再查',q):
                if not re.search(r'服务|进程',clause) or forbids_inspection(clause):continue
                for label in re.findall(NODE,clause):
                    checks=scope.setdefault(NODE_IDS[label.replace('的','')],[])
                    if 'services' not in checks:checks.append('services')
            return {'brief':dict(state, devices=sorted({device for pair in pairs for device in pair}), missing=None),
                    'scope':scope, 'continues':waiting and not REPLACE.match(question)}
        return {'brief':state,'scope':{},'continues':waiting or link_followup,
                'question':'要从哪台设备测到哪台？请给出源和目标，例如“局端主机到远端备机”。'}

    if (waiting or link_followup) and re.fullmatch(r'(?:(?:我说的是|我指的是|想(?:确认|看)|就是|看)\s*)?(?:两端的?)?(?:实际的?)?(?:业务|应用)(?:的)?(?:数据|流量)?(?:能不能|能否|是否)?(?:传(?:过去|输|到对面)|收发|到达)?[。？?]*', q):
        return {'brief':update_brief({'goal':'business_path','path':'business'}), 'scope':{}, 'continues':True,
                'unavailable':'你要确认的是实际业务收发。目前可检查设备、路由和隧道的局部状态。要确认数据到了对端，需要指定业务端点并核对发送和接收。'}

    if not (EXPLICIT.search(q) or direct_inspection_request(q) or REPLACE.match(q)):
        return None
    labels = set(re.findall(NODE, q))
    devices = {NODE_IDS[label.replace('的','')] for label in labels}
    continued = not devices and bool(re.search(r'它的|这台|那台', q)) and bool(brief.get('devices'))
    if continued:
        devices = set(brief['devices'])
    if len(devices) != 1:
        return None
    if re.search(r'为什么|原因|排障|排查|定位|怎么回事|异常|故障|不通|重启|启动一下|停止', q):
        return None
    device = next(iter(devices))
    if re.search(r'keepalived',q,re.I) and re.search(r'文件|磁盘',q) and re.search(r'接口|优先级|选举|vrrp',q,re.I):
        return {'brief':update_brief({'goal':'ha_configuration','path':'heartbeat','devices':[device]}),
                'scope':{device:['ha_config']},'continues':continued}
    if re.search(r'文件|磁盘|加载|生效',q):
        return None
    checks = []
    if re.search(r'服务|进程|keepalived.{0,6}(?:运行|状态)', q, re.I):
        checks.append('services')
    if re.search(r'接口|网口|tun16', q, re.I) and not re.search(r'心跳|配置', q):
        checks.append('interfaces')
    if re.search(r'只|仅',q) and re.search(r'路由表|策略路由|路由规则',q):
        checks.append('policy_routes' if re.search(r'策略|全部|所有',q) else 'routes')
    if re.search(r'只|仅',q) and re.search(r'日志',q):
        checks.append('logs')
    if not checks or re.search(r'配置|业务|连通|访问|\bvip\b', q, re.I) or (not re.search(r'只|仅',q) and re.search(r'路由|日志',q)):
        return None
    # Only take the fast path for a stated single check, an explicit replacement,
    # a known referent, or two named checks. Other plans still use the model.
    goal = 'tunnel' if '隧道' in q or 'tun16' in q.lower() else 'device_state'
    direct = len(checks) == 2 or continued or bool(REPLACE.match(question) or re.search(r'只|仅|隧道接口', q))
    return {'brief':update_brief({'goal':goal,'devices':[device]}), 'scope':{device:checks},
            'continues':continued,'prefer_model':not direct}
