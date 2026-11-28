"""Local, owner-scoped view of the last actual check of each topology fact."""
from contextlib import closing
from datetime import datetime
import ipaddress
import json
import re

from server.approval_store import SINGLE_APPROVAL, LOCAL_READ_USE, DIAGNOSTIC_DATA_USE
from devices.device_registry import DEVICES, CHECK_LABELS, PING_TARGETS, TUNNEL_CHECKS, PROBE_CHECKS, profile, tunnel_endpoints
from devices.diagnostic_probes import KEEPALIVED_FIELDS, ping_counts, ping_route
from legacy.evidence_scope import collection_time
from legacy.ha_croc_case import croc_state
from devices.link_inspection import interface_ipv4_samples
from legacy.question_routing import target_devices
from devices.measurement_evidence import project_measurement


PORT = re.compile(r'(?<![a-z0-9_])(?:tun|eth|ppp)\d+(?![a-z0-9_])', re.I)
RECORD = re.compile(r'最近(?:一次)?(?:的)?(?:现场|记录|实测|观测|排障)|最近查到|上次记录|调查记录|现场记录|已知拓扑|已观测')
PATH_QUESTION = re.compile(r'(?:那么|那|刚才|所以)?(?:两端的?)?'
                           r'(?P<path>真实地址|真实IP|固定地址|固定IP|物理地址|物理网口|以太网口|MAC地址|管理地址|管理IP|管理口|tun16|隧道|业务收发|业务)'
                           r'(?:之间)?(?:能否互通|通不通|互通吗|通吗|呢)',re.I)
INVENTORY = re.compile(r'(?:(?:请|帮我|介绍一下|说一下|说说|看看|总结一下|我们|这个|实验室|的|\s))*'
                       r'(?:拓扑|双机热备拓扑)(?:里|中|的|上)?(?:有哪些设备|有哪些节点|结构|是什么样的|是怎样的|怎么连接的|概况|情况)')
WINDOW = 30*86400


def address_question(text):
    ports=PORT.findall(text)
    if len(ports)==1 and re.search(r'地址|IP\b',text,re.I):
        return ports[0].lower()
    return None


def operational_question(question):
    """Classify a concrete topology field without treating a mechanism question as a live read."""
    if re.search(r'为什么|如何|怎么(?!样)|原理|配置|区别|代表|如果|一般|历史|记录|上次|刚才|不要|别|重启|修改|顺便|同时|以及|另外|网段|路由器',question):
        return None
    if not re.search(r'多少|哪些|什么地址|什么IP|几个|运行[吗没]|跑着吗|启动了吗|状态[？?]|什么状态|分配了什么|拿到什么',question,re.I):
        return None
    if re.search(r'(?:服务|keepalived|croc)',question,re.I) and not re.search(r'接口|网口|地址|路由',question):
        return 'services'
    if re.search(r'网口|接口|(?:eth|ppp|tun)\d+',question,re.I) and not re.search(r'业务|端口|互通|ping',question,re.I):
        return 'interfaces'
    return None


def interface_fields(device, output):
    samples=interface_ipv4_samples(output,profile(device)['interfaces'],preserve_prefix=True)
    fields={}
    for port,item in samples.items():
        if item['state']=='unconfirmed':continue
        ips=item['ipv4']
        suffix=' '+'、'.join(sorted(ips)) if ips else ('，未观测到 IPv4 地址' if ips is not None else '，地址未确认')
        fields[port]=port+' '+item['state']+(suffix if item['state']!='absent' else '（未发现此接口）')
    return fields


def project(device, check, output):
    """Only typed, allowlisted values are eligible; raw logs and model answers stay out."""
    fields={}
    if check=='interfaces':
        fields=interface_fields(device,output)
    elif check=='services':
        for unit in profile(device)['units']:
            state=croc_state(output,unit)
            if state!='unknown':
                name='Croc' if unit==profile(device)['croc_unit'] else 'Keepalived' if unit=='keepalived.service' else unit.split('.service')[0]
                fields[unit]=name+(' 正在运行' if state=='running' else ' 没有运行')
    elif check in PROBE_CHECKS:
        target=tunnel_endpoints(device,check)[1] if check in TUNNEL_CHECKS else DEVICES[PING_TARGETS[check]]['address']
        label=('→ 对端 tun16 ' if check in TUNNEL_CHECKS else '→ '+DEVICES[PING_TARGETS[check]]['label']+'管理 IP ')+target
        counts=ping_counts(output)
        if counts and (check not in TUNNEL_CHECKS or ping_route(output).get('interface')=='tun16'):
            fields['icmp']=label+' 应答 %d/%d'%(counts[1],counts[0])
        elif output.strip()=='ICMP_ROUTE_UNAVAILABLE=1':
            fields['icmp']=label+' 未查到可用路由，未发包'
        elif check in TUNNEL_CHECKS and output.strip()=='ICMP_TUNNEL_PATH_MISMATCH=1':
            fields['icmp']=label+' 本端地址或出口不匹配，未发包'
    elif check in ('routes','policy_routes'):
        # Preserve each default's table and gateway. Rules/routes alone do not prove traffic.
        for line in output.splitlines():
            match=re.fullmatch(r'default(?: via ([0-9.]+))? dev ([\w.:-]{1,32})(?:\s+(.*))?',line, re.ASCII)
            if match:
                try:gateway=str(ipaddress.IPv4Address(match[1])) if match[1] else ''
                except ValueError:continue
                table=re.search(r'(?:^| )table ([a-zA-Z0-9_-]{1,32})(?: |$)',match[3] or '')
                value='表 '+(table[1] if table else 'main')+' 默认路由经 '+match[2]+('，下一跳 '+gateway if gateway else '')
                fields[value]=value
    elif check=='ha_config':
        for line in output.splitlines():
            key,sep,value=line.partition('=')
            if sep and key in KEEPALIVED_FIELDS and re.fullmatch(r'[0-9A-Za-z_.:-]{1,32}',value):
                fields[line]=KEEPALIVED_FIELDS[key]+' '+value
    elif check=='logs':
        count=re.search(r'^LOG_RECORDS_RETURNED=(\d{1,3})$',output,re.M)
        if count:fields['count']='该次返回 '+count[1]+' 条日志'
    return fields


def latest(store, owner, scene, conversation=None, task=None, snapshot=None, revision=None):
    # A failed latest attempt shadows an earlier success. Each winning reservation
    # belongs to the caller's still accessible mode and an explicitly approved scope.
    filters=["t.owner=?","t.scene=?","t.status IN ('completed','incomplete','cancelled')",
             "p.flow=?","pr.updated>=?"]
    args=[owner,scene,SINGLE_APPROVAL,store.clock()-WINDOW]
    for column,value in [('t.conversation',conversation),('t.id',task),('t.mode_revision',revision)]:
        if value is not None:filters.append(column+'=?');args.append(value)
    query=("SELECT MAX(r.rowid) AS read_id FROM workflow_reads r JOIN workflow_tasks t ON t.id=r.task "
           "JOIN workflow_modes m ON m.owner=t.owner AND m.conversation=t.conversation AND m.scene=t.scene "
           "AND m.revision=t.mode_revision JOIN workflow_data_permissions p ON p.task=t.id "
           "JOIN workflow_progress pr ON pr.task=t.id WHERE "+' AND '.join(filters)+' GROUP BY r.device,r.check_name')
    sql=("SELECT r.device,r.check_name,t.id,t.conversation,t.approved_scope,t.result,pr.updated,p.approved_use "
         "FROM workflow_reads r JOIN workflow_tasks t ON t.id=r.task JOIN workflow_progress pr ON pr.task=t.id "
         "JOIN workflow_data_permissions p ON p.task=t.id "
         "WHERE r.rowid IN ("+query+") ORDER BY pr.updated DESC,r.rowid DESC")
    if snapshot is None:
        with closing(store.connect()) as db:
            rows=db.execute(sql,args).fetchall()
    else:
        rows=snapshot.execute(sql,args).fetchall()
    records=[]
    for row in rows:
        device,check=row['device'],row['check_name']
        # A withdrawn newest check masks older values across all readers.
        if row['approved_use'] not in (LOCAL_READ_USE,DIAGNOSTIC_DATA_USE):continue
        if device not in DEVICES or check not in CHECK_LABELS or check not in json.loads(row['approved_scope'] or '{}').get(device,[]):continue
        items=[o for o in json.loads(row['result'] or '{}').get('evidence',[]) if o.get('device')==device and o.get('check')==check]
        item=items[0] if len(items)==1 else {}
        stamp=item.get('collected_at')
        good=(item.get('ok') is True and item.get('origin')=='device_read' and item.get('freshness')=='current'
              and item.get('clock_source')=='collector_utc' and collection_time(stamp)
              and re.fullmatch(r'OBS-[1-9]\d*',str(item.get('id',''))) and isinstance(item.get('output'),str))
        fields=project(device,check,item['output']) if good else {}
        record={'device':device,'check':check,'task_id':row['id'],'conversation_id':row['conversation'],
                        'source_ref':item['id'] if fields else None,'fields':fields,'collected_at':stamp if good else None,
                        'checked_at':datetime.utcfromtimestamp(row['updated']).isoformat()+'Z',
                        'summary':'；'.join(fields.values()) if fields else '最近一次检查未取得可判读的观测'}
        measurement=project_measurement(device,check,item['output']) if good and fields else None
        if measurement is not None:record['measurement']=measurement
        if snapshot is not None:
            record['data_use']=row['approved_use']
            record['attempt_ref']=item.get('id') if re.fullmatch(r'OBS-[1-9]\d*',str(item.get('id',''))) else None
        records.append(record)
    return records


def related_fact(store,owner,scene,conversation,question):
    """Ground an explanation in one matching observation, locally after model generation."""
    devices=target_devices(question)
    if len(devices)!=1 or re.search(r'只(?:按|看|用)|不要|不用|假如|假设|举例|公共资料|别引用',question):
        return None
    ports=PORT.findall(question)
    units=[unit for unit in ('croc','keepalived') if unit in question.lower()]
    if len(ports)==1:
        check,key='interfaces',ports[0].lower()
    elif len(units)==1 and re.search(r'进程|服务',question):
        check='services'
        key=profile(devices[0])['croc_unit'] if units[0]=='croc' else 'keepalived.service'
    elif re.search(r'路由表|策略路由|默认路由',question):
        check,key='policy_routes',None
    else:
        return None
    for row in latest(store,owner,scene,conversation):
        if row['device']!=devices[0] or row['check']!=check or not row['source_ref']:continue
        detail=row['fields'].get(key) if key else next(iter(row['fields'].values()),None)
        if detail:
            fact={k:row[k] for k in ('device','check','task_id','conversation_id','source_ref','collected_at')}
            return dict(fact,summary=DEVICES[row['device']]['label']+' '+detail)
    return None


def answer(store,owner,conversation,interaction,topic,question,cross_conversation):
    q=question.strip(' 。？！?!')
    follow=PATH_QUESTION.fullmatch(q)
    path=follow['path'].lower() if follow else ''
    physical=path in ('物理网口','以太网口','mac地址')
    fixed=path in ('真实地址','真实ip','固定地址','固定ip','物理地址')
    inventory=INVENTORY.fullmatch(q)
    if not follow and not inventory and (not RECORD.search(q) or re.search(r'现在|当前|重新|再查|再测|顺便|删除|清空|原理|假设|为什么',q)):
        return None
    if not follow and not re.search(r'拓扑|接口|网口|地址|服务|进程|路由|探测|连通|互通|隧道|日志|配置|ping|icmp|tun\d|ppp\d|eth\d|croc|keepalived',q,re.I):
        return None
    task=None
    if follow:
        last=store.last_probe(owner,conversation,interaction['revision'])
        intake=(topic or {}).get('intake') or {}
        if last and (not topic or topic['domain'] in ('topology','lab') and intake.get('task_id',last['id'])==last['id']):
            task=last['id']
        elif not physical and not fixed:
            return None
    rows=latest(store,owner,interaction['scene'],conversation if follow or not cross_conversation else None,task)
    targets=target_devices(q)
    port=address_question(q)
    lines,evidence=[],[]
    if follow:
        allowed=(TUNNEL_CHECKS if path in ('隧道','tun16','业务','业务收发') else PING_TARGETS)
        rows=[r for r in rows if task and r['check'] in allowed]
    elif inventory:
        rows.sort(key=lambda r:0 if r['check'] in PROBE_CHECKS else 1 if r['check']=='services' else 2)
    elif port:rows=[r for r in rows if r['check']=='interfaces']
    elif re.search(r'路由',q):rows=[r for r in rows if r['check'] in ('routes','policy_routes')]
    elif re.search(r'接口|网口|地址',q):rows=[r for r in rows if r['check']=='interfaces']
    elif re.search(r'服务|进程|croc|keepalived',q,re.I):rows=[r for r in rows if r['check']=='services']
    elif re.search(r'日志',q):rows=[r for r in rows if r['check']=='logs']
    elif re.search(r'配置',q):rows=[r for r in rows if r['check']=='ha_config']
    elif re.search(r'隧道|ping|icmp|探测|连通|互通',q,re.I):rows=[r for r in rows if r['check'] in PROBE_CHECKS]
    for row in rows:
        if targets and row['device'] not in targets:continue
        if not targets and any(side in q for side in ('远端','局端')) and profile(row['device'])['side_label'] not in q:continue
        ident='OBS-'+str(len(evidence)+1)
        detail=row['fields'].get(port) if port else row['summary']
        if inventory and row['check']=='interfaces':
            keys=(profile(row['device'])['management_interface'],'tun16')
            detail='，'.join(row['fields'][k] for k in keys if k in row['fields'])
        if row['check']=='services':
            unit=next((u for u in ('keepalived','croc') if u in q.lower()),None)
            if unit:detail='，'.join(v for k,v in row['fields'].items() if k.lower().startswith(unit))
        label=DEVICES[row['device']]['label']
        if not row['source_ref'] or not detail:
            lines.append(label+'上次未取得'+(port or CHECK_LABELS[row['check']])+'的有效结果。')
            continue
        lines.append(label+' '+detail+' ['+ident+']')
        evidence.append(dict(id=ident,device=row['device'],check=row['check'],output=detail,ok=True,origin='device_read',
                             freshness='historical',collected_at=row['collected_at'],task_id=row['task_id'],
                             source_ref=row['source_ref'],conversation_id=row['conversation_id']))
        if len(lines)>=8:break
    if inventory:
        lines.insert(0,'登记的双机热备拓扑为局端一主一备、远端一主一备。'+
                     ('最近的现场观测如下。' if evidence else '目前没有可引用的现场观测。'))
    if physical:
        lines.insert(0,'两端物理网口之间是否能通，上一次检查没有直接验证。')
        lines.append('要测以太网口，请指定两端接口或 IP；如果你指 MAC，需要核对二层邻接。')
    elif fixed:
        lines.insert(0,'按前面的探测，先看两台设备固定 IP 的上次结果。')
        if any(r['device'].startswith('remote-') or PING_TARGETS.get(r['check'],'').startswith('remote-') for r in rows):
            lines.append('远端 10.7 地址属于 tun7 管理隧道；有线接口的实际 IP 需按对应接口核对。')
    elif path in ('业务','业务收发'):
        lines.insert(0,'应用业务收发还没有端点证据。')
    elif evidence and not inventory:
        lines[0]='最近一次采集：'+lines[0]
    if not lines:lines=['最近的排障记录中未取得这项现场观测。']
    if evidence:
        times=sorted({x['collected_at'] for x in evidence})
        lines.append('采集时间（UTC）'+times[0][:19].replace('T',' ')+(' 至 '+times[-1][:19].replace('T',' ') if len(times)>1 else '')+'。')
    return {'status':'answered','route':'diagnostic','answer':'\n'.join(lines),'refs':[x['id'] for x in evidence],
            'evidence':evidence,'sources':[],'events':[],'evidence_scope':{'kind':'historical','current_reads':0}}
