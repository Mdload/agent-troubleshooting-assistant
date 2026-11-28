"""Small fixed diagnostic commands and their evidence parsers."""
import re
import shlex
import ipaddress

from devices.device_registry import DEVICES, PING_TARGETS, PROBE_CHECKS, TUNNEL_CHECKS, tunnel_endpoints

PING_COUNT = 3
KEEPALIVED_FILE = '/etc/keepalived/keepalived.conf'
KEEPALIVED_FIELDS = {
    'interface': '接口', 'unicast_src_ip': '单播源', 'peer': '单播对端',
    'virtual_router_id': 'VRID', 'priority': '优先级', 'advert_int': '通告间隔',
}

# Emit just an allowlisted projection. In particular, never emit raw lines or follow includes.
KEEPALIVED_AWK = r'''
NR > 2048 { print "TRUNCATED=1"; exit }
{
    sub(/[#!].*$/, "")
    if ($1 ~ /^include/) { print "INCLUDES=1"; next }
    if ($1 == "vrrp_instance") { print "INSTANCE=" ++instance; peers=0; next }
    if ($1 == "unicast_peer" && $2 == "{") { peers=1; next }
    if ($1 == "}") { peers=0; next }
    if (peers && NF == 1 && length($1) <= 15 && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
        print "peer=" $1; next
    }
    if (NF != 2 || length($2) > 32) next
    if ($1 == "interface" && $2 ~ /^[a-zA-Z][a-zA-Z0-9_.:@-]*$/) print $1 "=" $2
    if ($1 == "unicast_src_ip" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) print $1 "=" $2
    if ($1 ~ /^(virtual_router_id|priority|advert_int)$/ && $2 ~ /^[0-9]+(\.[0-9]+)?$/) print $1 "=" $2
}'''


def extended_commands(device, check):
    if check in PROBE_CHECKS:
        tunnel = check in TUNNEL_CHECKS
        if tunnel:
            own, target = tunnel_endpoints(device,check)
        else:
            target = DEVICES[PING_TARGETS[check]]['address']
        args = ('ping', '-4', '-n', '-q', '-c', str(PING_COUNT), '-W', '1', target)
        command = ' '.join(shlex.quote(arg) for arg in args)
        # Route lookup and unbound ping run on the same source device, after identity verification.
        route = 'route=$(LC_ALL=C ip -4 route get ' + shlex.quote(target) + ' 2>/dev/null)'
        # Status 1 can be a valid zero- or partial-reply observation.
        guard = path_guard = ''
        if tunnel:
            skipped = '{ printf "ICMP_TUNNEL_PATH_MISMATCH=1\\n"; exit 0; }'
            guard = ("ip -o -4 addr show dev tun16 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "
                     +shlex.quote(own)+' || '+skipped+'\n')
            path_guard = 'case " $route " in *" dev tun16 "*) ;; *) '+skipped+' ;; esac\n'
        return (guard+route+' || { printf "ICMP_ROUTE_UNAVAILABLE=1\\n"; exit 0; }\n'
                'case " $route " in *" dev "*) ;; *) printf "ICMP_ROUTE_UNAVAILABLE=1\\n"; exit 0 ;; esac\n'
                +path_guard+'printf "ICMP_ROUTE=%s\\n" "$route"\nLC_ALL=C '+command+
                '\nrc=$?\n[ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]',)
    if check == 'policy_routes':
        return ('LC_ALL=C ip -4 rule show', 'LC_ALL=C ip -4 route show table all')
    if check == 'ha_config':
        return (f'if [ -r {KEEPALIVED_FILE} ]; then LC_ALL=C awk {shlex.quote(KEEPALIVED_AWK)} '
                f'{KEEPALIVED_FILE}; else printf "CONFIG_UNREADABLE=1\\n"; fi',)
    return None


def ping_counts(output):
    match = re.match(r'\AICMP_TX=([1-3])\nICMP_RX=([0-3])(?:\n|\Z)',output)
    if match and int(match[2]) <= int(match[1]):
        return int(match[1]), int(match[2])
    return None


def ping_route(output):
    fields={}
    for key,word in (('source','SRC'),('interface','DEV'),('gateway','VIA')):
        match=re.search(r'^ICMP_ROUTE_'+word+r'=([^\s]+)$',output,re.M)
        if match:fields[key]=match[1]
    return fields


def project_ping(output):
    if output.strip() in ('ICMP_ROUTE_UNAVAILABLE=1','ICMP_TUNNEL_PATH_MISMATCH=1'):
        return output.strip()+'\n'
    rows = re.findall(r'^(\d+) packets transmitted, (\d+) (?:packets )?received\b', output, re.M)
    if len(rows) == 1:
        sent, received = rows[0]
        safe = f'ICMP_TX={sent}\nICMP_RX={received}\n'
        if ping_counts(safe):
            route=re.search(r'^ICMP_ROUTE=(.*)$',output,re.M)
            if route:
                safe+='ICMP_MODE=kernel_route\n'
                for field,word in (('src','SRC'),('dev','DEV'),('via','VIA')):
                    value=re.search(r'(?:^|\s)'+field+r'\s+(\S+)',route[1])
                    if not value:continue
                    candidate=value[1]
                    if field=='dev':
                        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}',candidate):continue
                    else:
                        try:candidate=str(ipaddress.IPv4Address(candidate))
                        except ValueError:continue
                    safe+=f'ICMP_ROUTE_{word}={candidate}\n'
            return safe
    raise ValueError('invalid_ping_summary')
