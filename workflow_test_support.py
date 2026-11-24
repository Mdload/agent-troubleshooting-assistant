"""正式HTTP入口的隔离测试夹具；临时数据和测试口令均不用于实际服务。"""
import json
import datetime
import tempfile
import threading
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor
from urllib.error import HTTPError
from http.cookiejar import CookieJar
import app
from admin_auth import AdminAuth, password_record
from approval_store import ApprovalStore, DIAGNOSTIC_DATA_USE
from lab_workflow import WorkflowService
from device_tools import DEVICES

TEST_PASSWORD = 'test-only-password'
TEST_LAB_PASSWORD = 'offline-lab-registration'


class RunningApp:
    def __init__(self, planner=None, probe=None, lookup=None, configured=True,prefetch_overview=False,registration_password=TEST_LAB_PASSWORD):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ApprovalStore(self.root/'workflow.db')
        self.auth = AdminAuth(self.root/'admin.json')
        if configured:
            self.auth.path.write_text(json.dumps(password_record(TEST_PASSWORD)),encoding='utf-8')
        kwargs = {} if lookup is None else {'lookup':lookup}
        self.service = WorkflowService(self.store,planner,probe,prefetch_overview=prefetch_overview,**kwargs)
        self.server = app.Server(('127.0.0.1',0),workflow=self.service,admin_auth=self.auth)
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.opener = self.new_browser()
        self.registration_password=registration_password

    def new_browser(self):
        return build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()))

    def __enter__(self):
        self.thread.start(); self.call('/api/session'); return self

    def __exit__(self, *args):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def call(self,path,data=None,browser=None,token=True,origin=None,timeout=5):
        # 既有历史/排障测试默认携带夹具实验室口令；注册边界测试用None关闭此便捷准备。
        if path=='/api/account/register' and self.registration_password is not None and data is not None:
            accounts=self.server.get_accounts()
            if not accounts.registration()['configured']:accounts.configure_registration(self.registration_password,0)
            data=dict(data);data.setdefault('lab_password',self.registration_password)
        headers={}
        if data is not None:
            headers['Content-Type']='application/json'
            if token: headers['X-ATA-Token']=app.CSRF_TOKEN
            headers['Origin']=self.base if origin is None else origin
        request=Request(self.base+path,headers=headers,
            data=json.dumps(data).encode('utf-8') if data is not None else None)
        try:
            with (browser or self.opener).open(request,timeout=timeout) as response:
                return response.status,json.load(response)
        except HTTPError as error:
            return error.code,json.load(error)

    def chat(self,question='局端备机当前状态',consent=True,history=None,conversation_id=None):
        data={'question':question,'allow_context':consent,'history':history or []}
        if conversation_id is not None:data['conversation_id']=conversation_id
        state=self.call('/api/conversations/'+(conversation_id or 'default')+'/mode')[1]
        data.update(interaction_mode=state['mode'],interaction_revision=state['revision'])
        return self.call('/api/chat',data)

    def enter_diagnostic(self,conversation_id='default',scene='ha_topology'):
        return self.call('/api/conversations/'+conversation_id+'/mode',{'mode':'diagnostic','scene':scene})

    def diagnostic_chat(self,question='局端备机当前状态',consent=True,history=None,conversation_id=None):
        conversation=conversation_id or 'default'
        state=self.call('/api/conversations/'+conversation+'/mode')[1]
        if state['mode']!='diagnostic':
            code,_=self.enter_diagnostic(conversation)
            if code!=200: raise AssertionError('Test diagnostic entry failed')
        return self.chat(question,consent,history,conversation_id)

    def login(self):
        return self.call('/api/admin/login',{'password':TEST_PASSWORD})

    def approve(self,task_id,approved=True):
        return self.call('/api/tasks/'+task_id+'/decision',dict(approved=approved,**({'data_use':DIAGNOSTIC_DATA_USE} if approved else {})))

    def resume(self,task_id,consent=True):
        return self.call('/api/tasks/'+task_id+'/resume',{'allow_context':consent,'allow_live_data':consent})

def fake_planner(context):
    """确定性替身，仅演练控制流程，不作为真实模型回答质量。"""
    observations=context['observations']
    catalog=context['tool_catalog']
    if catalog and not observations:
        device=next(iter(catalog))
        if 'overview' in catalog[device]:
            return {'action':'read_overview','device':device},0
        return {'action':'read','device':device,'check':next(iter(catalog[device]['checks']))},0
    if observations:
        good=[o for o in observations if o.get('ok') is True]
        refs=[o['id'] for o in good]
        text=('演练已取得'+str(len(good))+'项合成观测；只证明对应读取流程完成。'
              '尚未验证真实设备、业务质量或故障根因。') if good else '演练读取失败，本轮状态未知，不能推断设备损坏。'
        return {'action':'answer','answer':text,'refs':refs,'basis':'evidence' if refs else 'limited'},0
    sources=context['sources']
    if sources:
        first=sources[0]
        excerpt=first.get('historical_finding','').strip()[:220]
        return {'action':'answer','answer':'资料摘录（未调用真实模型）：\n\n'+excerpt,
                'refs':[first['id']],'basis':'evidence'},0
    question=context['question'].lower()
    if '默认路由' in question:
        text='一般原理：默认路由是没有更具体路由匹配时使用的下一跳。它不等于本实验室的实际转发路径。'
    elif '列表' in question:
        text='Python列表按顺序存放元素，可以增加、删除或修改元素。'
    else: text='已分到通用技术问答。本地演练未调用真实模型，仅展示分流和权限流程。'
    return {'action':'answer','answer':text,'refs':[],'basis':'general'},0


def fake_probe(device,check):
    return {'ok':True,'output':'SIMULATION：固定合成状态，非现场数据。',
            'collected_at':'SIMULATION','origin':'synthetic','collector_utc':'2026-10-08T00:00:00Z'}


def current_probe_fixture(device,check):
    """仅测试worker响应的字段契约，不连接设备、不构成真实现场验证。"""
    return {'ok':True,'address':DEVICES[device][0],'output':'TEST FIXTURE: active',
            'collected_at':datetime.datetime.utcnow().isoformat()+'Z','clock_source':'collector_utc'}
