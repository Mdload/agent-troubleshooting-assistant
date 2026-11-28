"""本地界面演练：固定规划器/工具替身、临时数据，禁止真实模型和SSH。"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.workflow_test_support import RunningApp, fake_planner, current_probe_fixture
import app
from tests.ha_case_test_support import VARIANTS,case_probe,case_planner


class PreviewHandler(app.Handler):
    def send(self,code,value,content_type='application/json; charset=utf-8'):
        if isinstance(value,bytes) and content_type.startswith('text/html'):
            banner=('<style>.layout,main,.sidebar{height:calc(100dvh - 32px)}</style><div role="status" style="height:32px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;padding:4px 10px;font-size:12px;text-align:center;background:#fff2c9;">'
                '离线界面演练：模型和设备均为测试替身，不代表现场状态。排障由提问者确认，无需管理员登录。</div>')
            value=value.replace(b'<body>',('<body>'+banner).encode('utf-8'),1)
        super().send(code,value,content_type)

    def do_GET(self):
        if self.path=='/api/health' and self.local_host():
            self.send(200,{'status':'ok','version':app.VERSION,'model_configured':True,
                'offline_preview':True,'admin_configured':True,'repair_enabled':False})
        else: super().do_GET()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=17871)
    parser.add_argument('--scenario',choices=VARIANTS,help='固定HA案例替身；不连接设备')
    parser.add_argument('--memory-fixture',action='store_true',help='创建临时记忆演练账号及长对话，不使用真实用户数据')
    parser.add_argument('--history-fixture',action='store_true',help='临时长历史及排除资格演练，完全合成')
    parser.add_argument('--citation-fixture',action='store_true',help='在合成HA回答中附带OBS引用，仅演练可读依据标签')
    args=parser.parse_args()
    if args.citation_fixture and not args.scenario:parser.error('citation fixture requires a synthetic scenario')
    if not 1<=args.port<=65535: parser.error('Invalid port')
    original_connect=socket.socket.connect
    original_popen=subprocess.Popen
    def local_connect(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','localhost','::1'):
            raise RuntimeError('Offline preview prohibits external or worker connections')
        return original_connect(sock,address)
    def no_ssh(command,*positional,**kwargs):
        parts=command if isinstance(command,(list,tuple)) else [command]
        if any(Path(str(p)).name.lower() in ('ssh','ssh.exe','sudo') for p in parts):
            raise RuntimeError('Offline preview prohibits device SSH')
        return original_popen(command,*positional,**kwargs)
    socket.socket.connect=local_connect;subprocess.Popen=no_ssh
    def planner(context):
        if context.get('rolling_context'):
            quote=context['rolling_context']['entries'][0]['quote']
            return {'action':'answer','answer':'离线自动接续演练：收到较早的用户目标原话：\n\n'+quote+
                    '\n\n这是确定性替身，用于验证背景传递，不代表真实模型质量。','refs':[],'basis':'general'},0
        if context.get('task_memory'):
            quote=context['task_memory']['entries'][0]['quote']
            return {'action':'answer','answer':'离线记忆演练：本次收到之前确认的目标原话：\n\n'+quote+
                    '\n\n这只是测试替身展示背景传递，未验证真实模型质量。','refs':[],'basis':'general'},0
        if context.get('workflow_policy',{}).get('mode')=='material_analysis':
            source=context['sources'][0]
            return {'action':'answer','answer':'离线材料演练，仅展示提取与引用。\n\n```text\n'+source['historical_finding'][:180]+
                    '\n```\n\n依据['+source['id']+']；未核验现场，其他原文区域仍需另查。',
                    'refs':[source['id']],'basis':'limited'},0
        action,usage=case_planner(context) if args.scenario and context.get('diagnostic_case') else fake_planner(context)
        if args.citation_fixture and action.get('action')=='answer' and action.get('refs'):
            action=dict(action,answer=action['answer']+'\n\n以上判断依据（'+'、'.join(action['refs'])+'）。\n\n代码示例中的 `OBS-1` 保持原样。')
        return action,usage
    client=RunningApp(planner,case_probe(args.scenario) if args.scenario else current_probe_fixture,configured=False)
    client.server.server_close()
    try:
        client.server=app.Server(('127.0.0.1',args.port),handler=PreviewHandler,
                                 workflow=client.service,admin_auth=client.auth)
        import threading
        client.thread=threading.Thread(target=client.server.serve_forever,daemon=True)
        client.base='http://127.0.0.1:'+str(args.port)
        if args.memory_fixture or args.history_fixture:
            from server.user_accounts import AccountStore
            token,account=client.server.get_accounts().register('memory_preview','offline-password-only-123',True)
            messages=[{'role':'user','content':'我的学习目标是看懂Python字典，先写小实验，不换框架。EARLY_GOAL'}]
            messages += [{'role':'assistant' if i%2==0 else 'user','content':'第'+str(i+1)+'轮合成练习记录'} for i in range(12)]
            if args.history_fixture:
                messages=[{'role':'user','content':'Python字典的学习目标','context':True,'contextRevision':0},
                    {'role':'assistant','content':'合成长历史标记 '+('history filler '*250),'context':True,'contextRevision':0},
                    {'role':'user','content':'EXCLUDED_FAILED_TURN，仅供查看的失败轮次','context':False,'contextRevision':0}]
            client.server.get_history().put(AccountStore.owner(account),{'id':'memory-preview','title':'长对话任务记忆演练',
                'messages':messages,'expected_revision':0})
        with client:
            print(json.dumps({'url':client.base,'kind':'offline_ui_preview','model_api_calls':0,
                              'live_device_reads':0}),flush=True)
            # 在终端输入stop退出；临时审批库随进程正常退出清理。
            for line in sys.stdin:
                if line.strip()=='stop':break
    finally:
        socket.socket.connect=original_connect;subprocess.Popen=original_popen
        client.temp.cleanup()


if __name__=='__main__': main()
