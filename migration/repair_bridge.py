"""到140固定修复入口；SSH命令与目标不由网页或模型传入。"""
import json
import re
import subprocess
from devices.device_registry import HOPS

def execute_repair(request):
    if (not isinstance(request,dict) or set(request)!={'operation','lease','id'} or request['operation'] not in ('start_keepalived','inspect_keepalived')
            or any(not isinstance(request[k],str) or not re.fullmatch('[a-f0-9]{32}',request[k]) for k in ('lease','id'))):
        raise ValueError('Invalid repair request')
    command=['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=6','-o','ConnectionAttempts=1',
        '-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no','-o','ForwardAgent=no','-o','IdentitiesOnly=yes',
        '-i','/etc/ata/example-worker-key',HOPS['jump_host']['user']+'@'+HOPS['jump_host']['address'],
        '/usr/bin/python3 -I -B /usr/local/lib/ata-repair/dispatch.py']
    try:
        run=subprocess.run(command,input=json.dumps(request).encode()+b'\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=28)
        if len(run.stdout)>4096:raise ValueError('repair_transport_failed')
        result=json.loads(run.stdout.decode('utf-8'))
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise ValueError('invalid_repair_result')
        if run.returncode and result.get('status') not in ('refused','dispatching'):raise ValueError('repair_transport_failed')
        return result
    except (OSError,ValueError,subprocess.TimeoutExpired):return {'ok':False,'status':'unknown','error':'repair_transport_unconfirmed'}

def inspect_repair(request):
    if request.get('operation')!='inspect_keepalived':raise ValueError('Inspection only')
    return execute_repair(request)
