# Anonymous demonstration addresses and hostnames; no real registration.
"""登记局端/远端的固定只读工具；由独立worker执行，不接受命令文本。"""
import datetime
import shlex
import subprocess
import time

DEVICES = {
    'local-primary': ('192.0.2.13', 'example-ha-primary'),
    'local-backup': ('192.0.2.14', 'example-ha-backup'),
    'remote-primary': ('198.51.100.133', 'example-ha-owner'),
    'remote-backup': ('198.51.100.134', 'example-ha-backup'),
}
DEVICE_LABELS = {'local-primary': '局端主机', 'local-backup': '局端备机',
                 'remote-primary': '远端主机', 'remote-backup': '远端备机'}
CHECKS = {
    'services': "systemctl show keepalived.service example-croc-sar.service -p Id -p ActiveState -p SubState -p MainPID -p Result",
    'interfaces': "ip -br addr show dev eth2; ip -br link show dev eth2; ip -br addr show dev eth3; ip -br link show dev eth3; ip -br addr show dev tun16; ip -br link show dev tun16; ss -lntH 'sport = :9965'",
    'routes': "ip -4 route show table main; ip -4 rule show",
}
REMOTE_INTERFACES = ('eth0', 'eth1', 'eth2', 'eth3', 'eth4', 'tun7', 'tun16',
                     'ppp1', 'ppp2', 'ppp3', 'ppp4', 'ppp5', 'ppp6')
REMOTE_CHECKS = {
    # diald-ppp*是历史进程名，不是假定存在的systemd单元。
    'services': ("systemctl show keepalived.service example-croc-smr.service example-ha-routes.service "
                 "-p Id -p LoadState -p ActiveState -p SubState -p MainPID -p Result",),
    'interfaces': tuple(
        'if [ -e /sys/class/net/' + name + ' ]; then '
        'ip -br addr show dev ' + name + ' && ip -br link show dev ' + name +
        '; else printf "INTERFACE_ABSENT=' + name + '\\n"; fi'
        for name in REMOTE_INTERFACES
    ) + ("ss -ntH '( sport = :9965 or dport = :9965 )'",),
    'routes': tuple(CHECKS['routes'].split('; ')),
}
OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no']
ERRORS = {
    41: 'target_hostname_mismatch', 42: 'check_failed',
    43: 'ingress_hostname_mismatch', 44: 'ingress_user_mismatch',
    45: 'ingress_address_mismatch', 46: 'target_user_mismatch',
    47: 'target_address_mismatch',
}

def quote_command(parts):
    return ' '.join(shlex.quote(part) for part in parts)

def tool_catalog(devices=None):
    """让模型看到实际登记范围及读取边界；设备名称不是当前VIP角色证据。"""
    result = {}
    for device in (devices if devices is not None else DEVICES):
        remote = device.startswith('remote-')
        result[device] = {'label': DEVICE_LABELS[device],
            'checks': {
                'services': 'Keepalived/Croc/业务路由单元状态；不检查拨号进程' if remote else 'Keepalived/Croc单元状态',
                'interfaces': 'eth0～4、tun7/tun16、ppp1～6地址与链路标志、9965 TCP连接' if remote else 'eth2/eth3/tun16地址与链路、9965监听',
                'routes': '仅IPv4主路由表及策略规则；不含策略表内容或端到端流量'},
            'overview': {'action':'read_overview','checks':['services','interfaces'],'read_cost':2},
            'limits': '登记主备名称不证明当前角色；接口有IP、服务active或TCP连接不证明业务可用'}
    return result

def build_command(device_id, check):
    # 模型只能给枚举值，不能传入IP、用户名、命令、文件路径或SSH选项。
    if (not isinstance(device_id, str) or not isinstance(check, str) or
        device_id not in DEVICES or check not in CHECKS):
        raise ValueError('Unregistered device or check')
    address, hostname = DEVICES[device_id]
    remote = device_id.startswith('remote-')
    management_interface, prefix = ('tun7', '/16') if remote else ('eth2', '/24')
    guard = (
        'test "$(hostname)" = ' + shlex.quote(hostname) + ' || exit 41\n'
        'test "$(id -un)" = root || exit 46\n'
        "ip -o -4 addr show dev " + management_interface + " | awk '{print $4}' | grep -Fxq " +
        shlex.quote(address + prefix) + ' || exit 47\n'
    )
    # 每个命令单独捕获退出码，失败结果不冒充完整成功证据。
    body = guard + 'printf "IDENTITY_OK\\n"\n'
    commands = REMOTE_CHECKS[check] if remote else CHECKS[check].split('; ')
    for command in commands:
        body += command + '\ncode=$?\n[ "$code" -eq 0 ] || exit 42\n'
    if remote:
        # 演示管理跳板；示例root上下文直接连接登记远端，不经过示例接入网关。
        return ['sudo', '-n', '-H', '/usr/bin/ssh'] + OPTIONS + [
            'root@' + address, 'sh -c ' + shlex.quote(body)]
    downstream = quote_command(['/usr/bin/ssh'] + OPTIONS + ['root@' + address, 'sh -c ' + shlex.quote(body)])
    ingress_guard = ('test "$(hostname)" = example-ha-primary || exit 43; '
                     'test "$(id -un)" = root || exit 44; '
                     "ip -o -4 addr show dev eth0 | awk '{print $4}' | grep -Fxq 192.0.2.251/24 || exit 45; ")
    return ['sudo', '-n', '-H', '/usr/bin/ssh'] + OPTIONS + ['root@198.51.100.251', ingress_guard + downstream]

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
    if len(run.stdout) > 16000:
        result['error'] = 'output_limit'
        return finish()
    output = run.stdout.decode('utf-8', errors='replace')
    if not output.startswith('IDENTITY_OK\n'):
        result['error'] = 'identity_unconfirmed'
        return finish()
    result.update(ok=True, output=output[len('IDENTITY_OK\n'):])
    return finish()
