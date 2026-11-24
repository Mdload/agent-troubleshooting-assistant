"""公开演示登记，使用文档专用地址。网页和模型不能修改此表。"""
CHECK_LABELS = {'services': '服务状态', 'interfaces': '接口、VIP与连接',
                'routes': '主路由表与策略规则', 'logs': '固定服务的限量历史日志'}
CHECK_NAMES = tuple(CHECK_LABELS)
HOPS = {
    'jump_host': {'address': '203.0.113.140', 'hostname': 'example-jump', 'user': 'ubuntu', 'uid': 500},
    'local_ingress': {'address': '203.0.113.251', 'hostname': 'example-local-primary', 'user': 'root',
                      'interface': 'eth0', 'identity_address': '192.0.2.251/24'},
}
PROFILES = {
    'local_ha': {'group': 'local', 'side_label': '局端', 'business_role': 'Croc接收侧，连接实验室业务网络',
                 'management_interface': 'eth2', 'prefix': '/24',
                 'units': ('keepalived.service', 'croc-sar.service'), 'croc_unit': 'croc-sar.service',
                 'interfaces': ('eth2', 'eth3', 'tun16'),
                 'ha_vips': {'eth2': '192.0.2.15', 'eth3': '192.0.2.37'},
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
}
SCENE_DEFINITIONS = {
    'ha_topology': {'title': '双机热备拓扑', 'note': '支持已登记的局端、远端主备；Router等节点尚未接入。'},
    'remote_device': {'title': '独立三卡 / 六卡设备', 'note': '待接入代表设备、核实身份与检查工具。'},
    'croc_local': {'title': 'Croc业务局端', 'note': '支持已登记局端；独立物理机和云服务器待登记。'},
}

# 仅待接入资料，不加入DEVICES、SCENES或worker白名单。管理接口须现场核验。
ROUTER_CONTRACT = {'units':('keepalived.service','NetworkManager.service','route-envset.service'),
    'interfaces':tuple('eth'+str(i) for i in range(8)),
    'ha_vips':{'eth0':'203.0.113.61','eth1':'203.0.113.62','eth2':'203.0.113.63',
               'eth3':'203.0.113.64','eth6':'203.0.113.65','eth7':'203.0.113.66','eth5':'192.0.2.1'},
    'evidence_date':'2026-09-28','rule_refs':('KB-003','KB-008','KB-010')}
PENDING_DEVICES = {
    'router-1':{'label':'Router1','address':'192.0.2.23','historical_hostname':'example-router-1'},
    'router-2':{'label':'Router2','address':'192.0.2.24','historical_hostname':'example-router-2'},
}


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
    if check not in TUNNEL_CHECKS or check==TUNNEL_BACKUP_CHECK and profile(device)['group']!='local':
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
    return profile(device)['checks'] + tuple(check for check in EXTRA_CHECK_LABELS
                                             if PING_TARGETS.get(check)!=device and
                                             (check!=TUNNEL_BACKUP_CHECK or profile(device)['group']=='local'))


def scenes():
    return {scene: dict(info, devices=tuple(key for key,value in DEVICES.items() if scene in value['scenes']))
            for scene,info in SCENE_DEFINITIONS.items()}


def allows_check(device, check):
    return isinstance(device,str) and device in DEVICES and isinstance(check,str) and check in checks_for(device)


def public_catalog():
    # 固定管理地址用于管理员核对目标；不输出凭据或执行命令。
    return {'kind': 'reviewed_runtime_allowlist', 'current_state_verified': False,
            'devices': [dict(id=key, label=value['label'], address=value['address'],
                             inventory_role=value['inventory_role'], group=profile(key)['group'],
                             scenes=list(value['scenes']), checks=list(checks_for(key)),
                             management_path=list(value['management_path']), current_role='unknown')
                        for key,value in DEVICES.items()],
            'check_labels': CHECK_LABELS,
            'pending_devices':[dict(value,id=key,execution_enabled=False,management_interface=None,
                                    evidence_date=ROUTER_CONTRACT['evidence_date'],
                                    blocker='待核验当前管理接口与身份，未加入运行白名单')
                               for key,value in PENDING_DEVICES.items()],
            'scenes': [{'id':key,'title':value['title'],'available':bool(value['devices']),'note':value['note']}
                       for key,value in scenes().items()]}
