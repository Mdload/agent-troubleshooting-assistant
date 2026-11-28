"""Reuse verified investigation history locally within the authenticated owner's scope."""
from contextlib import closing
from datetime import datetime
import ipaddress
import json
import re

from server.approval_store import DIAGNOSTIC_DATA_USE, LOCAL_READ_USE, SINGLE_APPROVAL, passive_scope
from devices.device_registry import DEVICES, PING_TARGETS, PROBE_CHECKS, TUNNEL_CHECKS, tunnel_endpoints
from devices.diagnostic_probes import ping_counts, ping_route
from legacy.evidence_scope import CHECK_LABELS, collection_time, valid_collection_time
from legacy.execution_workflow import observation_steps, result_summary
from legacy.ha_croc_case import interface_addresses
from legacy.learning_intent import direct_inspection_request, forbids_inspection
from devices.link_inspection import BRIEF_ROW, parse_brief
from legacy.question_routing import target_devices


LOOK_BACK = 30 * 86400
SUPPORTED_GOALS = frozenset(('device_state', 'routing', 'vip_ownership', 'ha_configuration'))
REFERENCE = re.compile(r'刚才|上一轮|上一次|上次|前一轮|之前(?:查|测|看)|那次|当时')
EXPLAIN = re.compile(r'解释|什么意思|是什么|怎么理解|代表什么|说明什么|为啥|为什么|哪(?:个|些|条)|多少|结果|证据|依据|待机')
PING_DISPUTE = re.compile(r'自己|手动|手工|我这边|我测|我进去|我在')
PING = re.compile(r'(?<![a-z0-9])(?:ping|icmp)(?![a-z0-9])|探测|发包', re.I)
FRESH = re.compile(r'现在|当前|此刻|重新(?:检查|探测|读取|执行|查|测)|再(?:查|测|做|发|看|读)|继续(?:检查|排查|定位|查|测)')
OTHER_TARGET = re.compile(r'router|路由器|交换机|三卡|六卡|云服务器|物理机|[36]卡', re.I)
REUSE_EXCLUDED = re.compile(r'如果|假如|假设|原理|历史|上次|刚才|上一|继续|然后|顺便|另外|改查|换成|不要|不用|别|只|仅|'
                            r'重启|关机|修改|删除|清空|执行命令|sudo|router|路由器|交换机|三卡|六卡|云服务器|物理机|'
                            r'\b\d{1,3}(?:\.\d{1,3}){3}\b', re.I)


def saved_observations(db, row):
    result = json.loads(row['result'])
    reservations = {(item['device'], item['check_name']) for item in db.execute(
        'SELECT device,check_name FROM workflow_reads WHERE task=?', (row['id'],))}
    refs = set(result.get('refs', []))
    observations = []
    for item in result.get('evidence', []):
        pair = item['device'], item['check']
        if (pair in reservations and item.get('ok') is True and item.get('origin') == 'device_read'
                and item.get('freshness') == 'current' and valid_collection_time(item.get('collected_at'))
                and isinstance(item.get('output'), str)):
            observations.append(item)
    return observations, refs


def recall_kind(question):
    if OTHER_TARGET.search(question):
        return None
    if FRESH.search(question) and not forbids_inspection(question):
        return None
    if not forbids_inspection(question) and direct_inspection_request(question):
        return None
    if (PING.search(question) and PING_DISPUTE.search(question)
            and re.search(r'你|助手|结果|上次|刚才|之前', question)
            and re.search(r'不通|不同|矛盾|怎么|为什么|为啥', question)):
        return 'icmp'
    if not (REFERENCE.search(question) and EXPLAIN.search(question)):
        return None
    if PING.search(question):
        return 'icmp'
    if re.search(r'接口|网口|地址|vip|tun\d|ppp', question, re.I):
        return 'interfaces'
    if re.search(r'服务|进程|croc|keepalived|停着|运行', question, re.I):
        return 'services'
    if re.search(r'结果|观测|检查|调查|证据|你说|助手', question):
        return 'summary'
    return None


def interface_lines(output):
    addresses = interface_addresses(output)
    if addresses is None:
        return ''
    _, conflicts = parse_brief(output)
    lines = []
    for line in output.splitlines():
        match = BRIEF_ROW.match(line.strip())
        if not match:
            continue
        interface, state = match.group('name', 'state')
        if interface not in addresses or interface in conflicts:
            continue
        fields = line.split()[2:]
        values = []
        for token in fields:
            try:
                address = ipaddress.ip_interface(token)
            except ValueError:
                break
            if str(address.ip) in addresses[interface]:
                values.append(str(address))
        if values and len(values) == len(fields):
            lines.append(' '.join([interface, state, *values]))
    return '\n'.join(dict.fromkeys(lines))


def historical_answer(store, owner, conversation, interaction, topic, question):
    kind = recall_kind(question)
    if kind is None or (topic and topic['domain'] not in ('topology', 'lab')):
        return None
    intake = (topic or {}).get('intake') or {}
    binding = intake.get('task_id')
    with closing(store.connect()) as db, db:
        row = db.execute(
            'SELECT t.*,p.approved_use,p.flow FROM workflow_tasks t LEFT JOIN workflow_data_permissions p ON p.task=t.id '
            'WHERE t.owner=? AND t.conversation=? AND t.scene=? AND t.mode_revision=? '
            'ORDER BY t.rowid DESC LIMIT 1', (owner, conversation, interaction['scene'], interaction['revision'])).fetchone()
        if (row is None or row['status'] not in ('completed', 'incomplete') or not row['result']
                or row['flow'] != SINGLE_APPROVAL or row['approved_use'] not in (LOCAL_READ_USE, DIAGNOSTIC_DATA_USE)
                or binding and binding != row['id']):
            return None
        observations, _ = saved_observations(db, row)
    targets = set(target_devices(question))
    selected = []
    for item in observations:
        check = item['check']
        if targets and item['device'] not in targets and PING_TARGETS.get(check) not in targets:
            continue
        if kind == 'interfaces' or kind == 'summary' and check == 'interfaces':
            output = interface_lines(item['output']) if check == 'interfaces' else ''
        elif kind == 'icmp' or kind == 'summary' and check in PROBE_CHECKS:
            output = ''
            if check in PROBE_CHECKS:
                counts = ping_counts(item['output'])
                if counts:
                    fields = ping_route(item['output'])
                    output = 'ICMP_TX={}\nICMP_RX={}\n'.format(*counts)
                    for key, label in (('source', 'SRC'), ('gateway', 'VIA'), ('interface', 'DEV')):
                        value = fields.get(key, '')
                        if re.fullmatch(r'[0-9a-fA-F.:]{1,45}' if key != 'interface' else r'[A-Za-z0-9_.:-]{1,32}', value):
                            output += 'ICMP_ROUTE_'+label+'='+value+'\n'
                elif item['output'].strip() == 'ICMP_ROUTE_UNAVAILABLE=1':
                    output = 'ICMP_ROUTE_UNAVAILABLE=1\n'
                elif check in TUNNEL_CHECKS and item['output'].strip()=='ICMP_TUNNEL_PATH_MISMATCH=1':
                    output='ICMP_TUNNEL_PATH_MISMATCH=1\n'
        elif check == 'services' and kind in ('services', 'summary'):
            output = result_summary(item)
        else:
            output = ''
        if output:
            selected.append({key:item[key] for key in ('id', 'device', 'check', 'origin', 'collected_at', 'ok')})
            selected[-1].update(output=output, freshness='historical')
    if not selected:
        return None
    lines = []
    for item in selected:
        label = DEVICES[item['device']]['label']
        if item['check'] in PROBE_CHECKS:
            if item['check'] in TUNNEL_CHECKS:
                detail=label+' → 对端 tun16 '+tunnel_endpoints(item['device'],item['check'])[1]
            else:
                target = PING_TARGETS[item['check']]
                detail = label+' → '+DEVICES[target]['label']+'（管理地址 '+DEVICES[target]['address']+'）'
            counts = ping_counts(item['output'])
            if counts:
                detail += '，发出 {} 个探测，收到 {} 个应答。'.format(*counts)
                route = ping_route(item['output'])
                fields = [title+' '+route[key] for key,title in (('source','源地址'),('interface','出口'),('gateway','下一跳'))
                          if key in route]
                if fields:
                    detail += '探测前的选路为'+'、'.join(fields)+'。'
            elif item['output'].strip()=='ICMP_TUNNEL_PATH_MISMATCH=1':
                detail += '本端隧道地址或出口不匹配，当时未发包。'
            else:
                detail += '没有取得可用路由，当时未发包。'
            detail += ' ['+item['id']+']'
        else:
            detail = label+' '+CHECK_LABELS[item['check']]+' ['+item['id']+']\n'+item['output']
        lines.append('采集时间 '+item['collected_at']+'\n'+detail)
    if kind == 'icmp':
        takeaway = '如果手工 ping 的结果不同，先核对当时使用的源设备、源地址、目标地址和出口，再比较两次采集时间。未收到应答只描述这次探测。'
    elif kind == 'interfaces':
        takeaway = '斜杠后的数字是前缀长度。接口状态和地址只说明采集时的本机状态。'
    else:
        takeaway = '以上是采集时的局部观察。'
    return {'status':'answered','route':'diagnostic','answer':'上次检查的历史观测如下。\n\n'+'\n\n'.join(lines)+'\n\n'+takeaway,
            'refs':[item['id'] for item in selected], 'evidence':selected,'execution_steps':observation_steps(selected),
            'sources':[],'events':[],'evidence_scope':{'kind':'historical','current_reads':0},
            'historical_investigation_id':row['id']}


def request_key(question):
    question = re.sub(r'^(?:请帮我|帮我|替我|麻烦你|麻烦|请)\s*', '', question.strip())
    question = re.sub(r'^(?:核对|看看|看一下|看下|查看|检查|查一下|读取)\s*', '', question)
    question = re.sub(r'[\s，。！？、,.!?]+', '', question).lower()
    return re.sub(r'(?:有没有问题|是否有问题|有没有异常|是否异常)$', '', question)


def learned_plan(store, owner, conversation, scene, question, options):
    """Two independent completed conversations must agree on the same evidence-backed plan."""
    targets = set(target_devices(question))
    if (not targets or REUSE_EXCLUDED.search(question) or PING.search(question)
            or not direct_inspection_request(question)):
        return None
    key = request_key(question)
    now = store.clock()
    oldest = datetime.utcfromtimestamp(now - LOOK_BACK)
    with closing(store.connect()) as db, db:
        rows = db.execute(
            "SELECT t.* FROM workflow_tasks t JOIN workflow_modes m ON m.owner=t.owner AND m.conversation=t.conversation "
            "AND m.scene=t.scene AND m.revision=t.mode_revision JOIN workflow_data_permissions p ON p.task=t.id "
            "WHERE t.owner=? AND t.scene=? AND t.conversation<>? AND t.status='completed' AND t.result IS NOT NULL "
            "AND t.expires>? AND p.flow=? AND p.approved_use=? AND t.approver='task-owner' ORDER BY t.rowid DESC LIMIT 36",
            (owner, scene, conversation, now-LOOK_BACK, SINGLE_APPROVAL, DIAGNOSTIC_DATA_USE)).fetchall()
        origins, plan = set(), None
        for row in rows:
            if request_key(row['question']) != key:
                continue
            goal = row['diagnostic_goal']
            scope = json.loads(row['approved_scope'] or '{}')
            if (goal not in SUPPORTED_GOALS or set(scope) != targets or not passive_scope(scope)
                    or not all(set(checks) <= set(options.get(device, {}).get('checks', ()))
                               for device, checks in scope.items())):
                return None
            observations, refs = saved_observations(db, row)
            pairs = {(item['device'], item['check']) for item in observations
                     if item['id'] in refs and collection_time(item['collected_at']) >= oldest}
            if pairs != {(device, check) for device, checks in scope.items() for check in checks}:
                continue
            candidate = {'goal':goal,'scope':scope}
            if plan and plan != candidate:
                return None
            plan = candidate
            origins.add(row['conversation'])
    if len(origins) >= 2:
        return dict(plan, supporting_conversations=len(origins))
    return None
