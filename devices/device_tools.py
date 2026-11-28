"""登记设备的固定巡检和受控恢复工具；由独立 worker 执行。"""
import datetime
import shlex
import subprocess
import time
from devices.diagnostic_logs import log_command, project_logs, RAW_LIMIT
from devices.device_registry import DEVICES as REGISTRY, HOPS, PROFILES, ROUTER_CONTRACT, profile, allows_check, checks_for, EXTRA_CHECK_LABELS, PROBE_CHECKS
from devices.device_registry import GENERAL_CHECK_LABELS, allows_probe, allows_repair, REPAIR_SERVICE_VERBS, route_parts
from devices.device_registry import MANUAL_CROC_VERBS, run_command_actions
from devices.diagnostic_probes import extended_commands, project_ping

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
ROUTER_CHECKS = {
        'services':('systemctl show '+' '.join(ROUTER_CONTRACT['units'])+
                    ' -p Id -p LoadState -p ActiveState -p SubState -p MainPID -p Result',),
        'interfaces':tuple(interface_command(name) for name in ROUTER_CONTRACT['interfaces']),
        'routes':('ip -4 route show table main','ip -4 rule show',
                  "printf 'IP_FORWARD=' && cat /proc/sys/net/ipv4/ip_forward"),
        'logs':(log_command(group='router'),)}
GROUP_COMMANDS = {'local': LOCAL_COMMANDS, 'remote': REMOTE_CHECKS, 'router': ROUTER_CHECKS}


def bounded_rows(limit, condition='NF', variables=''):
    return ("awk " + variables + " '" + condition +
            " {if (++n <= " + str(limit) + ") print} END {if (!n) print \"NOT_FOUND\";" +
            " else if (n > " + str(limit) + ") print \"TRUNCATED total=\" n}'")


def bounded_read(command):
    # Keep each section available even if tools are absent or permission is denied.
    return ("printf '%s\\n' " + shlex.quote(command) +
            "; { " + command + " 2>/dev/null || printf 'CHECK_UNAVAILABLE\\n'; } | "
            "LC_ALL=C awk 'NR <= 25 {print length($0) > 140 ? substr($0,1,140) \" ...\" : $0}"
            " END {if (NR > 25) print \"TRUNCATED total=\" NR}'")


GENERAL_COMMANDS = {
    'nat_firewall': tuple(map(bounded_read, ('iptables -t nat -S', 'iptables -S', 'ip -4 rule show'))),
    'all_services': (
        'units=$(systemctl list-units --type=service --all --plain --full --no-legend --no-pager) || exit 1\n'
        r"""printf '%s\n' "$units" | awk '
            $2 == "loaded" || $3 == "failed" {
                group = ($3 == "failed" ? "failed" : ($4 == "running" ? "running" : "other"))
                if (++count[group] <= 20) print $1, $2, $3, $4
            }
            END {printf "TOTAL failed=%d running=%d other=%d (up to 20 per group)\n",
                        count["failed"], count["running"], count["other"]}'""",
    ),
    'listening_ports': ('ss -tulnp',),
    'system_overview': (
        'uptime',
        'free -h',
        'disks=$(df -h -l -x tmpfs -x devtmpfs) && printf "%s\\n" "$disks" | ' + bounded_rows(24),
        'processes=$(ps -eo pid,comm,pcpu,pmem --sort=-pcpu) && printf "%s\\n" "$processes" | head -n 11',
        """if command -v docker >/dev/null 2>&1; then
            if containers=$(docker ps --format '{{.Names}} {{.Image}} {{.Status}}'); then
                printf 'Docker containers:\\n'
                printf '%s\\n' "$containers" | """ + bounded_rows(20) + """
            else
                printf 'DOCKER_QUERY_FAILED\\n'
            fi
        else
            printf 'DOCKER_UNAVAILABLE\\n'
        fi""",
    ),
}
CHECKS.update(GENERAL_COMMANDS)
CHECKS.update(EXTRA_CHECK_LABELS)
OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no']
ERRORS = {
    41: 'target_hostname_mismatch', 42: 'check_failed',
    43: 'ingress_hostname_mismatch', 44: 'ingress_user_mismatch',
    45: 'ingress_address_mismatch', 46: 'target_user_mismatch',
    47: 'target_address_mismatch',
    48: 'extended_check_unavailable',
    49: 'repair_command_failed',
}

def quote_command(parts):
    return ' '.join(shlex.quote(part) for part in parts)

def tool_catalog(devices=None):
    """让模型看到实际登记范围及读取边界；设备名称不是当前VIP角色证据。"""
    result = {}
    for device in (devices if devices is not None else DEVICES):
        group = profile(device)['group']
        remote = group == 'remote'
        result[device] = {'label': DEVICE_LABELS[device],
            'registered_management_interface': profile(device)['management_interface'],
            'checks': {
                'services': 'Keepalived/Croc/业务路由单元状态；不检查拨号进程' if remote else 'Keepalived/Croc单元状态',
                'interfaces': 'eth0～4、tun7/tun16、ppp1～6地址与链路标志、9965 TCP连接' if remote else 'eth2/eth3/tun16地址与链路、9965监听',
                'routes': '仅IPv4主路由表及策略规则；不含策略表内容或端到端流量',
                'logs': '固定Keepalived/Croc'+('/业务路由' if remote else '')+'服务当前启动期间最近40条日志，消息限长/敏感字段过滤；历史事件不证明当前状态',
                **GENERAL_CHECK_LABELS,
                **{check:EXTRA_CHECK_LABELS[check] for check in checks_for(device) if check in EXTRA_CHECK_LABELS}},
            'overview': {'action':'read_overview','checks':['services','interfaces'],'read_cost':2},
            'limits': '登记主备名称不证明当前角色；登记管理接口不是本轮已观测接口。接口有IP、服务active或TCP连接不证明业务可用'}
        if group == 'router':
            result[device]['checks'].update(
                services='Keepalived、NetworkManager 和 route-envset 单元状态',
                interfaces='eth0～7 地址、VIP 与链路标志',
                routes='IPv4 主路由表、策略规则和 IPv4 转发开关',
                logs='固定 Router 服务当前启动期间最近 40 条日志，过滤敏感字段',
            )
    return result


def commands_for(device,check):
    if check in GENERAL_COMMANDS:
        return GENERAL_COMMANDS[check]
    extended=extended_commands(device,check)
    if extended is not None:return extended
    return GROUP_COMMANDS[profile(device)['group']][check]


def build_command(device_id, check):
    # 模型只能给枚举值，不能传入IP、用户名、命令、文件路径或SSH选项。
    if (not isinstance(device_id, str) or not isinstance(check, str) or
        not allows_check(device_id,check)):
        raise ValueError('Unregistered device or check')
    error_code = 48 if check in EXTRA_CHECK_LABELS else 42
    return _build_read_command(device_id, commands_for(device_id, check), error_code)


def build_probe_command(device, kind, target):
    if not allows_probe(device, kind, target):
        raise ValueError('Unregistered or invalid probe scope')
    arg = shlex.quote(str(int(target)) if kind == 'port' else target)
    if kind == 'service':
        command = (
            'unit=$(systemctl show --no-pager -p Id -p LoadState -p ActiveState -p SubState '
            '-p MainPID -p Result -- ' + arg + ')\n'
            'status=$?\n'
            'if printf "%s\\n" "$unit" | grep -Fxq LoadState=not-found; then\n'
            '    printf "NOT_FOUND\\n"\n'
            'elif [ "$status" -eq 0 ] && [ -n "$unit" ]; then\n'
            '    printf "%s\\n" "$unit"\n'
            'else exit 42; fi'
        )
    elif kind == 'package':
        command = (
            'if command -v dpkg-query >/dev/null 2>&1; then\n'
            """    packages=$(dpkg-query -W -f='${binary:Package} ${db:Status-Status} ${Version}\\n') || exit 42
        elif command -v rpm >/dev/null 2>&1; then
            packages=$(rpm -qa --queryformat '%{NAME} installed %{VERSION}-%{RELEASE}\\n') || exit 42
        else exit 48; fi
        printf '%s\\n' "$packages" | """ +
            bounded_rows(25, '$2 == "installed" && index(tolower($1), tolower(target))', '-v target=' + arg)
        )
    elif kind == 'process':
        command = ('processes=$(ps -e -o pid=,comm=) && printf "%s\\n" "$processes" | ' +
                   bounded_rows(25, 'index(tolower($2), tolower(target))', '-v target=' + arg))
    elif kind == 'port':
        command = ('listeners=$(ss -tulnpH ' + shlex.quote('sport = :' + str(int(target))) +
                   ') && printf "%s\\n" "$listeners" | ' +
                   bounded_rows(25))
    else:
        command = 'command -v -- ' + arg + ' || printf "NOT_FOUND\\n"'
    return _build_read_command(device, (command,), 42)


def repair_command_text(device_id, action, target):
    if not allows_repair(device_id, action, target):
        raise ValueError('Unregistered or invalid repair scope')
    if action == 'run_command':
        return target  # Display the exact proposal. Only parsed commands go to SSH.
    return _fixed_repair_command(device_id, action, target)


def _fixed_repair_command(device_id, action, target):
    if action == 'stop_test_server':
        return quote_command(['pkill', '-f',
                              r'^([^ ]*/)?python(3(\.[0-9]+)?)? -m http\.server ' + target + r'( |$)'])
    if action == 'list_firewall':
        return quote_command(['iptables', '-t', target, '-L', '-n'])
    if action == 'enable_ip_forward':
        return 'sysctl -w net.ipv4.ip_forward=1'
    if action in ('add_route', 'del_route'):
        cidr, via, interface = route_parts(device_id, target)
        return quote_command(['ip', '-4', 'route', action.split('_')[0],
                              cidr, 'via', via, 'dev', interface, 'proto', 'static'])
    if action == 'bring_up_interface':
        return quote_command(['ip', 'link', 'set', 'dev', target, 'up'])
    return quote_command(['systemctl', {**REPAIR_SERVICE_VERBS, **MANUAL_CROC_VERBS}[action], target])


def build_repair_command(device_id, action, target):
    if action == 'run_command':
        commands = [_fixed_repair_command(device_id, parsed, value)
                    for parsed, value in run_command_actions(device_id, target)]
        bounded = ['timeout -k 2s 20s ' + command + ' 2>&1' for command in commands]
        return _build_read_command(device_id, bounded, 49)
    return _build_read_command(device_id, (repair_command_text(device_id, action, target),), 49)


def execute_repair(device, action, target, executor=None):
    command = build_repair_command(device, action, target)
    return dict(_collect(device, 'repair', command, executor, timeout=98 if action == 'run_command' else 20), action=action, target=target)


def _build_read_command(device_id, commands, error_code):
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
    for command in commands:
        body += command + '\ncode=$?\n'
        if error_code == 49:
            body += 'printf "\\nREPAIR_EXIT_CODE=%s\\n" "$code"\n'
        body += f'[ "$code" -eq 0 ] || exit {error_code}\n'
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
    return _collect(device_id, check, build_command(device_id, check), executor)


def probe_device(device, kind, target, executor=None):
    command = build_probe_command(device, kind, target)
    return dict(_collect(device, 'probe_device', command, executor), kind=kind, target=target)


def _collect(device_id, check, command, executor, timeout=20):
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
        run = executor(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        result['error'] = 'timeout'
        return finish()
    except OSError:
        result['error'] = 'transport_unavailable'
        return finish()
    result['return_code'] = run.returncode
    if run.returncode != 0:
        result['error'] = ERRORS.get(run.returncode, 'ssh_failed')
        if check != "repair" or run.returncode != 49:
            return finish()
    limit = RAW_LIMIT if check == 'logs' else 16000
    if len(run.stdout) > limit and check != 'repair':
        result['error'] = 'output_limit'
        return finish()
    output = run.stdout[:limit].decode('utf-8', errors='replace')
    if len(run.stdout) > limit:
        output += '\nTRUNCATED\n'
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
            output = project_logs(output.encode('utf-8'), group=profile(device_id)['group'])
        except (ValueError, UnicodeError):
            result['error'] = 'log_format_invalid'
            return finish()
        result['evidence_role'] = 'event_history'
    result.update(ok=run.returncode == 0, output=output)
    return finish()
