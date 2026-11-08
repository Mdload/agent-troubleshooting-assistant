"""一键离线测试：禁止外网与SSH；保留平台跳过，不把跳过算通过。"""
import argparse
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',help='新的JSON报告文件；不覆盖已有结果')
    args=parser.parse_args()
    output=Path(args.output).resolve() if args.output else None
    if output and output.exists():
        parser.error('结果已存在，请使用新文件路径')
    manifest=json.loads((ROOT/'scripts/test-suite.json').read_text(encoding='utf-8'))
    missing=[name for name in manifest['modules'] if not (ROOT/(name+'.py')).is_file()]
    if missing:
        parser.error('测试模块缺失：'+','.join(missing))
    original_connect=socket.socket.connect
    original_popen=subprocess.Popen
    def offline_connect(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
            raise RuntimeError('Offline tests prohibit external network access')
        if isinstance(address,(str,bytes)) and ('ata-device-worker.sock' in str(address) or '/run/ata-example/' in str(address)):
            raise RuntimeError('Offline tests prohibit production worker access')
        return original_connect(sock,address)
    def offline_popen(command,*positional,**kwargs):
        parts=command if isinstance(command,(list,tuple)) else [command]
        if any(Path(str(part)).name.lower() in ('ssh','ssh.exe','sudo') for part in parts):
            raise RuntimeError('Offline tests prohibit device SSH access')
        return original_popen(command,*positional,**kwargs)
    socket.socket.connect=offline_connect
    subprocess.Popen=offline_popen
    old_dir=os.getcwd()
    os.chdir(str(ROOT))
    try:
        suite=unittest.defaultTestLoader.loadTestsFromNames(manifest['modules'])
        discovered=suite.countTestCases()
        if discovered!=manifest['expected_tests']:
            raise RuntimeError('Expected %s tests; discovered %s'%(manifest['expected_tests'],discovered))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        os.chdir(old_dir)
        socket.socket.connect=original_connect
        subprocess.Popen=original_popen
    report={'kind':'offline_engineering_tests','not_model_accuracy':True,
            'created_at_utc':datetime.datetime.utcnow().isoformat()+'Z',
            'python':sys.version.split()[0],'platform':sys.platform,
            'tests_run':result.testsRun,'passed':result.testsRun-len(result.skipped)-len(result.failures)-len(result.errors),
            'skipped':[{'test':str(test),'reason':reason} for test,reason in result.skipped],
            'failures':len(result.failures),'errors':len(result.errors),
            'successful':result.wasSuccessful(),'model_api_calls':0,'live_device_reads':0}
    if output:
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':
    sys.exit(main())
