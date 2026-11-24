"""从固定登记生成桥接白名单；构造文本不会启动SSH或Unix服务。"""
from device_registry import DEVICES, HOPS, checks_for

REMOTE_CODE = '''import json,os,pwd,socket,sys
assert socket.gethostname() == __HOSTNAME__
assert os.getuid() == __UID__ and pwd.getpwuid(os.getuid()).pw_name == __USER__
SCOPES = __SCOPES__
line = sys.stdin.buffer.readline(1025)
assert len(line) <= 1024 and line.endswith(b'\\n')
request = json.loads(line.decode('utf-8'))
health = request == {'operation':'health'}
if health:
    payload = {'device':'__migration_health__','check':'services'}
elif request == {'operation':'capabilities'}:
    payload = request
else:
    assert isinstance(request,dict) and set(request) == {'device','check'}
    assert isinstance(request['device'],str) and isinstance(request['check'],str)
    assert request['device'] in SCOPES and request['check'] in SCOPES[request['device']]
    payload = request
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
    connection.settimeout(23)
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
               .replace('__SCOPES__',repr({key:checks_for(key) for key in DEVICES})))
