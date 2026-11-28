"""Local task continuity. Saved evidence never becomes a fresh read or a permission."""
from contextlib import closing
import json
import re

from server.approval_store import SINGLE_APPROVAL
from devices.diagnostic_intake import update_brief
from devices.device_registry import CHECK_LABELS, DEVICES, PING_TARGETS, PROBE_CHECKS, TUNNEL_CHECKS
from legacy.investigation_learning import LOOK_BACK
from legacy.learning_intent import forbids_inspection, read_method_question
from legacy.question_routing import target_devices
from devices.topology_observations import latest


RETURN = re.compile(r'(?:回到|接着|恢复|继续)(?:刚才|之前|前面|原来|原先|上次|原)(?:的)?(?:网络|连通性)?(?:排障|排查|检查|调查|任务|探测)')
RESULT = re.compile(r'通不通|通了?吗|都不通|单向|双向|结果|结论|说明|意味着|证明|矛盾|自洽')
PATH = re.compile(r'管理(?:地址|路径|网络)|隧道|tun16|ping|icmp', re.I)
OTHER = re.compile(r'router|路由器|交换机|三卡|六卡|物理机|云服务器|\b\d{1,3}(?:\.\d{1,3}){3}\b', re.I)
ROUTES = re.compile(r'(?:查|检查|核对|看|读).{0,12}(?:策略路由|路由表|路由规则|选路)')


def task_context(store, owner, conversation, interaction):
    """Use one server task as the focus; never search browser-supplied transcripts."""
    with closing(store.connect()) as db:
        row = db.execute(
            'SELECT * FROM workflow_tasks WHERE owner=? AND conversation=? AND scene=? AND mode_revision=? '
            'AND expires>? ORDER BY rowid DESC LIMIT 1',
            (owner, conversation, interaction['scene'], interaction['revision'], store.clock()-LOOK_BACK)).fetchone()
        if row is None:
            return None
        request = store.task_request(db, row['id'])
        scope = json.loads(row['scope'])
        related_scope = {d:set(checks) for d,checks in scope.items()}
        devices = set(scope)
        devices.update(PING_TARGETS[c] for checks in scope.values() for c in checks if c in PING_TARGETS)
        # A continuation records provenance only, without inheriting an approval.
        parent = db.execute("SELECT body FROM workflow_events WHERE task=? AND kind='context_continuation' LIMIT 1",
                            (row['id'],)).fetchone()
        if parent:
            source = db.execute('SELECT scope FROM workflow_tasks WHERE id=? AND owner=? AND conversation=? '
                                'AND scene=? AND mode_revision=?',
                (json.loads(parent['body'])['parent'],owner,conversation,interaction['scene'],interaction['revision'])).fetchone()
            if source:
                prior_scope = json.loads(source['scope'])
                devices.update(prior_scope)
                devices.update(PING_TARGETS[c] for checks in prior_scope.values() for c in checks if c in PING_TARGETS)
                for device, checks in prior_scope.items():
                    related_scope.setdefault(device,set()).update(checks)
        brief = request.get('brief') or update_brief({'goal':row['diagnostic_goal'], 'devices':sorted(devices)})
        value = {'id':row['id'], 'question':row['question'], 'brief':brief,
                 'devices':devices, 'status':row['status'], 'expires':row['expires']}
    # Latest reservations, including failures, shadow older successful readings.
    # Only typed projections are returned; credentials, arbitrary log text and model claims are excluded.
    value['records'] = [r for r in latest(store, owner, interaction['scene'], conversation)
                        if r['check'] in related_scope.get(r['device'],())]
    return value


def result_followup(question, topic, context=None):
    if (OTHER.search(question) or read_method_question(question)
            or re.search(r'现在|当前|此刻|重新|再查|再测|继续查|实测|假如|假设|如果|怎么查|如何查', question)):
        return False
    topical = bool(topic and topic['domain'] in ('topology', 'lab') or
                   topic is None and context and (PATH.search(question) or re.search(r'通不通|单向|双向',question)))
    reference = bool(RETURN.search(question) or re.search(r'刚才|上次|之前(?:查|测)|上一轮', question))
    return bool((reference and RESULT.search(question)) or
                topical and (re.match(r'^(?:所以|那|那么|这|结果)', question) and RESULT.search(question)
                             or re.search(r'为什么|为啥|原因', question) and PATH.search(question)))


def historical_result(context, question, records_selected=False):
    records = context['records']
    probes = bool(PATH.search(question) or re.search(r'通不通|通了?吗|都不通|单向|双向',question))
    checks = PROBE_CHECKS if probes else (
        {'policy_routes','routes'} if '路由' in question else {'services'} if re.search(r'服务|进程',question) else
        {'interfaces'} if re.search(r'接口|网口|VIP',question,re.I) else {'ha_config'} if '配置' in question else None)
    if checks is not None and not records_selected:
        records = [r for r in records if r['check'] in checks]
    named = set(target_devices(question))
    if named and not records_selected:
        records = [r for r in records if r['device'] in named or PING_TARGETS.get(r['check']) in named]
    if not records:
        return None
    lines, evidence = [], []
    for row in records[:12]:
        detail = '；'.join(row['fields'].values())
        label = DEVICES[row['device']]['label']
        if not row['source_ref'] or not detail:
            lines.append(label+'上次未取得'+CHECK_LABELS[row['check']]+'的可判读结果。')
            continue
        ident = 'OBS-'+str(len(evidence)+1)
        lines.append(label+' '+detail+' ['+ident+']')
        evidence.append(dict(id=ident, device=row['device'], check=row['check'], output=detail,
            ok=True, origin='device_read', freshness='historical', collected_at=row['collected_at'],
            task_id=row['task_id'], source_ref=row['source_ref'], conversation_id=row['conversation_id']))
    explanation = ''
    if re.search(r'为什么|为啥|原因|矛盾|说明|证明', question) and any(r['check'] in PROBE_CHECKS for r in records):
        if any('未查到可用路由' in r['fields'].get('icmp', '') for r in records):
            explanation = ('管理路径的检查停在路由查询阶段；仅凭这项结果还不能确定是配置缺失、策略选路还是查询异常。'
                           'tun16 与管理地址是不同路径，需要分别判断。下一步应核对未发包源设备的策略路由。\n\n')
        else:
            explanation = 'ICMP 应答只支持对应源、目标和采集时刻的可达性；未收到应答的具体原因尚未定位。\n\n'
    times = sorted({e['collected_at'] for e in evidence})
    stamp = ('采集时间（UTC）'+times[0][:19].replace('T',' ')+
             (' 至 '+times[-1][:19].replace('T',' ') if len(times)>1 else '')+'。\n\n') if times else ''
    return {'status':'answered', 'route':'diagnostic',
        'answer':('按最近一次探测，管理地址和 tun16 分别判断：' if probes else '最近一次相关检查取得的结果：')+'\n\n'+'\n\n'.join(lines)+'\n\n'+
                 explanation+stamp+'以上是历史观测，本轮未重新发包。实际业务收发仍待验证。',
        'refs':[e['id'] for e in evidence], 'evidence':evidence, 'sources':[], 'events':[],
        'evidence_scope':{'kind':'historical','current_reads':0},
        'diagnostic_context':{'task_id':context['id'], 'state':context['status'], 'approval_restored':False}}


def continuation_plan(context, question, topic):
    """A new passive request may use the prior intent, never its old authorization."""
    if (context is None or context['status'] not in ('completed','incomplete')
            or forbids_inspection(question) or read_method_question(question) or OTHER.search(question)
            or re.search(r'如果|假如|假设|取消|重启|修改|删除|顺便|同时|先别|不要|不用|别查', question)
            or not ROUTES.search(question) or target_devices(question)):
        return None
    topical = bool(topic and topic['domain'] in ('topology','lab'))
    if not (RETURN.search(question) or topical):
        return None
    probes = [r for r in context['records'] if r['check'] in PROBE_CHECKS]
    if not probes:
        return None
    failed = {r['device'] for r in probes if r['source_ref'] and
              re.search(r'应答 0/|未发包', r['fields'].get('icmp',''))}
    failures_only = bool(re.search(r'不通|失败|没通|无应答|没回包|未发包',question))
    sources = failed if failures_only else {r['device'] for r in probes}
    if not sources:
        return None
    return {'scope':{d:['policy_routes'] for d in sorted(sources)}, 'continues':True,
            'anchor':context['question'], 'brief':update_brief({'goal':'routing',
                'devices':sorted(sources), 'path':context['brief'].get('path')})}


def recall(store, owner, conversation, interaction, topic, question, context):
    if context is None:
        return None
    returning = RETURN.search(question)
    only_return = bool(returning and re.fullmatch(r'(?:请|帮我|麻烦|我想|我想要|\s)*',question[:returning.start()])
                       and re.fullmatch(r'[，,。！？?!\s]*(?:(?:继续|接着|吧|呢)[，,。！？?!\s]*)?',question[returning.end():]))
    if only_return and context['status'] in ('pending','approved','running') and context['expires']>store.clock():
        task = store.get(context['id'], owner)
        if task['status'] in ('pending','approved','running'):
            return {'status':'approval_required' if task['status']=='pending' else 'investigation_started',
                    'route':'diagnostic', 'task':task,
                    'answer':('已回到原排障任务，原检查范围仍待确认，本轮未执行设备检查。' if task['status']=='pending' else
                              '已回到原排障任务，请查看现有任务进度。'),
                    'diagnostic_context':{'task_id':task['id'],'approval_restored':False}}
    if result_followup(question, topic, context) or only_return:
        return historical_result(context, question)
    return None
