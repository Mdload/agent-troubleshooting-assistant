"""Readable records from fixed tools; never reconstruct the model's hidden reasoning."""
import math
import re
from device_registry import DEVICES,allows_check,profile,EXTRA_CHECK_LABELS,PROBE_CHECKS
from device_tools import commands_for
from diagnostic_probes import ping_counts
from diagnostic_logs import SENSITIVE
from diagnostic_report import check_purpose
from ha_croc_case import croc_state,vip_state
from evidence_scope import CHECK_LABELS,valid_collection_time

CHECK_WORDS={
    'services':('查看主备和业务程序有没有运行','检查 Keepalived 和 Croc 等程序是否已经启动。'),
    'interfaces':('看看这台设备有没有接管业务地址','查看 VIP（主备共用的业务地址）是否在这台设备上。'),
    'routes':('查看业务数据往哪里走','检查发往业务网段的数据会走哪个接口。'),
    'logs':('查看最近的启动、停止和报错记录','翻看主备和业务程序最近的日志，找异常发生的线索。'),
    **{check:(label,label) for check,label in EXTRA_CHECK_LABELS.items()}}
ERROR_LABELS={'timeout':'这次检查超时了，等了较久仍没返回，暂时不知道结果。',
    'tool_unavailable':'暂时无法执行这项检查，还没有得到结果。',
    'transport_unavailable':'这次没能连接设备，暂时查不到结果。','ssh_failed':'这次没能连接设备，暂时查不到结果。',
    'identity_unconfirmed':'还不能确认连接的是不是这台设备，检查已停止。',
    'target_hostname_mismatch':'连接到的设备名称与预期不符，检查已停止。',
    'target_user_mismatch':'登录到的身份与预期不符，检查已停止。',
    'target_address_mismatch':'连接到的设备地址与预期不符，检查已停止。',
    'check_failed':'这项检查没有完成，还不能判断设备是否有问题。',
    'extended_check_unavailable':'设备没有返回可判读的检查结果，可能是命令不可用或本次执行失败。',
    'output_limit':'返回内容过多，这次没有取得完整结果。',
    'log_projection_failed':'暂时没能整理出日志内容，还不能判断发生了什么。'}

def safe_text(value,limit=12000):
    if not isinstance(value,str):return ''
    lines=['[敏感信息已隐藏]' if SENSITIVE.search(line) else line for line in value.splitlines()]
    text='\n'.join(lines)
    text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]','',text)
    return text[:limit]+('\n[展示已截短，原记录保留]' if len(text)>limit else '')

def check_request(device,check):
    if not allows_check(device,check):raise ValueError('Unregistered check')
    commands=commands_for(device,check)
    return {'device':device,'check':check,'label':CHECK_LABELS[check],
            'title':CHECK_WORDS[check][0],'action_text':CHECK_WORDS[check][1],
            'command':'\n'.join(commands),'purpose':CHECK_WORDS[check][1],
            'command_note':'以下保留程序实际请求的命令；检查中途失败时，命令可能没有全部执行。'}

def result_summary(item, question=''):
    if item.get('ok') is not True:
        return ERROR_LABELS.get(item.get('error'),'这项检查没有完成，暂时不知道结果。')
    output=item.get('output','') if isinstance(item.get('output'),str) else ''
    check=item['check'];p=profile(item['device'])
    if check=='services':
        state=croc_state(output,p['croc_unit'])
        keep=croc_state(output,'keepalived.service')
        prefix={'running':'负责主备切换的 Keepalived 正在运行。','stopped':'负责主备切换的 Keepalived 没有运行。','unknown':''}[keep]
        return prefix+{'running':'Croc 正在运行。是否已经能正常传输数据，还要继续检查。',
            'stopped':'Croc 没有运行。还不知道它为什么停下；如果这台正在待机，停止也可能是正常的。',
            'unknown':'返回的信息还不足以看出 Croc 有没有正常运行。'}[state]
    if check=='interfaces':
        from link_inspection import ppp_summary
        summary = ppp_summary(output, p['interfaces'], question)
        if summary:
            return summary
        state=vip_state(output,p['ha_vips'])
        return {'owner':'看到了这组全部 VIP。这台设备有业务地址，但还需要确认另一台有没有同时使用这些地址。',
            'non_owner':'没有看到这组 VIP，也就是这台没有使用这些业务地址。它是不是在待机，还要结合另一台的情况确认。',
            'partial':'只看到部分 VIP，这组业务地址没有全部出现在这台设备上。还需要核对另一台。',
            'unknown':'这次返回的信息不完整，还不能确认业务地址在哪台设备上。'}[state]
    if check in PROBE_CHECKS:
        if output.strip()=='ICMP_TUNNEL_PATH_MISMATCH=1':
            return '本机的 tun16 地址或出口与登记路径不符，本次未发包。'
        if output.strip()=='ICMP_ROUTE_UNAVAILABLE=1':
            return '路由查询未返回可用路径，本次未发包。'
        counts=ping_counts(output)
        return ('发送 %d 个 ICMP 探测，收到 %d 个应答。'%counts if counts
                else '未取得可判读的 ICMP 探测统计。')
    if check=='ha_config':return ('配置文件无法读取。' if 'CONFIG_UNREADABLE=1' in output
                                  else '只展示配置文件中的许可字段，生效配置需结合运行状态核对。')
    if check in ('routes','policy_routes'):return '已经查到这台设备的路由信息。实际流向需要结合源、目标和策略匹配判断。'
    match=re.search(r'^LOG_RECORDS_RETURNED=(\d+)\s*$',output,re.M)
    count=int(match.group(1)) if match else None
    if count==0:return '这次没有找到可用的日志记录。这并不能说明之前没有出过问题。'
    return ('找到了%d条最近的日志。'%count if count is not None else '已经取到最近的日志。')+'可以用来追查发生过什么，不能单凭旧日志判断设备现在是否正常。'

def readable_steps(items):
    result=[]
    for saved in items if isinstance(items,list) else []:
        if not isinstance(saved,dict) or not allows_check(saved.get('device'),saved.get('check')):continue
        step=dict(saved);title,action=CHECK_WORDS[step['check']]
        step.update(title=title,action_text=action,purpose=action)
        item=dict(step,ok=step.get('status')=='succeeded')
        if item['ok']:step['summary']=result_summary(item)
        else:
            old=step.get('summary','')
            step['summary']=(ERROR_LABELS['timeout'] if '超时' in old else
                ERROR_LABELS['ssh_failed'] if '连接' in old else '这项检查没有完成，暂时不知道结果。')
        result.append(step)
        if len(result)==12:break
    return result

def observation_step(item):
    if not isinstance(item,dict) or not allows_check(item.get('device'),item.get('check')):
        raise ValueError('Invalid observation')
    if not isinstance(item.get('id'),str) or not re.fullmatch(r'OBS-[1-9][0-9]?',item['id']):
        raise ValueError('Invalid observation id')
    result=check_request(item['device'],item['check'])
    result.update(id=item['id'],status='succeeded' if item.get('ok') is True else 'failed',
        summary=result_summary(item),output=safe_text(item.get('output')) if item.get('ok') is True else '',
        origin=item.get('origin') if item.get('origin') in ('device_read','synthetic') else 'unconfirmed',
        freshness=item.get('freshness') if item.get('freshness') in ('current','historical','synthetic','stale') else 'unconfirmed',
        collected_at=item.get('collected_at') if valid_collection_time(item.get('collected_at')) else None)
    value=item.get('host_duration_ms',item.get('duration_ms'))
    if type(value) in (int,float) and math.isfinite(value) and value>=0:result['duration_ms']=round(value,3)
    return result

def observation_steps(items):
    result=[];seen=set()
    for item in items if isinstance(items,list) else []:
        try:step=observation_step(item)
        except (ValueError,KeyError,TypeError):continue
        key=(step['device'],step['check'])
        if key in seen:continue
        seen.add(key);result.append(step)
        if len(result)==12:break
    return result
