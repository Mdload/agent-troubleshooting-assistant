"""登记局端/远端的固定只读工具；由独立worker执行，不接受命令文本。"""
import datetime
import shlex
import subprocess
import time
from diagnostic_logs import log_command, project_logs, RAW_LIMIT
from device_registry import DEVICES as REGISTRY, HOPS, PROFILES, profile, allows_check, checks_for, EXTRA_CHECK_LABELS, PROBE_CHECKS
from diagnostic_probes import extended_commands, project_ping

DEVICES = {key: (value['address'],value['hostname']) for key,value in REGISTRY.items()}
DEVICE_LABELS = {key: value['label'] for key,value in REGISTRY.items()}
CHECKS = {
    'services': 'systemctl show '+ ' '.join(PROFILES['local_ha']['units'])+' -p Id -p LoadState -p ActiveState -p SubState -p MainPID -p Result',
    'interfaces': "ip -br addr show dev eth2; ip -br link show dev eth2; ip -br addr show dev eth3; ip -br link show dev eth3; ip -br addr show dev tun16; ip -br link show dev tun16; ss -lntH 'sport = :9965'",
    'routes': "ip -4 route show table main; ip -4 rule show",
    'logs': log_command(),
}
REMOTE_INTERFACES = PROFILES['remote_ha']['interfaces']
def interface_command(name):
    return ('if [ -e /sys/class/net/'+name+' ]; then ip -br addr show dev '+name+
            ' && ip -br link show dev '+name+'; else printf "INTERFACE_ABSENT='+name+'\\n"; fi')

LOCAL_COMMANDS = {check:tuple(command.split('; ')) for check,command in CHECKS.items()}
LOCAL_COMMANDS['interfaces'] = tuple(interface_command(name) for name in PROFILES['local_ha']['interfaces']) + (
    "ss -lntH 'sport = :9965'",)
CHECKS['interfaces'] = '; '.join(LOCAL_COMMANDS['interfaces'])
REMOTE_CHECKS = {
    # diald-ppp*是历史进程名，不是假定存在的systemd单元。
    'services': ('systemctl show '+' '.join(PROFILES['remote_ha']['units'])+
                 ' -p Id -p LoadState -p ActiveState -p SubState -p MainPID -p Result',),
    'interfaces': tuple(interface_command(name) for name in REMOTE_INTERFACES) + ("ss -ntH '( sport = :9965 or dport = :9965 )'",),
    'routes': tuple(CHECKS['routes'].split('; ')),
    'logs': (log_command(remote=True),),
}
CHECKS.update(EXTRA_CHECK_LABELS)
OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no']
ERRORS = {
    41: 'target_hostname_mismatch', 42: 'check_failed',
    43: 'ingress_hostname_mismatch', 44: 'ingress_user_mismatch',
    45: 'ingress_address_mismatch', 46: 'target_user_mismatch',
    47: 'target_address_mismatch',
    48: 'extended_check_unavailable',
}

def quote_command(parts):
    return ' '.join(shlex.quote(part) for part in parts)

def tool_catalog(devices=None):
    """让模型看到实际登记范围及读取边界；设备名称不是当前VIP角色证据。"""
    result = {}
    for device in (devices if devices is not None else DEVICES):
        remote = profile(device)['group']=='remote'
        result[device] = {'label': DEVICE_LABELS[device],
            'registered_management_interface': profile(device)['management_interface'],
            'checks': {
                'services': 'Keepalived/Croc/业务路由单元状态；不检查拨号进程' if remote else 'Keepalived/Croc单元状态',
                'interfaces': 'eth0～4、tun7/tun16、ppp1～6地址与链路标志、9965 TCP连接' if remote else 'eth2/eth3/tun16地址与链路、9965监听',
                'routes': '仅IPv4主路由表及策略规则；不含策略表内容或端到端流量',
                'logs': '固定Keepalived/Croc'+('/业务路由' if remote else '')+'服务当前启动期间最近40条日志，消息限长/敏感字段过滤；历史事件不证明当前状态',
                **{check:EXTRA_CHECK_LABELS[check] for check in checks_for(device) if check in EXTRA_CHECK_LABELS}},
            'overview': {'action':'read_overview','checks':['services','interfaces'],'read_cost':2},
            'limits': '登记主备名称不证明当前角色；登记管理接口不是本轮已观测接口。接口有IP、服务active或TCP连接不证明业务可用'}
    return result

def commands_for(device,check):
    extended=extended_commands(device,check)
    if extended is not None:return extended
    return (REMOTE_CHECKS if profile(device)['group']=='remote' else LOCAL_COMMANDS)[check]


def build_command(device_id, check):
    # 模型只能给枚举值，不能传入IP、用户名、命令、文件路径或SSH选项。
    if (not isinstance(device_id, str) or not isinstance(check, str) or
        not allows_check(device_id,check)):
        raise ValueError('Unregistered device or check')
    address, hostname = DEVICES[device_id]
    device_profile=profile(device_id)
    management_interface, prefix = device_profile['management_interface'],device_profile['prefix']
    guard = (
        'test "$(hostname)" = ' + shlex.quote(hostname) + ' || exit 41\n'
        'test "$(id -un)" = root || exit 46\n'
        "ip -o -4 addr show dev " + management_interface + " | awk '{print $4}' | grep -Fxq " +
        shlex.quote(address + prefix) + ' || exit 47\n'
    )
    # 每个命令单独捕获退出码，失败结果不冒充完整成功证据。
    body = guard + 'printf "IDENTITY_OK\\n"\n'
    for command in commands_for(device_id,check):
        error_code=48 if check in EXTRA_CHECK_LABELS else 42
        body += command + f'\ncode=$?\n[ "$code" -eq 0 ] || exit {error_code}\n'
    management_path=REGISTRY[device_id]['management_path']
    if management_path not in (('jump_host',), ('jump_host','local_ingress')):
        raise ValueError('Unreviewed management path')
    if management_path==('jump_host',):
        # Windows先进入140；这里从140 root身份直接连接登记远端，不经过251。
        return ['sudo', '-n', '-H', '/usr/bin/ssh'] + OPTIONS + [
            'root@' + address, 'sh -c ' + shlex.quote(body)]
    downstream = quote_command(['/usr/bin/ssh'] + OPTIONS + ['root@' + address, 'sh -c ' + shlex.quote(body)])
    ingress=HOPS[REGISTRY[device_id]['management_path'][-1]]
    ingress_guard = ('test "$(hostname)" = '+shlex.quote(ingress['hostname'])+' || exit 43; '
                     'test "$(id -un)" = '+shlex.quote(ingress['user'])+' || exit 44; '
                     "ip -o -4 addr show dev "+ingress['interface']+" | awk '{print $4}' | grep -Fxq "+
                     shlex.quote(ingress['identity_address'])+' || exit 45; ')
    return ['sudo', '-n', '-H', '/usr/bin/ssh'] + OPTIONS + ['root@'+ingress['address'], ingress_guard + downstream]

def probe(device_id, check, executor=None):
    command = build_command(device_id, check)
    started = datetime.datetime.utcnow().isoformat() + 'Z'
    result = {'device': device_id, 'address': DEVICES[device_id][0], 'check': check,
              'collected_at': started, 'clock_source': 'collector_utc',
              'ok': False, 'output': '', 'return_code': None}
    began = time.monotonic()
    def finish():
        result['duration_ms'] = round((time.monotonic() - began) * 1000)
        return result
    executor = executor or subprocess.run
    try:
        run = executor(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        result['error'] = 'timeout'
        return finish()
    except OSError:
        result['error'] = 'transport_unavailable'
        return finish()
    result['return_code'] = run.returncode
    if run.returncode != 0:
        result['error'] = ERRORS.get(run.returncode, 'ssh_failed')
        return finish()
    if len(run.stdout) > (RAW_LIMIT if check == 'logs' else 16000):
        result['error'] = 'output_limit'
        return finish()
    output = run.stdout.decode('utf-8', errors='replace')
    if not output.startswith('IDENTITY_OK\n'):
        result['error'] = 'identity_unconfirmed'
        return finish()
    output = output[len('IDENTITY_OK\n'):]
    if check in PROBE_CHECKS:
        try:
            output = project_ping(output)
        except ValueError:
            result['error'] = 'extended_check_unavailable'
            return finish()
    if check == 'logs':
        try:
            output = project_logs(output.encode('utf-8'), remote=profile(device_id)['group']=='remote')
        except (ValueError, UnicodeError):
            result['error'] = 'log_format_invalid'
            return finish()
        result['evidence_role'] = 'event_history'
    result.update(ok=True, output=output)
    return finish()
