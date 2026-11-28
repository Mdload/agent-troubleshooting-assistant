"""Bounded server-side investigation context for one diagnostic mode generation."""
from contextlib import closing
import ipaddress
import json
import re

from server.approval_store import DIAGNOSTIC_DATA_USE, SINGLE_APPROVAL, TaskError
from devices.diagnostic_continuity import RETURN, result_followup
from devices.diagnostic_logs import PRIVATE_KEY_BEGIN, SENSITIVE
from devices.device_registry import PING_TARGETS, PROBE_CHECKS, TUNNEL_CHECKS, DEVICES, profile
from legacy.investigation_learning import LOOK_BACK, OTHER_TARGET
from legacy.learning_intent import direct_inspection_request, forbids_inspection
from legacy.question_routing import classify, target_devices
from devices.topology_observations import latest
from legacy.unified_chat import is_followup, needs_topology, new_general_topic


MAX_TASKS = 12
MAX_RECORDS = 12
TURN_LIMITS = {'goal': 1, 'correction': 8, 'turn': 16, 'detour': 1}
CORRECTION = re.compile(r'更正|纠正|说错了|写错了|我指的是|我的意思是|不是.{0,20}(?:而是|是指)|'
                        r'^(?:改查|改成|改为|换成|换查|只分析|只查|先不查|不要|别查)')
REFERENCE = re.compile(r'刚刚|刚才|上次|上一[次轮项步]|前一[次轮项步]|之前|先前|此前|当时|'
                       r'那[次轮项]|这[次轮项]|回顾|回到|最早|第一次')
EARLIEST = re.compile(r'最早|最初|第一次|首轮|最开始|一开始那次')
SUBJECT = re.compile(r'排障|排查|调查|检查|检测|测试|探测|回包|发包|应答|互访|互通|路由|隧道')
FRESH = re.compile(r'现在|此刻|当前.{0,10}(?:状态|正常|可用|通不通)|'
                   r'重新(?:查|测|读|检查|探测)|再(?:查|测|读|发包|跑一遍)|'
                   r'^(?:改查|换查|实测|先查|再查|去查|继续(?:查|测|检查|排查))')
MANAGEMENT = re.compile(r'管理(?:地址|路径|网络|口|\s*ip)|固定地址|未发包|没有发包', re.I)
TUNNEL = re.compile(r'隧道|tun16', re.I)
PROBE = re.compile(r'ping|icmp|互访|互通|探测|连通|单向|双向|通不通|通了?吗|应答|回包|发包|有回应|'
                   r'(?:几个|多少).{0,3}包|管理地址|隧道|tun16', re.I)
STANDALONE = re.compile(r'换个话题|另一个问题|^另外问|^(?:请|麻烦|能不能)?\s*'
                        r'(?:说说|讲讲|讲解|介绍|解释|说明|写|编写|生成|给我|做个).{2,}|'
                        r'是什么|什么意思|怎么写|叫什么|是哪里|在哪里|有何区别|一般原理')
DEPENDENT = re.compile(r'^(?:(?:好|行|嗯|对)[，,。\s]*)?(?:那|所以|那么|下一步|接下来)|'
                       r'(?:这|那)(?:件事|事|一环|一步|个结果|项结果)|'
                       r'^(?:请|说说|把|回顾一下)?\s*(?:刚才|刚刚|上次|上一[次轮项步]|当时|先前)|'
                       r'(?:^|[，,。]|^(?:把|仅|只|总结|区分)\s*|'
                       r'(?:我|你|咱们|我们)(?:就问|只问|想问)?(?:刚才|刚刚|上次|之前)?)'
                       r'(?:已(?:经)?|实际)(?:证实|核实|确认|看到|拿到|得到|看见|查到|测到|排除)')


class InvestigationChanged(TaskError):
    """A different check attempt has become the latest for this recap."""


def safe_excerpt(text, limit=360):
    if SENSITIVE.search(text) or PRIVATE_KEY_BEGIN.search(text):
        return None
    return text[:limit]


def matching_records(records, question):
    checks = set()
    if re.search(r'策略路由|路由表|路由规则|路由检查|选路', question) or (
            '路由' in question and not PROBE.search(question)):
        checks.update(('routes', 'policy_routes'))
    if PROBE.search(question):
        management, tunnel = MANAGEMENT.search(question), TUNNEL.search(question)
        checks.update(PING_TARGETS if management and not tunnel else
                      TUNNEL_CHECKS if tunnel and not management else PROBE_CHECKS)
    if re.search(r'接口|网口|VIP', question, re.I):
        checks.add('interfaces')
    if re.search(r'服务|进程', question):
        checks.add('services')
    if '日志' in question:
        checks.add('logs')
    if '配置' in question:
        checks.add('ha_config')
    named = set(target_devices(question))
    direction = re.search(r'(?:从)?(局端|远端).{0,8}(?:到|往|向)(局端|远端)', question)
    side = {'局端': 'local-', '远端': 'remote-'}
    return [r for r in records
            if (not checks or r['check'] in checks)
            and (not named or r['device'] in named or PING_TARGETS.get(r['check']) in named)
            and (not direction or r['device'].startswith(side[direction[1]]) and
                 (r['check'] not in PING_TARGETS or PING_TARGETS[r['check']].startswith(side[direction[2]])))]


def task_summary(row):
    status = row['status'] if row['approved_use'] == DIAGNOSTIC_DATA_USE or row['status'] == 'pending' else 'not_disclosed'
    return {'task_id': row['id'], 'request': safe_excerpt(row['question']), 'goal': row['diagnostic_goal'],
            'state': status}


def observations(records, data_use=DIAGNOSTIC_DATA_USE, offset=0):
    result = []
    for row in records:
        if row['data_use'] != data_use:
            continue
        good = bool(row['source_ref'])
        detail = safe_excerpt(row['summary'], 540) if good else '最近一次检查未取得可判读的观测'
        if not detail:
            continue
        result.append(dict(id='OBS-'+str(offset+len(result)+1), device=row['device'], check=row['check'],
            output=detail, ok=good, origin='device_read' if good else 'unconfirmed', freshness='historical',
            collected_at=row['collected_at'], checked_at=row['checked_at'], task_id=row['task_id'],
            source_ref=row['source_ref'] or row['attempt_ref'], conversation_id=row['conversation_id']))
        if good and row.get('measurement'):
            result[-1]['measurement']=row['measurement']
    return result


def device_scope_changes(question):
    """Read explicit directives in user clauses and ignore quoted or discussed directives."""
    positive=re.compile(r'(?:改为\s*)?(?:只|仅)(?:分析|看|讨论|回顾|解释|针对)|改为(?:分析|看|针对)')
    reset=re.compile(r'(?:取消|解除|清除)(?:(?:之前|此前|当前|本轮)的?)?(?:设备分析|设备|分析)?范围(?:限制|限定)|'
                     r'恢复(?:原|全部|所有)(?:设备)?(?:分析)?范围|不再(?:只|仅)(?:分析|看|针对)')
    prefix=re.compile(r'^(?:(?:更正(?:一下)?|纠正(?:一下)?|我的意思是|我(?:希望|想|要)|本轮|这次|现在|接下来|以后|请|先|把|我们)\s*)+')
    question=re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|`[^`\n]*`',' ',question)
    changes=[]
    for part in re.split(r'[，,。；;！!？?\n]|但是|然而|不过',question):
        if re.search(r'是什么意思|什么含义|什么叫|怎么理解|如何理解|这句话|这样的说法',part):
            continue
        clause=prefix.sub('',part.strip())
        if reset.match(clause):
            changes.append(None)
            continue
        match=positive.match(clause)
        if not match:continue
        fragment=re.split(r'不再|不要|留待|后续',clause[match.end():],maxsplit=1)[0]
        devices=set(target_devices(fragment))
        devices.update(device for device in DEVICES if re.search(
            r'(?<![\w-])'+re.escape(device)+r'(?![\w-])',fragment,re.ASCII|re.I))
        if devices:changes.append(sorted(devices))
    return changes


def analysis_device_focus(questions):
    """The latest explicit replacement or reset wins within this conversation."""
    focus=None
    for question in questions:
        changes=device_scope_changes(question)
        if changes:focus=changes[-1]
    return focus


def focused_records(records, devices):
    return [row for row in records if devices is None or row['device'] in devices]


def build(store, owner, conversation, interaction, question):
    """Take one scoped snapshot, and record only the new user's original request."""
    scope = (owner, conversation, interaction['scene'], interaction['revision'])
    since = store.clock()-LOOK_BACK
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if store.interaction_row(db, owner, conversation) != interaction:
            raise TaskError('mode_changed')
        task_sql = ('SELECT t.id,t.question,t.scope,t.status,t.diagnostic_goal,t.expires,p.approved_use '
                    'FROM workflow_tasks t JOIN workflow_data_permissions p ON p.task=t.id '
                    'WHERE t.owner=? AND t.conversation=? AND t.scene=? AND t.mode_revision=? '
                    'AND t.expires>? AND p.flow=? AND t.status NOT IN (\'cancelled\',\'expired\',\'rejected\')')
        task_args = scope+(since,SINGLE_APPROVAL,MAX_TASKS)
        tasks = db.execute(task_sql+' ORDER BY t.rowid DESC LIMIT ?', task_args).fetchall()
        previous = db.execute(
            'SELECT kind,question FROM workflow_investigation_turns WHERE owner=? AND conversation=? '
            'AND scene=? AND revision=? AND at>=? ORDER BY rowid DESC',
            scope+(since,)).fetchall()
        effective_devices=analysis_device_focus([row['question'] for row in reversed(previous)]+[question])
        detached = bool(previous and previous[0]['kind'] == 'detour')
        explicit = bool(RETURN.search(question) or SUBJECT.search(question) and (
            REFERENCE.search(question) or EARLIEST.search(question) or re.search(r'(?:这|那)(?:个|项|轮|次)',question)))
        exact_earliest = explicit and bool(EARLIEST.search(question))
        completed = bool(SUBJECT.search(question) and re.search(r'做完|测完|查完|拿到|得到了|发现了', question))
        contextual = bool(explicit or not detached and (DEPENDENT.search(question) or completed or
                          is_followup(question) and (SUBJECT.search(question) or PROBE.search(question) or
                            re.match(r'^(?:它|具体|继续|这个(?:怎么|怎样|为什么)|为什么[呢？?。]*$)',question))))
        scope_correction=bool(device_scope_changes(question))
        correction = bool((CORRECTION.search(question) or scope_correction) and
                          (contextual or tasks or previous or needs_topology(question)))
        topical = bool(needs_topology(question) or direct_inspection_request(question))
        anchored = bool(explicit or completed or correction and SUBJECT.search(question))
        general = (new_general_topic(question) and not anchored or
                   not anchored and not contextual and re.search(r'原理|一般机制|通用概念',question) or
                   not (topical or contextual or SUBJECT.search(question)) and STANDALONE.search(question))
        # An ambiguous standalone request can use task intent. A historical
        # projection requires a reference to prior work or an actual result.
        active = bool(tasks and (not detached or explicit))
        current_request = not forbids_inspection(question) and (
            FRESH.search(question) or direct_inspection_request(question) or
            not explicit and not result_followup(question,None,tasks) and classify(question)['intent']=='live')
        belongs = not general and not OTHER_TARGET.search(question) and (
            topical or contextual or correction or active and not current_request)
        recap = bool(active and belongs and not current_request and
                     any(row['status'] in ('completed','incomplete') for row in tasks))
        records = []
        by_task = {row['id']: row for row in tasks}
        if recap:
            if exact_earliest:
                earliest = db.execute(task_sql+" AND t.status IN ('completed','incomplete') ORDER BY t.rowid LIMIT ?",
                                      task_args).fetchall()
                for row in earliest:
                    scoped = latest(store, owner, scope[2], conversation, row['id'], snapshot=db, revision=scope[3])
                    if focused_records(matching_records(scoped, question),effective_devices):
                        by_task[row['id']] = row
                        records = scoped
                        break
            else:
                records = latest(store, owner, scope[2], conversation, snapshot=db, revision=scope[3])
            records = focused_records(matching_records(records, question),effective_devices)[:MAX_RECORDS]
        excerpt = safe_excerpt(question)
        if excerpt:
            kind = ('detour' if not belongs else 'correction' if correction else
                    'goal' if not any(row['kind']=='goal' for row in previous) and not tasks and topical else 'turn')
            # Expire old excerpts and keep separate bounds so recent dialogue cannot displace a correction.
            where = 'owner=? AND conversation=? AND scene=? AND revision=?'
            db.execute('DELETE FROM workflow_investigation_turns WHERE '+where+' AND at<?', scope+(since,))
            db.execute('INSERT INTO workflow_investigation_turns VALUES (?,?,?,?,?,?,?)',
                       scope+(kind,excerpt,store.clock()))
            db.execute('DELETE FROM workflow_investigation_turns WHERE '+where+' AND kind=? AND rowid NOT IN ('
                       'SELECT rowid FROM workflow_investigation_turns WHERE '+where+' AND kind=? '
                       'ORDER BY rowid DESC LIMIT ?)', scope+(kind,)+scope+(kind,TURN_LIMITS[kind]))
    if not belongs or not (tasks or previous):
        return None
    initial = next((row['question'] for row in previous if row['kind']=='goal'), None)
    if initial is None and tasks:
        initial = safe_excerpt(tasks[-1]['question'])
    corrections = [row['question'] for row in reversed(previous) if row['kind']=='correction']
    if correction and excerpt:
        corrections.append(excerpt)
    focus = tasks[0] if tasks else None
    directory=[]
    for row in by_task.values():
        if row['status'] not in ('completed','incomplete'):continue
        devices={device:checks for device,checks in json.loads(row['scope']).items()
                 if effective_devices is None or device in effective_devices}
        if devices:
            directory.append({'task_id':row['id'],'checks':sorted({check for values in devices.values() for check in values}),
                              'devices':devices})
    model_context = {'original_goal':initial, 'corrections':corrections[-8:],
        'focus':task_summary(focus) if focus else None,
        'related_tasks':[task_summary(row) for row in by_task.values()], 'latest_request':excerpt,
        'effective_devices':effective_devices}
    history = [{'role':'user','content':row['question']} for row in reversed(previous[:8])
               if row['kind'] not in ('detour','goal','correction')]
    return {'recap':recap, 'explicit':explicit, 'exact_earliest':exact_earliest, 'directory':directory,
            'records':records, 'model':model_context, 'history':history,'effective_devices':effective_devices}


def with_evidence(model_context, evidence):
    next_checks = [{'device':row['device'],'check':'policy_routes','basis_ref':row['id']} for row in evidence
                   if row['check'] in PROBE_CHECKS and row['ok'] and re.search(r'未发包|应答 0/',row['output'])]
    registry={device:{'origin':'static_expected_registry','expected_vips':dict(profile(device)['ha_vips']),
                      'croc_unit':profile(device)['croc_unit']}
              for device in sorted({row['device'] for row in evidence if row['device'] in DEVICES})}
    return dict(model_context, suggested_checks=next_checks, registered_ha_resources=registry,
                unverified=['当前状态','实际业务收发','根因'] if evidence else [])


def typed_recap(evidence, local=()):
    """Render measured facts for the recaps covered by the typed host projections."""
    checks={row['check'] for row in evidence}
    resources=bool(checks and checks<={'services','interfaces'})
    mixed=bool(local and checks.intersection(PROBE_CHECKS)
               and checks<=(PROBE_CHECKS|{'routes','policy_routes'}))
    if not (resources or mixed) or not any(row['ok'] for row in evidence):
        return None

    lines=['历史检查结果（本轮未重新读取）']
    for row in evidence:
        if local and row is local[0]:
            lines.append('本地历史检查')
        label=DEVICES[row['device']]['label']
        text=label+' '+row['output']
        if row['ok']:
            if row['check']=='interfaces':
                added=[]
                for interface, expected in profile(row['device'])['ha_vips'].items():
                    for field in row['output'].split('；'):
                        parts=field.split(maxsplit=2)
                        if len(parts)!=3 or parts[0]!=interface:continue
                        try:
                            addresses={str(ipaddress.IPv4Interface(token).ip)
                                       for token in parts[2].split('、')}
                        except ValueError:
                            continue
                        if expected in addresses:added.append(interface+' 观测到登记 VIP '+expected)
                if added:text+='。'+'，'.join(added)
            measurement=row.get('measurement') or {}
            route=measurement.get('route',{})
            details=[name+' '+route[key] for key,name in
                     (('source','源地址'),('interface','出口'),('gateway','下一跳')) if key in route]
            if details:text+='。探测前选路：'+'，'.join(details)
            date=row['collected_at'][:19].replace('T',' ')
            text+='（'+date+' UTC） ['+row['id']+']'
        lines.append(text)
    if resources:
        lines.append('证据缺口：VIP 的地址归属以当时采集为准。接口 UP 和配置地址不能确认对端隧道已建立。'
                     '要判断实际接管和业务是否可用，还需核对运行时主备角色及两端实际收发。')
    else:
        lines.append('证据缺口：探测结果仅适用于当时这条源到目的路径。路由表片段不能证明实际往返路径。'
                     '如需定位无应答，可对照同一源和目的的正向及回程路由。')
    return '\n\n'.join(lines)


def recall_history(context, action):
    """Select from the current turn's bounded directory and winning attempts."""
    if set(action)-{'action','task_id','checks','devices'}:
        raise ValueError('invalid_history_selection')
    task = action.get('task_id')
    tasks = {row['task_id']:row['checks'] for row in context['directory']}
    if not isinstance(task,str) or task!='latest' and task not in tasks:
        raise ValueError('invalid_history_selection')
    checks = action.get('checks')
    allowed = {check for values in tasks.values() for check in values} if task=='latest' else set(tasks[task])
    if 'checks' in action and (not isinstance(checks,list) or not checks or len(checks)>MAX_RECORDS
                               or any(not isinstance(check,str) or check not in allowed for check in checks)
                               or len(checks)!=len(set(checks))):
        raise ValueError('invalid_history_selection')
    scopes={row['task_id']:row.get('devices',{}) for row in context['directory']}
    allowed_devices={}
    for ident, scope in scopes.items():
        if task=='latest' or ident==task:
            for device, device_checks in scope.items():
                allowed_devices.setdefault(device,set()).update(device_checks)
    devices=action.get('devices')
    pairs=None
    if isinstance(devices,dict):
        # A model can echo the declared directory shape. Validate each pair before using it.
        pairs=set()
        if not devices or len(devices)>len(DEVICES):raise ValueError('invalid_history_selection')
        for device, selected_checks in devices.items():
            if (device not in allowed_devices or not isinstance(selected_checks,list) or not selected_checks
                    or len(selected_checks)>MAX_RECORDS or any(not isinstance(check,str) or
                        check not in allowed_devices[device] for check in selected_checks)
                    or len(set(selected_checks))!=len(selected_checks)):
                raise ValueError('invalid_history_selection')
            pairs.update((device,check) for check in selected_checks)
    elif 'devices' in action and (not isinstance(devices,list) or not devices or len(devices)>len(DEVICES)
            or any(not isinstance(device,str) or device not in allowed_devices for device in devices)
            or len(devices)!=len(set(devices))):
        raise ValueError('invalid_history_selection')
    # Selecting an ID narrows the winners. It never revives superseded values.
    return [row for row in context['records'] if row['check'] in tasks.get(row['task_id'],())
            and (task=='latest' or row['task_id']==task)
            and (not scopes.get(row['task_id']) or row['check'] in scopes[row['task_id']].get(row['device'],()))
            and (devices is None or row['device'] in devices)
            and (pairs is None or (row['device'],row['check']) in pairs)
            and (checks is None or row['check'] in checks)]


def assert_active(store, owner, conversation, interaction, context=None, question=None):
    """Recheck source permissions immediately before/after each planner call."""
    with closing(store.connect()) as db:
        db.execute('BEGIN')
        if store.interaction_row(db, owner, conversation) != interaction:
            raise TaskError('mode_changed')
        if context is None:
            return
        if not context['exact_earliest']:
            current = latest(store,owner,interaction['scene'],conversation,snapshot=db,revision=interaction['revision'])
            if focused_records(matching_records(current,question),context.get('effective_devices'))[:MAX_RECORDS] != context['records']:
                raise InvestigationChanged('investigation_context_changed')
            return
        bindings = {(row['task_id'],row['data_use']) for row in context['records']}
        for task, data_use in bindings:
            match = db.execute('SELECT 1 FROM workflow_tasks t JOIN workflow_data_permissions p ON p.task=t.id '
                'WHERE t.id=? AND t.owner=? AND t.conversation=? AND t.scene=? AND t.mode_revision=? '
                'AND p.flow=? AND p.approved_use=? AND t.status IN (\'completed\',\'incomplete\',\'cancelled\')',
                (task,owner,conversation,interaction['scene'],interaction['revision'],SINGLE_APPROVAL,
                 data_use)).fetchone()
            if match is None:
                raise TaskError('data_scope_required')
