"""经审阅的固定运行登记。规划地址不产生权限，网页和模型不能修改此表。"""
import re
import shlex
from ipaddress import IPv4Address, IPv4Network

GENERAL_CHECK_LABELS = {
    'nat_firewall': 'IPv4 NAT 与防火墙规则、策略选路规则',
    'all_services': '已加载、运行及失败的 systemd 服务摘要',
    'listening_ports': 'TCP/UDP 监听端口和对应进程',
    'system_overview': '负载、内存、磁盘、CPU 占用最高的进程和 Docker 容器',
}
CHECK_LABELS = {'services': '服务状态', 'interfaces': '接口、VIP与连接',
                'routes': '主路由表与策略规则', 'logs': '固定服务的限量历史日志'}
CHECK_NAMES = tuple(CHECK_LABELS)
CHECK_LABELS.update(GENERAL_CHECK_LABELS)
PROBE_KINDS = ('service', 'package', 'process', 'port', 'binary')
PROBE_TARGET_PATTERN = r'[A-Za-z0-9._:@-]{1,64}'
PROBE_PORT_PATTERN = r'[0-9]{1,5}'
MAX_PARALLEL_READS = 4
HOPS = {
    'jump_host': {'address': '203.0.113.140', 'hostname': 'example-jump', 'user': 'ubuntu', 'uid': 500},
    'local_ingress': {'address': '10.250.251.251', 'hostname': 'example-local-primary', 'user': 'root',
                      'interface': 'eth0', 'identity_address': '192.0.2.251/24'},
}
ROUTER_CONTRACT = {'units':('keepalived.service','NetworkManager.service','route-envset.service'),
    'interfaces':tuple('eth'+str(i) for i in range(8)),
    'ha_vips':{'eth0':'203.0.113.61','eth1':'203.0.113.62','eth2':'203.0.113.63',
               'eth3':'203.0.113.64','eth6':'203.0.113.65','eth7':'203.0.113.66','eth5':'192.0.2.25'},
    'rule_refs':('KB-003','KB-008','KB-010')}

PROFILES = {
    'router_ha': {**ROUTER_CONTRACT, 'group': 'router', 'side_label': '路由器',
                  'business_role': '中间网络转发', 'management_interface': 'eth5',
                  'prefix': '/24', 'croc_unit': None, 'checks': CHECK_NAMES},
    'local_ha': {'group': 'local', 'side_label': '局端', 'business_role': 'Croc接收侧，连接实验室业务网络',
                 'management_interface': 'eth2', 'prefix': '/24',
                 'units': ('keepalived.service', 'croc-sar.service'), 'croc_unit': 'croc-sar.service',
                 'interfaces': ('eth2', 'eth3', 'tun16'),
                 'ha_vips': {'eth2': '192.0.2.15', 'eth3': '172.27.10.37'},
                 'rule_refs': ('KB-013', 'KB-014'), 'checks': CHECK_NAMES},
    'remote_ha': {'group': 'remote', 'side_label': '远端', 'business_role': 'Croc发起侧，连接现场业务网络',
                  'management_interface': 'tun7', 'prefix': '/16',
                  'units': ('keepalived.service', 'croc2.0smr.service', 'remote-ha-routes.service'),
                  'croc_unit': 'croc2.0smr.service',
                  'interfaces': ('eth0', 'eth1', 'eth2', 'eth3', 'eth4', 'tun7', 'tun16',
                                 'ppp1', 'ppp2', 'ppp3', 'ppp4', 'ppp5', 'ppp6'),
                  'ha_vips': {'eth0': '198.51.100.29'},
                  'rule_refs': ('KB-004', 'KB-010'), 'checks': CHECK_NAMES},
}
DEVICES = {
    'local-primary': {'label': '局端主机', 'address': '192.0.2.13', 'hostname': 'example-local-primary',
                      'profile': 'local_ha', 'inventory_role': 'primary',
                      'management_path': ('jump_host', 'local_ingress'), 'scenes': ('ha_topology', 'croc_local')},
    'local-backup': {'label': '局端备机', 'address': '192.0.2.14', 'hostname': 'example-backup',
                     'profile': 'local_ha', 'inventory_role': 'backup',
                     'management_path': ('jump_host', 'local_ingress'), 'scenes': ('ha_topology', 'croc_local')},
    'remote-primary': {'label': '远端主机', 'address': '198.51.100.133', 'hostname': 'example-remote-primary',
                       'profile': 'remote_ha', 'inventory_role': 'primary',
                       'management_path': ('jump_host',), 'scenes': ('ha_topology',)},
    'remote-backup': {'label': '远端备机', 'address': '198.51.100.134', 'hostname': 'example-backup',
                      'profile': 'remote_ha', 'inventory_role': 'backup',
                      'management_path': ('jump_host',), 'scenes': ('ha_topology',)},
    'router-1': {'label': 'Router1', 'address': '192.0.2.23', 'hostname': 'example-router-1',
                 'profile': 'router_ha', 'inventory_role': 'primary',
                 'management_path': ('jump_host', 'local_ingress'), 'scenes': ('ha_topology',)},
    'router-2': {'label': 'Router2', 'address': '192.0.2.24', 'hostname': 'example-router-2',
                 'profile': 'router_ha', 'inventory_role': 'backup',
                 'management_path': ('jump_host', 'local_ingress'), 'scenes': ('ha_topology',)},
}
SCENE_DEFINITIONS = {
    'ha_topology': {'title': '双机热备拓扑', 'note': '支持已登记的局端、远端和 Router 主备。'},
    'remote_device': {'title': '独立三卡 / 六卡设备', 'note': '待接入代表设备、核实身份与检查工具。'},
    'croc_local': {'title': 'Croc业务局端', 'note': '支持已登记局端；独立物理机和云服务器待登记。'},
}

PENDING_DEVICES = {}

REPAIR_SERVICE_VERBS = {
    'start_service': 'start',
    'restart_service': 'restart',
    'reset_failed_service': 'reset-failed',
    'stop_service': 'stop',
}
REPAIR_UNITS = {
    'local_ha': ('keepalived.service', 'croc-sar.service'),
    'remote_ha': ('keepalived.service', 'croc2.0smr.service', 'remote-ha-routes.service'),
    'router_ha': ('keepalived.service', 'route-envset.service'),
}
APPROVAL_SERVICE_UNITS = (
    'mysql.service', 'mysqld.service', 'mariadb.service', 'redis.service',
    'redis-server.service', 'nginx.service', 'docker.service',
)
AUTO_SERVICE_ACTIONS = ('start_service', 'restart_service', 'reset_failed_service')
REPAIR_APPROVAL_NOTE = (
    'mysql、mysqld、mariadb、redis、redis-server、nginx、docker 等非管理业务服务'
    '可人工批准启动、重启、清除失败状态与停止，服务名可省略 .service。'
    '管理与系统基础服务禁止变更。'
)
IP_FORWARD_TARGET = 'net.ipv4.ip_forward'
PRIVATE_ROUTE_NETWORKS = tuple(map(IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
PROTECTED_ROUTE_NETWORKS = tuple(map(IPv4Network, (
    '192.0.2.0/24', '10.250.251.0/24', '198.51.100.0/24', '172.27.110.0/24',
)))


def business_interfaces(device):
    info = profile(device)
    protected = {info['management_interface']}
    if info['group'] == 'remote':
        protected.add('eth4')  # VRRP heartbeat
    return tuple(interface for interface in info['interfaces'] if interface not in protected)


def route_parts(device, target):
    """Validate the canonical route payload before constructing a command."""
    parts = target.split(' ')
    if len(parts) != 5 or parts[1] != 'via' or parts[3] != 'dev' or '/' not in parts[0]:
        raise ValueError('invalid_route')
    cidr, _, via, _, interface = parts
    if interface not in business_interfaces(device):
        raise ValueError('invalid_route_interface')
    network, gateway = IPv4Network(cidr), IPv4Address(via)
    if (str(network) != cidr or str(gateway) != via
            or not any(network.network_address in scope and network.broadcast_address in scope
                       for scope in PRIVATE_ROUTE_NETWORKS)
            or any(network.overlaps(scope) or gateway in scope for scope in PROTECTED_ROUTE_NETWORKS)
            or gateway.is_loopback or gateway.is_link_local or gateway.is_multicast
            or gateway.is_reserved or gateway in IPv4Network('0.0.0.0/8')):
        raise ValueError('invalid_route_network')
    return cidr, via, interface


def repair_scopes(device):
    device_profile = DEVICES[device]['profile']
    units = REPAIR_UNITS[device_profile] + APPROVAL_SERVICE_UNITS
    scopes = {action: units for action in REPAIR_SERVICE_VERBS}
    if device_profile == 'router_ha':
        scopes['enable_ip_forward'] = (IP_FORWARD_TARGET,)
    scopes['add_route'] = scopes['del_route'] = ('CIDR via IPv4 dev business_interface',)
    scopes['bring_up_interface'] = business_interfaces(device)
    scopes['run_command'] = ('human_approval_required',)
    return scopes


def allows_repair(device, action, target):
    if not (isinstance(device, str) and device in DEVICES
            and isinstance(action, str) and isinstance(target, str)):
        return False
    try:
        if action == 'run_command':
            run_command_actions(device, target)
            return True
        if action in ('add_route', 'del_route'):
            route_parts(device, target)
            return True
    except ValueError:
        return False
    return target in repair_scopes(device).get(action, ())


def allows_auto_repair(device, action, target):
    if not allows_repair(device, action, target):
        return False
    if action in AUTO_SERVICE_ACTIONS:
        return target in REPAIR_UNITS[DEVICES[device]['profile']]
    return action in ('enable_ip_forward', 'add_route', 'bring_up_interface')


def service_unit(name):
    return name if name.endswith('.service') else name + '.service'


# Explicit diagnostic reasons for denied requests. Execution also requires a
# match against the command grammar below, including every command argument.
HIGH_RISK_COMMANDS = {
    'disk': r'\b(?:rm|mkfs(?:\.\w+)?|dd|shred)\b|>\s*/dev/(?:sd|vd|nvme)',
    'power': r'\b(?:reboot|shutdown|poweroff|halt)\b|\b(?:telinit|init)\s+[06]\b',
    'firewall': r'\b(?:iptables(?:-legacy|-nft)?|ip6tables)\b[^\n;]*(?:\s-[FX]\b|--(?:flush|delete-chain)\b)'
                r'|\bnft\s+flush\b',
    'management': r'\b(?:ssh|sshd|authorized_keys|passwd|chpasswd|userdel|NetworkManager|systemd-networkd|systemd-resolved|dbus)\b',
}
MANUAL_CROC_VERBS = {'reload_croc': 'reload'}
MAX_COMMAND_LENGTH = 1000
MAX_REQUEST_BYTES = 2048
COMMAND_CHARACTERS = r"""[A-Za-z0-9._/:=,@'"\- \n]+"""
MAX_COMMAND_LINES = 4


def run_command_actions(device, target):
    """Parse an approved command list into fixed action/target pairs."""
    if (not isinstance(device, str) or device not in DEVICES or not isinstance(target, str)
            or not 1 <= len(target) <= MAX_COMMAND_LENGTH):
        raise ValueError('invalid_command')
    for category, pattern in HIGH_RISK_COMMANDS.items():
        if re.search(pattern, target, re.IGNORECASE):
            raise ValueError('high_risk_' + category)
    protected_interfaces = set(profile(device)['interfaces']) - set(business_interfaces(device))
    for line in target.split('\n'):
        if (re.search(r'\b(?:down|del(?:ete)?|flush)\b', line, re.IGNORECASE)
                and (any(re.search(rf'\b{re.escape(name)}\b', line, re.IGNORECASE)
                         for name in protected_interfaces)
                     or re.search(r'\b(?:192\.0\.2\.|10\.250\.251\.|198\.51\.100\.|251\b|default\b|0\.0\.0\.0/0\b)', line))):
            raise ValueError('high_risk_management')
    if not re.fullmatch(COMMAND_CHARACTERS, target):
        raise ValueError('unsupported_command')
    lines = [line for line in target.split('\n') if line.strip()]
    if not 1 <= len(lines) <= MAX_COMMAND_LINES:
        raise ValueError('invalid_command')
    actions = [_parse_run_command(device, shlex.split(line)) for line in lines]
    if len(set(actions)) != len(actions):
        raise ValueError('repair_action_already_proposed')
    return actions


def _parse_run_command(device, words):
    if len(words) == 3 and words[0] == 'systemctl':
        unit = service_unit(words[2])
        for action, verb in MANUAL_CROC_VERBS.items():
            if words[1] == verb and unit == profile(device)['croc_unit']:
                return action, unit
        for action, verb in REPAIR_SERVICE_VERBS.items():
            if words[1] == verb and allows_repair(device, action, unit):
                return action, unit
    if words[:2] == ['pkill', '-f'] and len(words) == 3:
        match = re.fullmatch(r'http\.server ([1-9][0-9]{3,4})', words[2])
        if match and 1024 <= int(match[1]) <= 65535:
            return 'stop_test_server', match[1]
    if words in (['iptables', '-t', 'nat', '-L', '-n'], ['iptables', '-L', '-n']):
        return 'list_firewall', 'nat' if '-t' in words else 'filter'
    if len(words) == 5 and words[:3] == ['ip', 'link', 'set']:
        words = [*words[:3], 'dev', *words[3:]]
    if words == ['sysctl', '-w', IP_FORWARD_TARGET+'=1'] and allows_repair(
            device, 'enable_ip_forward', IP_FORWARD_TARGET):
        return 'enable_ip_forward', IP_FORWARD_TARGET
    if len(words) == 6 and words[:4] == ['ip', 'link', 'set', 'dev'] and words[-1] == 'up':
        if allows_repair(device, 'bring_up_interface', words[4]):
            return 'bring_up_interface', words[4]
    if words[:2] == ['ip', '-4']:
        words = ['ip', *words[2:]]
    if words[:2] == ['ip', 'route'] and words[2:3] in (['add'], ['del']) and words[-2:] == ['proto', 'static']:
        action = words[2] + '_route'
        target = ' '.join(words[3:-2])
        if allows_repair(device, action, target):
            return action, target
    raise ValueError('unsupported_command')


def allows_repair_request(operation, device, action, target):
    expected = 'manual_command' if action == 'run_command' else 'approved_repair'
    return operation == expected and allows_repair(device, action, target)

PING_TARGETS = {'ping_' + key.replace('-', '_'): key for key in DEVICES}
TUNNEL_CHECK = 'ping_tunnel_peer'
TUNNEL_BACKUP_CHECK = 'ping_tunnel_remote_backup'
TUNNEL_CHECKS = frozenset((TUNNEL_CHECK,TUNNEL_BACKUP_CHECK))
# The local receiver address is shared by the local HA pair. Remote chassis
# have separate inner addresses, including when the standby takes over.
TUNNEL_ENDPOINTS = {'local-primary':'203.0.113.1','local-backup':'203.0.113.1',
                    'remote-primary':'203.0.113.133','remote-backup':'203.0.113.134'}
PROBE_CHECKS = frozenset(PING_TARGETS) | TUNNEL_CHECKS


def tunnel_endpoints(device, check=TUNNEL_CHECK):
    if device not in TUNNEL_ENDPOINTS or check not in TUNNEL_CHECKS or check==TUNNEL_BACKUP_CHECK and profile(device)['group']!='local':
        raise ValueError('Unregistered tunnel direction')
    target=('local-primary' if profile(device)['group']=='remote' else
            'remote-backup' if check==TUNNEL_BACKUP_CHECK else 'remote-primary')
    return TUNNEL_ENDPOINTS[device],TUNNEL_ENDPOINTS[target]


EXTRA_CHECK_LABELS = {
    TUNNEL_CHECK: '核对本端 tun16 地址及系统选路，向登记的对端隧道地址发送 3 个 ICMP 探测',
    TUNNEL_BACKUP_CHECK: '核对本端 tun16 地址及系统选路，向远端备机隧道地址 203.0.113.134 发送 3 个 ICMP 探测',
    'policy_routes': 'IPv4 策略规则和全部路由表',
    'ha_config': 'Keepalived 文件中的接口和选举字段',
    **{check: '查看去往' + DEVICES[target]['label'] + '管理地址的路由，再按系统选路发送 3 个 ICMP 探测' for check,target in PING_TARGETS.items()},
}
CHECK_LABELS.update(EXTRA_CHECK_LABELS)


def profile(device):
    return PROFILES[DEVICES[device]['profile']]


def checks_for(device):
    return profile(device)['checks'] + tuple(GENERAL_CHECK_LABELS) + tuple(check for check in EXTRA_CHECK_LABELS
                                             if PING_TARGETS.get(check)!=device and
                                             (check not in TUNNEL_CHECKS or device in TUNNEL_ENDPOINTS) and
                                             (check!=TUNNEL_BACKUP_CHECK or profile(device)['group']=='local'))


def scenes():
    return {scene: dict(info, devices=tuple(key for key,value in DEVICES.items() if scene in value['scenes']))
            for scene,info in SCENE_DEFINITIONS.items()}


def allows_check(device, check):
    return isinstance(device,str) and device in DEVICES and isinstance(check,str) and check in checks_for(device)


def allows_probe(device, kind, target):
    if (not isinstance(device, str) or device not in DEVICES or
            kind not in PROBE_KINDS or not isinstance(target, str)):
        return False
    if kind == 'port':
        return bool(re.fullmatch(PROBE_PORT_PATTERN, target)) and 1 <= int(target) <= 65535
    return bool(re.fullmatch(PROBE_TARGET_PATTERN, target))


def public_catalog():
    # 固定管理地址用于管理员核对目标；不输出凭据或执行命令。
    return {'kind': 'reviewed_runtime_allowlist', 'current_state_verified': False,
            'devices': [dict(id=key, label=value['label'], address=value['address'],
                             inventory_role=value['inventory_role'], group=profile(key)['group'],
                             scenes=list(value['scenes']), checks=list(checks_for(key)),
                             management_path=list(value['management_path']), current_role='unknown')
                        for key,value in DEVICES.items()],
            'check_labels': CHECK_LABELS,
            'pending_devices': [],
            'scenes': [{'id':key,'title':value['title'],'available':bool(value['devices']),'note':value['note']}
                       for key,value in scenes().items()]}
