"""Read-only Linux interface findings for the registered HA topology.

Adapted from netops-ai's parse_brief and check_interfaces in
netops_ai/inspection/status.py at ee3133a181455e2c1143f2918a72e83c3d8b2008.
Copyright 2026 the netops-ai authors. Apache-2.0.
Changed the input from IOS output to Linux ip -br and limited findings to
registered interfaces and literal observations rather than path health.
"""
import ipaddress
import re
from collections import Counter


BRIEF_ROW = re.compile(r'^(?P<name>[^\s@]+)(?:@[^\s]+)?\s+'
                       r'(?P<state>UP|DOWN|UNKNOWN|LOWERLAYERDOWN|DORMANT|NOTPRESENT|TESTING)(?:\s|$)')


def parse_brief(output):
    rows = {}
    conflicts = set()
    for line in output.splitlines():
        match = BRIEF_ROW.match(line.strip())
        if match:
            name, state = match.group('name', 'state')
            previous = rows.get(name)
            if previous and previous['state'] != state:
                conflicts.add(name)
            rows[name] = {'state':state, 'line':line.strip()}
    return rows, conflicts


def check_interfaces(output, names):
    """Each observed state has its original row. Missing output stays unknown."""
    rows,conflicts = parse_brief(output)
    absent = {line.split('=',1)[1] for line in output.splitlines() if line.startswith('INTERFACE_ABSENT=')}
    findings = {}
    for name in names:
        row = rows.get(name)
        if row and name not in absent and name not in conflicts:
            findings[name] = dict(row)
        elif not row and name in absent:
            findings[name] = {'state':'absent', 'line':'INTERFACE_ABSENT='+name}
        else:
            findings[name] = {'state':'unconfirmed', 'line':None}
    return findings


def interface_ipv4_samples(output, names, preserve_prefix=False):
    """Read local addresses separately from peer addresses and ip -br link rows."""
    findings = check_interfaces(output, names)
    samples = {name: set() for name in names}
    for line in output.splitlines():
        match = BRIEF_ROW.match(line.strip())
        if not match or match['name'] not in samples:
            continue
        fields = line.strip()[match.end():].split()
        if any('<' in field or field.startswith('link/') for field in fields):
            continue
        addresses = []
        peer = False
        try:
            for field in fields:
                if field == 'peer' and not peer:
                    peer = True
                    continue
                address = ipaddress.ip_interface(field)
                if not peer and address.version == 4:
                    addresses.append(str(address if preserve_prefix and '/' in field else address.ip))
                peer = False
            sample = None if peer else frozenset(addresses)
        except ValueError:
            sample = None
        samples[match['name']].add(sample)
    for name, finding in findings.items():
        values = samples[name]
        finding['ipv4'] = (next(iter(values)) if len(values) == 1 and
                           finding['state'] not in ('absent', 'unconfirmed') else None)
    return findings


def wants_ppp_connections(question):
    return bool(re.search(r'ppp|拨号', question, re.I) and
                re.search(r'(?<!\d)9965(?!\d)|(?:tcp|croc)\s*(?:连接|会话)', question, re.I))


def established_local_addresses(output):
    sockets = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != 'ESTAB' or not all(value.isdecimal() for value in fields[1:3]):
            continue
        local, peer = fields[3], fields[4]
        local_host, _, local_port = local.rpartition(':')
        peer_host, _, peer_port = peer.rpartition(':')
        if peer_port != '9965' or not local_port.isdecimal() or len(local_port) > 5 or not 0 < int(local_port) <= 65535:
            continue
        try:
            local_ip = ipaddress.ip_address(local_host.strip('[]'))
            peer_ip = ipaddress.ip_address(peer_host.strip('[]'))
        except ValueError:
            continue
        if local_ip.version == 6:
            local_ip = local_ip.ipv4_mapped
        if local_ip is not None:
            sockets.add((str(local_ip), int(local_port), peer_ip))
    return Counter(local_ip for local_ip, _, _ in sockets)


def ppp_summary(output, names, question):
    ports = [name for name in names if re.fullmatch(r'ppp\d+', name)]
    if not ports or not re.search(r'ppp|拨号', question, re.I):
        return ''
    connections = wants_ppp_connections(question)
    findings = interface_ipv4_samples(output, names if connections else ports)
    if connections:
        sockets = established_local_addresses(output)
        owners = Counter(address for finding in findings.values() for address in finding['ipv4'] or ())
    lines = []
    for name in ports:
        finding = findings[name]
        state, addresses = finding['state'], finding['ipv4']
        if state == 'absent':
            label = '未发现接口'
        elif addresses is None:
            label = '本地 IPv4 未确认'
        else:
            label = ('有本地 IPv4' if addresses else '未见本地 IPv4') + '（' + state + '）'
        if connections and addresses:
            count = sum(sockets[address] for address in addresses if owners[address] == 1)
            ambiguous = any(sockets[address] and owners[address] > 1 for address in addresses)
            if count:
                label += '，本端地址匹配 ' + str(count) + ' 条已建立连接'
            if ambiguous:
                label += '，多个接口共享地址，无法唯一归属连接'
            elif not count:
                label += '，未见匹配连接'
        lines.append(name + ' ' + label)
    limit = ('。按本次采样的本端地址与对端 TCP 9965 已建立连接比对，实际出接口和业务收发未验证。'
             if connections else '。逐卡可达性仍需核对源地址选路及限次探测。')
    return '；'.join(lines) + limit
