"""从固定登记生成桥接白名单；构造文本不会启动SSH或Unix服务。"""
from devices.device_registry import (DEVICES, HOPS, PROBE_KINDS, PROBE_PORT_PATTERN,
                             PROBE_TARGET_PATTERN, COMMAND_CHARACTERS, MAX_COMMAND_LENGTH, MAX_REQUEST_BYTES, checks_for, repair_scopes)

REMOTE_CODE = '''import json,os,pwd,re,socket,sys
assert socket.gethostname() == __HOSTNAME__
assert os.getuid() == __UID__ and pwd.getpwuid(os.getuid()).pw_name == __USER__
SCOPES = __SCOPES__
REPAIR_SCOPES = __REPAIR_SCOPES__
line = sys.stdin.buffer.readline(__MAX_REQUEST_BYTES__ + 1)
assert len(line) <= __MAX_REQUEST_BYTES__ and line.endswith(b'\\n')
request = json.loads(line.decode('utf-8'))
health = request == {'operation':'health'}
if health:
    payload = {'device':'__migration_health__','check':'services'}
elif request == {'operation':'capabilities'}:
    payload = request
elif isinstance(request,dict) and set(request) == {'operation','device','action','target'}:
    device, action, target = request['device'], request['action'], request['target']
    assert isinstance(device,str) and device in REPAIR_SCOPES
    assert isinstance(action,str) and isinstance(target,str)
    if action == 'run_command':
        assert request['operation'] == 'manual_command' and action in REPAIR_SCOPES[device]
        assert 1 <= len(target) <= __MAX_COMMAND_LENGTH__
        assert re.fullmatch(__COMMAND_CHARACTERS__, target)
    else:
        assert request['operation'] == 'approved_repair'
        if action in ('add_route', 'del_route'):
            # The bridge and worker apply the complete address/interface policy.
            assert re.fullmatch(r'[0-9.]+/[0-9]+ via [0-9.]+ dev [a-z0-9]+', target)
        else:
            assert target in REPAIR_SCOPES[device].get(action, ())
    payload = request
elif isinstance(request,dict) and set(request) == {'device','kind','target'}:
    device, kind, target = request['device'], request['kind'], request['target']
    assert isinstance(device,str) and device in SCOPES
    assert kind in __PROBE_KINDS__ and isinstance(target,str)
    if kind == 'port':
        assert re.fullmatch(__PROBE_PORT_PATTERN__,target) and 1 <= int(target) <= 65535
    else:
        assert re.fullmatch(__PROBE_TARGET_PATTERN__,target)
    payload = request
else:
    assert isinstance(request,dict) and set(request) == {'device','check'}
    assert isinstance(request['device'],str) and isinstance(request['check'],str)
    assert request['device'] in SCOPES and request['check'] in SCOPES[request['device']]
    payload = request
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
    connection.settimeout(101 if request.get('action') == 'run_command' else 23)
    connection.connect('/run/ata-diagnostics/worker.sock')
    connection.sendall(json.dumps(payload).encode() + b'\\n')
    with connection.makefile('rb') as stream:
        response = stream.readline(65537)
assert len(response) <= 65536 and response.endswith(b'\\n')
result = json.loads(response.decode('utf-8'))
assert isinstance(result,dict) and type(result.get('ok')) is bool
if health:
    assert result['ok'] is False and result.get('error') == 'worker_request_failed'
    result = {'ok':True,'kind':'worker_transport','device_reads':0,
              'host':__HOSTNAME__,'user':__USER__}
encoded = json.dumps(result).encode() + b'\\n'
assert len(encoded) <= 65536
sys.stdout.buffer.write(encoded)
'''
REMOTE_CODE = (REMOTE_CODE.replace('__HOSTNAME__',repr(HOPS['jump_host']['hostname']))
               .replace('__UID__',repr(HOPS['jump_host']['uid']))
               .replace('__USER__',repr(HOPS['jump_host']['user']))
               .replace('__PROBE_KINDS__',repr(PROBE_KINDS))
               .replace('__PROBE_PORT_PATTERN__',repr(PROBE_PORT_PATTERN))
               .replace('__PROBE_TARGET_PATTERN__',repr(PROBE_TARGET_PATTERN))
               .replace('__MAX_REQUEST_BYTES__',repr(MAX_REQUEST_BYTES))
               .replace('__COMMAND_CHARACTERS__',repr(COMMAND_CHARACTERS))
               .replace('__MAX_COMMAND_LENGTH__',repr(MAX_COMMAND_LENGTH))
               .replace('__SCOPES__',repr({key:checks_for(key) for key in DEVICES}))
               .replace('__REPAIR_SCOPES__',repr({key:repair_scopes(key) for key in DEVICES})))
