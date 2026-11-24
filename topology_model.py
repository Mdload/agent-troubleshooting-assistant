"""从固定运行登记投影HA资源关系；登记信息不代表现场状态或权限。"""
from device_registry import DEVICES, HOPS, profile

SNAPSHOT_DATE='2026-09-28'

TOPOLOGY_POLICY = '''此演示拓扑采用三层独立热备。远端下挂通过198.51.100.29进入活动SMR的tun16/Croc，经有线模拟卫星或三张业务PPP、中间TOR4和Router，到局端192.0.2.15的SAR，再到局端业务网192.0.2.0/24；响应反向。Router只转发，七个VIP应同步接管，角色切换仍需实际收发证据。
地址按用途解释。远端198.51.100.133 与 198.51.100.134是tun7管理地址；局端192.0.2.13/.14是固定外层IP，.15是Croc接收VIP，.25是Router局端侧VIP。远端主机tun16为203.0.113.133，备机为203.0.113.134，局端为203.0.113.1。对话比较真实地址、物理地址、VIP、隧道IP时，真实地址优先指设备固定IP；只有明确问MAC或二层邻接时讨论MAC。
普通 ping 记录内核默认选路，指定源地址探测检查该源的策略路由，二者应同时保留方法与目标。普通ping失败只支持该次选路的结果；结合ip rule、全部路由表和回程再判断Croc路径。任何指定源探测都要先展示源、目标与批准范围，当前工具缺少该能力时说明缺项。
合成有线路径示例为远端eth1/203.0.113.69经203.0.113.70到Router侧VLAN64，eth2/203.0.113.55经203.0.113.56到VLAN65，eth3/203.0.113.41经203.0.113.42到VLAN66。PPP6/5/4对应三个示例运营商及VLAN61/62/63。TOR4内部配置与最新接线仍须核实。
故障判断逐层取证。管理跳板到接入网关失联不能推出四台下游业务均坏；基站下电时仍要核对有线候选与Croc启动门禁；VIP漂移不证明Router已接收业务；tun16的ICMP应答不证明两台下挂业务收发。远端切换引起的暂时会话空缺，应先核对局端本机服务、监听与路由。
正常HA中，PPP拨号、tun7、SSH两台常驻，业务VIP、Croc和相应业务路由随MASTER。固定主机测试模式由ha-business-test服务控制，start进入、stop恢复，判断前核对模式。旧配置、旧故障和估计切换时间均带历史范围；回答先给所问结论和关键依据，再给会改变判断的缺项。以上机制不产生当前角色、工具权限或本轮观测。'''


def ha_pairs():
    pairs=[]
    for group in ('local','remote'):
        devices=[device for device in DEVICES if profile(device)['group']==group]
        if len(devices)!=2:continue
        first=profile(devices[0])
        if any(profile(device)['ha_vips']!=first['ha_vips'] for device in devices):continue
        pairs.append({'id':group+'_ha','group':group,'label':first['side_label']+'主备',
            'devices':devices,'expected_vips':dict(first['ha_vips']),
            'operating_mode':'normal_ha','rule_refs':list(first['rule_refs']),
            'evidence_date':SNAPSHOT_DATE,
            'configuration_verified_current':False})
    return pairs


def peer_relation(device):
    """从同组资源关系找对端；登记主/备名称不决定谁当前持有VIP。"""
    for pair in ha_pairs():
        if device in pair['devices']:
            peer=next(item for item in pair['devices'] if item!=device)
            return {'id':pair['id'],'kind':'shared_ha_resources','device':device,'peer':peer,
                'peer_label':DEVICES[peer]['label'],'rule_refs':list(pair['rule_refs']),
                'evidence_date':pair['evidence_date'],'operating_mode':pair['operating_mode'],
                'configuration_verified_current':False,'grants_device_access':False}
    return None


def snapshot():
    """最小结构化视图，仅已登记主备与管理路径；物理接线等未知项不补猜。"""
    return {'kind':'reviewed_registry_projection','current_state_verified':False,
        'nodes':[{'id':device,'label':item['label'],'group':profile(device)['group'],
            'inventory_role':item['inventory_role'],'current_role':'unknown',
            'management_address':item['address'],'management_path':list(item['management_path']),
            'software_version':None} for device,item in DEVICES.items()],
        'management_hops':[dict(value,id=key) for key,value in HOPS.items()],
        'ha_resource_groups':ha_pairs(),
        'unknown':['当前模式、配置与角色','交换机物理接线与VLAN','Router实时资源状态',
                   '节点软件版本','独立三卡/六卡设备映射'],
        'grants_device_access':False}
