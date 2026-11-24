"""七个Croc案例的正式接口合成回放：确定性替身，零模型/设备调用。"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workflow_test_support import RunningApp
from ha_case_test_support import VARIANTS,case_probe,case_planner

EXPECTED={'nonowner_stopped':('non_owner_stop_expected',2),'owner_stopped':('owner_service_stopped',3),
          'nonowner_running':('non_owner_service_running',3),'owner_running':('owner_service_running',4),
          'partial_vip':('partial_vip',2),'missing_unit':('insufficient',2),'read_failure':('insufficient',2)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',help='可选新JSON文件，不覆盖历史结果')
    args=parser.parse_args();output=Path(args.output).resolve() if args.output else None
    if output and output.exists():parser.error('结果已存在，请使用新路径')
    original_connect=socket.socket.connect;original_popen=subprocess.Popen
    def offline_connect(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','localhost','::1'):
            raise RuntimeError('Replay only permits loopback TCP')
        return original_connect(sock,address)
    def no_ssh(command,*positional,**kwargs):
        parts=command if isinstance(command,(list,tuple)) else [command]
        if any(Path(str(p)).name.lower() in ('ssh','ssh.exe','sudo') for p in parts):
            raise RuntimeError('Replay prohibits device SSH')
        return original_popen(command,*positional,**kwargs)
    socket.socket.connect=offline_connect;subprocess.Popen=no_ssh
    rows=[]
    try:
        for variant in VARIANTS:
            with RunningApp(case_planner,case_probe(variant),lookup=lambda q:[]) as client:
                code,requested=client.diagnostic_chat('局端备机当前Croc业务不通')
                if code!=200 or requested.get('status')!='approval_required': raise RuntimeError('Request failed')
                task=requested['task']['id']
                if client.login()[0]!=200 or client.approve(task)[0]!=200: raise RuntimeError('Approval failed')
                code,finished=client.resume(task)
                result=finished.get('result',{});case=result.get('diagnostic_case',{})
                finding=case.get('nodes',[{}])[0].get('finding')
                if code!=200 or finished['status']!='completed' or (finding,finished['attempts'])!=EXPECTED[variant]:
                    raise RuntimeError('Unexpected fixture outcome: '+variant)
                rows.append({'variant':variant,'finding':finding,'read_attempts':finished['attempts'],
                    'tool_sequence':[e['check'] for e in result['evidence']],
                    'root_cause_confirmed':case['root_cause_confirmed'],'repair_executed':case['repair_executed']})
    finally:
        socket.socket.connect=original_connect;subprocess.Popen=original_popen
    report={'kind':'deterministic_synthetic_worker_contract_replay','not_model_accuracy':True,
            'model_api_calls':0,'live_device_reads':0,'scenarios':rows}
    if output:
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
