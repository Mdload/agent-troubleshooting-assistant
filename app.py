#!/usr/bin/env python3
"""正式聊天入口：知识问答、逐任务审批和受控只读诊断；仅监听回环。"""
import argparse
import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import qa
import unified_chat
from admin_auth import AdminAuth
from approval_store import ApprovalStore, TaskError, SINGLE_APPROVAL
from lab_workflow import WorkflowService
from diagnostic_core import audit_text, request_trace
from diagnostic_logs import filter_log_output
from question_routing import classify
from conversation_topic import validate_conversation_id
from interaction_mode import scene_catalog
from device_registry import public_catalog
from version import VERSION
from experience_store import ExperienceStore, ExperienceError
from user_accounts import AccountStore,AccountError,SESSION_SECONDS
from chat_history import HistoryStore,HistoryError
from text_materials import MaterialStore,MaterialError
from task_memory import TaskMemory,MemoryError
from rolling_context import RollingError
from public_evidence import PublicEvidenceError
import investigation_history
from selection_explain import stream_explanation
from chat_model import ModelFailure
from repair_control import RepairManager
from context_history import prepare_history
from request_limits import RequestLimiter, category as request_category
from agent_memory import AgentMemoryStore
from agent_v2 import AutonomousAgent

ROOT = Path(__file__).resolve().parent
CSRF_TOKEN = secrets.token_urlsafe(32)
TASK_PATH = re.compile(r'^/api/(admin/)?tasks/([0-9a-f]{32})(?:/(resume|start|cancel|decision|input))?$')
MODE_PATH = re.compile(r'^/api/conversations/([A-Za-z0-9_-]{1,80})/mode$')
EXPERIENCE_PATH = re.compile(r'^/api/(admin/)?experiences/([a-f0-9]{32})(?:/(edit|review|publish|withdraw))?$')
HISTORY_PATH = re.compile(r'^/api/history/([A-Za-z0-9_-]{1,80})(?:/(delete))?$')
MATERIAL_PATH = re.compile(r'^/api/materials/([a-f0-9]{32})(?:/(delete))?$')
MATERIAL_LIST = re.compile(r'^/api/conversations/([A-Za-z0-9_-]{1,80})/materials$')
INVESTIGATION_PATH = re.compile(r'^/api/conversations/([A-Za-z0-9_-]{1,80})/investigations(?:/([a-f0-9]{32}))?$')
MEMORY_PATH = re.compile(r'^/api/conversations/([A-Za-z0-9_-]{1,80})/memory(?:/(draft|withdraw|rolling))?$')
REPAIR_PATH = re.compile(r'^/api/repairs/([a-f0-9]{32})(?:/(decision|execute|cancel|reconcile))?$')
REPAIR_WINDOW_PATH = re.compile(r'^/api/(admin/)?repair-windows/([a-f0-9]{32})(?:/(decision|revoke))?$')
EXPERIENCE_ERRORS = {'experience_not_found':'找不到该经验或没有访问权限',
    'experience_consent_required':'请确认所选纠正用于管理员审核及审核后共享知识',
    'experience_changed':'内容已改变，请刷新后重新核对',
    'experience_source_changed':'来源已变化或不可核对，请修改草稿并重新审核',
    'experience_not_pending':'该草稿已处理，请刷新状态',
    'experience_not_approved':'请先完成来源与适用条件审核',
    'experience_not_published':'该经验尚未发布',
    'experience_regression_failed':'检索或直接支持范围检查未通过，请修改草稿或回归问题',
    'experience_duplicate':'本浏览器已有相同内容的草稿',
    'experience_limit':'经验数量已达上限，请联系管理员整理'}
ERRORS = {'not_found':'找不到该申请或没有访问权限', 'not_approved':'申请尚未批准或已失效',
    'repair_device_not_armed':'设备端独立许可尚未开启、已使用或即将到期；网页批准不能越过设备许可。',
    'repair_device_lease_used':'这张设备许可已绑定过申请，请使用新的维护许可。',
    'repair_window_not_pending':'本次修复权限申请已处理或过期，请刷新。',
    'repair_task_not_pending':'请在本轮只读排障开始前申请修复权限，并同时选择远端主备服务与接口。',
    'repair_not_reviewable':'当前状态不能核查；执行中请等待，核查不会重新下发命令。',
    'repair_inspector_unavailable':'设备执行记录核查通道尚未就绪。',
    'repair_window_inactive':'本次修复权限未批准、已撤销或已过期。',
    'repair_not_approved':'请先确认本次具体修复方案。',
    'rolling_context_changed':'自动接续内容已被其他窗口更新，请刷新后再操作',
    'rolling_context_not_active':'自动接续已关闭、到期或原对话改变，本轮已停止使用旧背景',
    'data_scope_required':'本申请尚未取得对应数据用途的范围批准；旧申请请重新提交。',
    'memory_confirmation_required':'请核对原话并确认保存任务记忆',
    'memory_changed':'任务记忆已被另一窗口更新，请刷新后核对',
    'memory_source_changed':'原对话已改变，请重新提取并核对原话',
    'memory_not_active':'任务记忆已撤回、到期或来源改变，请重新核对',
    'memory_qa_only':'任务记忆目前仅供普通问答；设备调查和材料分析使用各自来源',
    'investigation_not_ended':'调查尚未结束，请在本次调查卡片查看进度',
    'material_not_found':'材料不存在、已过期或不属于本次对话',
    'material_type_not_supported':'目前只接受TXT/LOG纯文本，不解析PDF、图片或压缩包',
    'material_invalid_text':'需要非空UTF-8文本，不能包含二进制或控制字符；请转换编码后再试',
    'material_too_large':'单份材料须为1字节至256KB，超出请拆分，原输入仍保留',
    'material_owner_limit':'24小时内最多保存10份且合计1MB，请先删除不需要的材料',
    'material_store_full':'材料存储暂满，请稍后重试',
    'material_consent_required':'请确认所选材料的相关片段可发送给Inferera分析',
    'account_required':'请先登录个人账号', 'account_session_expired':'账号会话已失效，请重新登录',
    'account_invalid_credentials':'账号或密码不正确', 'account_unavailable':'该用户名暂不可用',
    'account_storage_consent':'请确认将登录后的文字对话保存在服务器',
    'lab_password_required':'注册时请输入实验室口令', 'lab_password_invalid':'实验室口令不正确',
    'lab_password_not_configured':'请先让已有账号的用户登录，在个人账号中设置实验室注册口令',
    'lab_password_changed':'实验室口令已被更新，请刷新后重试',
    'account_login_limited':'登录或注册尝试过多，请15分钟后再试', 'account_limit':'最多支持10个个人账号，请使用已有账号登录',
    'history_not_found':'找不到该对话或没有访问权限',
    'history_changed':'另一窗口已更新对话，请先导出本页未保存内容，再重新载入历史',
    'history_count_limit':'对话数量已达上限，请先导出并删除旧对话',
    'history_size_limit':'对话过长，尚未保存，请先导出并开始新对话',
    'comparison_same_task':'请选择两次不同的调查',
    'comparison_different_context':'只比较同一对话、同一场景的两次调查',
    'comparison_not_ready':'两次调查均须结束且有结果后才能比较',
    'comparison_invalid_task':'调查标识无效，请刷新原申请后重试',
    'not_pending':'申请已经处理或已过期', 'not_cancellable':'该申请已结束',
    'task_input_closed':'本次调查已结束接收补充', 'task_input_limit':'本次调查的补充已达上限',
    'task_input_changed':'补充消息与已保存内容不一致',
    'scope_changed':'申请范围发生变化，请重新申请', 'busy':'已有请求正在处理，请稍后再试',
    'too_many_pending':'待处理申请过多，请先取消旧申请',
    'mode_changed':'对话模式已改变，请刷新模式后重试',
    'topic_changed':'对话已有更新，请按最新内容继续提问',
    'diagnostic_required':'请主动进入只读排障并重新申请检查范围',
    'scene_unavailable':'该场景的设备和工具尚未接入',
    'scene_scope_mismatch':'设备不在所选场景范围',
    'conversation_limit':'本浏览器排障对话数量已达上限，请沿用已有对话',
    'consent_required':'请确认本次问题及批准范围内现场数据的使用',
    'session_required':'浏览器会话已失效，请刷新页面', 'admin_required':'请先登录管理员',
    'admin_not_configured':'管理员尚未设置，请由作者在项目终端运行 python -B admin_auth.py',
    'invalid_credentials':'管理员口令不正确', 'login_limited':'登录尝试过多，请15分钟后再试'}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup(); self.connection.settimeout(300); self.response_cookies = []

    def log_message(self, fmt, *args):
        pass  # 不记录问题、口令、配置或上游响应。

    def send(self, code, value, content_type='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src http://127.0.0.1:17875; frame-ancestors 'none'")
        for cookie in self.response_cookies: self.send_header('Set-Cookie', cookie)
        self.response_cookies = []
        self.end_headers(); self.wfile.write(body)

    def cookie(self, name):
        try:
            jar = SimpleCookie(); jar.load(self.headers.get('Cookie', ''))
            return jar[name].value if name in jar else ''
        except Exception: return ''

    def set_cookie(self, name, token, age):
        self.response_cookies.append(name+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(age))

    def owner(self, create=False):
        if self.cookie('ata_account'):
            account=self.server.get_accounts().resolve(self.cookie('ata_account'))
            if not account:raise AccountError('account_session_expired')
            return AccountStore.owner(account)
        token = self.cookie('ata_session')
        store = self.server.get_workflow().store
        if re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            owner = hashlib.sha256(token.encode('ascii')).hexdigest()
            if store.has_session(owner): return owner
        if not create: raise TaskError('session_required')
        token = secrets.token_urlsafe(32)
        owner = hashlib.sha256(token.encode('ascii')).hexdigest()
        store.add_session(owner); self.set_cookie('ata_session', token, 86400)
        return owner

    def account_owner(self):
        if not self.cookie('ata_account'):raise AccountError('account_required')
        return self.owner()

    def end_identity(self):
        try:
            owner=self.owner();workflow=self.server.get_workflow()
            workflow.repairs.permissions.revoke_owner(owner);workflow.store.end_identity(owner)
        except (TaskError,AccountError):pass
        self.server.get_accounts().logout(self.cookie('ata_account'))
        self.server.admin_auth.logout(self.cookie('ata_admin'))
        for name in ('ata_account','ata_admin','ata_session'):self.set_cookie(name,'',0)

    def identity_guard(self,owner):
        token=self.cookie('ata_account')
        def active():
            if token:
                account=self.server.get_accounts().resolve(token)
                if not account or AccountStore.owner(account)!=owner:raise unified_chat.ToolRejection('account_session_expired')
            elif not self.server.get_workflow().store.has_session(owner):
                raise unified_chat.ToolRejection('investigation_inactive')
        return active

    def require_admin(self):
        if not self.server.admin_auth.valid(self.cookie('ata_admin')):
            raise TaskError('admin_required')

    def task_error(self, error):
        code = str(error)
        status = 404 if code in ('not_found','experience_not_found','history_not_found','material_not_found') else 401 if code in ('admin_required','session_required','invalid_credentials','account_required','account_session_expired','account_invalid_credentials') else 429 if code in ('busy','login_limited','experience_limit','account_login_limited','account_limit','material_owner_limit','material_store_full') else 409
        self.send(status, {'error':EXPERIENCE_ERRORS.get(code,ERRORS.get(code,'请求未通过校验')), 'reason':code})

    def do_GET(self):
        if not self.local_host(): self.send(403, {'error':'请通过本机SSH隧道访问'}); return
        path = self.path.split('?', 1)[0]
        try:
            if path == '/':
                if not self.cookie('ata_account'):self.owner(create=True)
                page = (ROOT/'web/index.html').read_text(encoding='utf-8').replace('__CSRF_TOKEN__', CSRF_TOKEN).replace('__APP_VERSION__', VERSION)
                page=page.replace('__ACCOUNT_SCRIPT__',(ROOT/'web/account-ui.js').read_text(encoding='utf-8'))
                page=page.replace('__MATERIAL_SCRIPT__',(ROOT/'web/material-ui.js').read_text(encoding='utf-8'))
                page=page.replace('__WORKSPACE_STYLE__',(ROOT/'web/workspace.css').read_text(encoding='utf-8'))
                page=page.replace('__REFINED_STYLE__',(ROOT/'web/refined.css').read_text(encoding='utf-8'))
                page=page.replace('__WORKSPACE_SCRIPT__',(ROOT/'web/workspace-ui.js').read_text(encoding='utf-8'))
                page=page.replace('__MEMORY_SCRIPT__',(ROOT/'web/memory-ui.js').read_text(encoding='utf-8'))
                page=page.replace('__SELECTION_FOLLOWUP_SCRIPT__',(ROOT/'web/selection-followup.js').read_text(encoding='utf-8'))
                page=page.replace('__SELECTION_STREAM_SCRIPT__',(ROOT/'web/selection-stream.js').read_text(encoding='utf-8'))
                page=page.replace('__REPAIR_SCRIPT__',(ROOT/'web/repair-ui.js').read_text(encoding='utf-8'))
                page=page.replace('__SELECTION_SCRIPT__',(ROOT/'web/selection-ui.js').read_text(encoding='utf-8'))
                self.send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
            elif path == '/api/health':
                self.send(200, {'project':'Agent-Troubleshooting-Assistant', 'version':VERSION,
                    'status':'ok', 'case_count':len(qa.CASES), 'knowledge_count':len(qa.KNOWLEDGE),
                    'model_configured':qa.model_ready(), 'device_execution_enabled':True,
                    'device_approval_required':True, 'device_approval_policy':'requester_per_task',
                    'automatic_read_only_enabled':self.server.get_workflow().auto_read_only,
                    'admin_configured':self.server.admin_auth.configured(),
                    'repair_enabled':self.server.get_workflow().repairs.available()})
            elif path == '/api/diagnostic-scenes':
                self.send(200, {'scenes':scene_catalog(), 'repair_enabled':self.server.get_workflow().repairs.available()})
            elif path == '/api/devices':
                self.owner();self.send(200,public_catalog())
            elif REPAIR_PATH.fullmatch(path) and REPAIR_PATH.fullmatch(path).group(2) is None:
                self.send(200,self.server.get_workflow().repairs.get(REPAIR_PATH.fullmatch(path).group(1),self.owner()))
            elif path in ('/api/repair-windows','/api/admin/repair-windows'):
                owner=self.account_owner()
                if path.startswith('/api/admin/'):self.require_admin();owner=None
                self.send(200,{'windows':self.server.get_workflow().repairs.permissions.list(owner)})
            elif REPAIR_WINDOW_PATH.fullmatch(path):
                admin,ident,action=REPAIR_WINDOW_PATH.fullmatch(path).groups()
                if action:self.send(404,{'error':'接口不存在'});return
                owner=self.account_owner()
                if admin:self.require_admin();owner=None
                self.send(200,self.server.get_workflow().repairs.permissions.get(ident,owner))
            elif MODE_PATH.fullmatch(path):
                conversation = MODE_PATH.fullmatch(path).group(1)
                self.send(200, self.server.get_workflow().store.interaction(self.owner(),conversation,include_active=True))
            elif path == '/api/session':
                self.owner(create=True); self.send(200, {'status':'ok'})
            elif path == '/api/account':
                account=self.server.get_accounts().resolve(self.cookie('ata_account'))
                self.send(200,{'authenticated':bool(account),'account':account,
                    'expired':bool(self.cookie('ata_account')) and not account})
            elif path == '/api/account/registration':
                self.send(200,self.server.get_accounts().registration())
            elif path == '/api/history':
                owner=self.account_owner()
                chats=self.server.get_history().list(owner)
                interactions=self.server.get_workflow().store.interactions(owner)
                for chat in chats:
                    if chat['id'] in interactions:chat['interaction']=interactions[chat['id']]
                self.send(200,{'chats':chats})
            elif MATERIAL_LIST.fullmatch(path):
                conversation=MATERIAL_LIST.fullmatch(path).group(1)
                self.send(200,{'materials':self.server.get_materials().list(self.owner(),conversation)})
            elif MEMORY_PATH.fullmatch(path):
                conversation,action=MEMORY_PATH.fullmatch(path).groups()
                if action=='rolling':
                    self.send(200,self.server.get_task_memory().rolling.get(self.account_owner(),conversation));return
                if action:self.send(404,{'error':'接口不存在'});return
                owner=self.owner()
                mem_store=self.server.get_agent_memory()
                summary_row=mem_store.get_summary(owner,conversation)
                granted=mem_store.granted_devices(owner)
                catalog_map={x['id']:x['label'] for x in public_catalog()['devices']}
                agent_payload={
                    'granted_devices':[{'id':d,'label':catalog_map.get(d,d)} for d in granted],
                    'summary':summary_row['summary'] if summary_row else '',
                    'memories':mem_store.list_memories(owner,limit=80),
                    'shared_memories':mem_store.list_shared_memories(owner=owner,limit=60),
                }
                progress_payload=self.server.get_agent_v2().get_progress(owner,conversation) if (self.server.agent_v2 is not None or self.server.agent_v2_enabled) else {'active':False,'stage':'idle','text':'','elapsed_seconds':0,'steps':[]}
                if self.cookie('ata_account'):
                    legacy=self.server.get_task_memory().get(owner,conversation)
                    legacy['agent_memory']=agent_payload
                    legacy['live_progress']=progress_payload
                    self.send(200,legacy)
                else:
                    self.send(200,{'state':'empty','revision':0,'entries':[],'agent_memory':agent_payload,'live_progress':progress_payload})
            elif INVESTIGATION_PATH.fullmatch(path):
                conversation,ident=INVESTIGATION_PATH.fullmatch(path).groups()
                store=self.server.get_workflow().store;owner=self.owner()
                self.send(200,investigation_history.get_record(store,owner,conversation,ident) if ident
                    else investigation_history.list_records(store,owner,conversation))
            elif MATERIAL_PATH.fullmatch(path):
                ident,action=MATERIAL_PATH.fullmatch(path).groups()
                if action:self.send(404,{'error':'接口不存在'});return
                self.send(200,self.server.get_materials().get(self.owner(),ident))
            elif HISTORY_PATH.fullmatch(path):
                ident,action=HISTORY_PATH.fullmatch(path).groups()
                if action:self.send(404,{'error':'接口不存在'});return
                self.send(200,self.server.get_history().get(self.account_owner(),ident))
            elif path.startswith('/api/knowledge-libraries/'):
                from knowledge_modules import module_document
                from knowledge_catalog import LIBRARY_MAP
                ident=path[len('/api/knowledge-libraries/'):]
                if ident not in LIBRARY_MAP:
                    self.send(404,{'error':'知识模块不存在'});return
                records=qa.RECORDS+self.server.get_experiences().records()
                self.send(200,module_document(ident,records))
            elif path == '/api/topology-observations':
                from topology_observations import latest
                owner=self.account_owner()
                records=latest(self.server.get_workflow().store,owner,'ha_topology')
                self.send(200,{'records':records,'window_days':30})
            elif path == '/api/knowledge-libraries':
                self.send(200, qa.catalog(qa.RECORDS))
            elif path == '/api/admin/status':
                self.send(200, {'configured':self.server.admin_auth.configured(),
                    'authenticated':self.server.admin_auth.valid(self.cookie('ata_admin'))})
            elif path == '/api/admin/tasks':
                self.send(410, {'error':'诊断管理员接口已关闭，请在本人聊天中确认本次检查'})
            elif path == '/api/experience-references':
                self.owner();self.send(200,{'references':self.server.get_experiences().references()})
            elif path in ('/api/experiences','/api/admin/experiences'):
                if path.startswith('/api/admin/'):
                    self.require_admin();owner=None
                else:owner=self.owner()
                self.send(200,{'experiences':self.server.get_experiences().list(owner)})
            elif EXPERIENCE_PATH.fullmatch(path):
                admin,ident,action=EXPERIENCE_PATH.fullmatch(path).groups()
                if action:self.send(404,{'error':'接口不存在'});return
                if admin:self.require_admin();owner=None
                else:owner=self.owner()
                self.send(200,self.server.get_experiences().get(ident,owner))
            elif path == '/api/tasks':
                self.send(200, {'tasks':self.server.get_workflow().store.list(self.owner())})
            elif path in ('/api/cases','/api/sources'):
                self.send(200, {'cases':qa.CASES} if path.endswith('cases') else qa.SOURCES)
            else:
                match = TASK_PATH.fullmatch(path)
                if not match or match.group(3): self.send(404, {'error':'接口不存在'}); return
                if match.group(1):
                    self.send(410, {'error':'诊断管理员接口已关闭，请在本人聊天中查看本次检查'});return
                else: owner = self.owner()
                self.send(200, self.server.get_workflow().store.get(match.group(2), owner))
        except (TaskError,ExperienceError,AccountError,HistoryError,MaterialError,MemoryError,RollingError) as error: self.task_error(error)
        except Exception: self.send(503, {'error':'服务暂不可用，请稍后重试'})

    def do_POST(self):
        if not self.local_host(): self.send(403, {'error':'请通过本机SSH隧道访问'}); return
        match = TASK_PATH.fullmatch(self.path)
        mode_match = MODE_PATH.fullmatch(self.path)
        experience_match=EXPERIENCE_PATH.fullmatch(self.path)
        history_match=HISTORY_PATH.fullmatch(self.path)
        material_match=MATERIAL_PATH.fullmatch(self.path)
        memory_match=MEMORY_PATH.fullmatch(self.path)
        repair_match=REPAIR_PATH.fullmatch(self.path)
        window_match=REPAIR_WINDOW_PATH.fullmatch(self.path)
        if self.path not in ('/api/ask','/api/chat','/api/selection/explain','/api/diagnose','/api/admin/login','/api/admin/logout','/api/experiences','/api/task-comparison','/api/account/register','/api/account/login','/api/account/logout','/api/account/registration','/api/repair-windows','/api/history','/api/materials') and not match and not mode_match and not experience_match and not history_match and not material_match and not memory_match and not repair_match and not window_match:
            self.send(404, {'error':'接口不存在'}); return
        if self.path != '/api/ask' and not secrets.compare_digest(self.headers.get('X-ATA-Token','').encode('utf-8'), CSRF_TOKEN.encode('ascii')):
            self.send(403, {'error':'页面凭据失效，请刷新后重试'}); return
        origin = self.headers.get('Origin')
        if ((self.path not in ('/api/ask','/api/diagnose') and not origin) or
            (origin and origin != 'http://'+self.headers.get('Host',''))):
            self.send(403, {'error':'不接受跨站请求'}); return
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            self.send(415, {'error':'需要 JSON 请求'}); return
        try:
            length = int(self.headers.get('Content-Length','0'))
            limit=400000 if self.path in ('/api/history','/api/materials') else 100000
            if not 0 < length <= limit: raise ValueError()
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(data, dict): raise ValueError()
        except (ValueError, UnicodeError, TypeError):
            self.send(400, {'error':'需要有效的JSON请求'}); return
        # 旧诊断接口不能绕过新审批。仅内部Python函数保留兼容。
        if self.path == '/api/diagnose':
            self.send(410, {'status':'approval_required','error':'诊断入口已升级，请在聊天页面提交问题并确认本次检查。'}); return
        background=bool(match and not match.group(1) and match.group(3)=='decision' and data.get('background') is True)
        purpose='model' if background else request_category(self.path,match.group(3) if match and not match.group(1) else None)
        try:
            rate_owner=self.owner()
        except (TaskError,AccountError):
            # 只用服务端验证的身份；伪造Cookie不会取得独立配额。
            rate_owner='anonymous:'+self.client_address[0]
        if not self.server.request_limiter.allow(rate_owner,purpose):
            if self.server.workflow is not None and (self.path=='/api/chat' or match):
                self.server.workflow.audit.record(match.group(2) if match else secrets.token_hex(16),
                    0,'http_rejected',{'operation':match.group(3) if match else 'chat','reason':'rate_limited'})
            self.send(429, {'error':'请求过于频繁，请稍后再试','code':'rate_limited','retry_after_seconds':60}); return
        model_slot=purpose=='model' and not background
        if model_slot and not self.server.model_slots.acquire(False):
            self.send(429, {'error':'正在处理其他提问，请稍后重试','code':'busy'}); return
        chat_trace=None
        try:
            if self.path=='/api/repair-windows' or window_match:
                owner=self.account_owner();guard=self.identity_guard(owner);guard()
                permissions=self.server.get_workflow().repairs.permissions
                if self.path=='/api/repair-windows':
                    if set(data)!={'task'}:raise ValueError()
                    result=permissions.request(owner,data['task'])
                else:
                    admin,ident,action=window_match.groups()
                    if admin:self.require_admin()
                    if admin and action=='decision':
                        if type(data.get('approved')) is not bool:raise ValueError()
                        def administrator_active():guard();self.require_admin()
                        if data['approved']:
                            if set(data)!={'approved','duration_minutes'}:raise ValueError()
                            result=permissions.grant(ident,owner,data['duration_minutes'],administrator_active)
                        else:
                            if set(data)!={'approved'}:raise ValueError()
                            result=permissions.revoke(ident,owner)
                    elif action=='revoke' and not data:result=permissions.revoke(ident,owner,None if admin else owner)
                    else:raise ValueError()
                self.send(200,result);return
            if repair_match:
                ident,action=repair_match.groups();manager=self.server.get_workflow().repairs;owner=self.owner()
                self.identity_guard(owner)()
                if action=='decision' and set(data)=={'approved'}:result=manager.decide(ident,owner,data['approved'])
                elif action=='execute' and not data:result=manager.execute(ident,owner,self.identity_guard(owner))
                elif action=='cancel' and not data:result=manager.cancel(ident,owner)
                elif action=='reconcile' and not data:result=manager.reconcile(ident,owner,self.identity_guard(owner))
                else:raise ValueError()
                self.send(200,result);return
            if self.path=='/api/selection/explain':
                if set(data)!={'text','allow_context'} or data.get('allow_context') is not True:raise ValueError()
                text=data.get('text')
                if not isinstance(text,str) or not 1<=len(text.strip())<=800:raise ValueError()
                owner=self.owner(create=True);guard=self.identity_guard(owner)
                service=self.server.get_workflow();guard()
                self.send_selection_stream(text,guard,service)
                return
            if memory_match:
                conversation,action=memory_match.groups()
                if not action and isinstance(data.get('action'),str):
                    owner=self.owner()
                    mem_store=self.server.get_agent_memory()
                    op=data['action']
                    extra_payload={}
                    if op=='delete_memory' and isinstance(data.get('id'),str):
                        mem_store.delete_memory(owner,data['id'])
                    elif op=='add_memory':
                        saved=mem_store.save_memory(
                            owner,str(data.get('kind','preference')),
                            str(data.get('title','')),str(data.get('content','')),
                            str(data.get('scope','')),source_conversation=conversation,
                            shared=bool(data.get('shared',False)),
                            tags=str(data.get('tags','')),
                        )
                        extra_payload['saved']=saved
                    elif op=='promote_shared' and isinstance(data.get('id'),str):
                        mem_store.set_shared(owner,data['id'],True)
                    elif op=='unshare_memory' and isinstance(data.get('id'),str):
                        mem_store.set_shared(owner,data['id'],False)
                    elif op=='feedback_accurate':
                        saved=self.server.get_agent_v2().learn_from_feedback(
                            owner,conversation,'accurate',
                            str(data.get('question','')),str(data.get('answer','')),
                            note=str(data.get('note','')),
                            tool_summary=str(data.get('tool_summary','')),
                            share_to_lab=bool(data.get('share_to_lab',False)),
                        )
                        extra_payload['saved']=saved
                    elif op=='feedback_correct':
                        saved=self.server.get_agent_v2().learn_from_feedback(
                            owner,conversation,'incorrect',
                            str(data.get('question','')),str(data.get('answer','')),
                            note=str(data.get('note','')),
                            tool_summary=str(data.get('tool_summary','')),
                            share_to_lab=bool(data.get('share_to_lab',False)),
                        )
                        extra_payload['saved']=saved
                    elif op=='consolidate_conversation':
                        items=self.server.get_agent_v2().consolidate_conversation(owner,conversation)
                        extra_payload['consolidated']=items
                    elif op=='revoke_device' and isinstance(data.get('device'),str):
                        mem_store.revoke_device(owner,data['device'])
                    elif op=='revoke_all_devices':
                        mem_store.revoke_all_devices(owner)
                    else:
                        raise ValueError()
                    catalog_map={x['id']:x['label'] for x in public_catalog()['devices']}
                    summary_row=mem_store.get_summary(owner,conversation)
                    result={'status':'ok','agent_memory':{
                        'granted_devices':[{'id':d,'label':catalog_map.get(d,d)} for d in mem_store.granted_devices(owner)],
                        'summary':summary_row['summary'] if summary_row else '',
                        'memories':mem_store.list_memories(owner,limit=80),
                        'shared_memories':mem_store.list_shared_memories(owner=owner,limit=60),
                    }}
                    result.update(extra_payload)
                else:
                    owner=self.account_owner();store=self.server.get_task_memory()
                    if action=='rolling':result=store.rolling.configure(owner,conversation,data)
                    elif action=='draft':
                        if data:raise ValueError()
                        result=store.draft(owner,conversation)
                    elif action=='withdraw':
                        if set(data)!={'expected_revision'}:raise ValueError()
                        result=store.withdraw(owner,conversation,data['expected_revision'])
                    else:result=store.save(owner,conversation,data)
            elif self.path=='/api/materials':
                result=self.server.get_materials().add(self.owner(),data)
            elif material_match:
                ident,action=material_match.groups()
                if action!='delete' or data:raise ValueError()
                result=self.server.get_materials().delete(self.owner(),ident)
            elif self.path=='/api/account/registration':
                self.account_owner()
                if set(data)!={'password','expected_revision'}:raise ValueError()
                result=self.server.get_accounts().configure_registration(data['password'],data['expected_revision'],self.cookie('ata_account'))
            elif self.path in ('/api/account/register','/api/account/login'):
                register=self.path.endswith('/register')
                if set(data)!=({'username','password','allow_storage','lab_password'} if register else {'username','password'}):raise ValueError()
                accounts=self.server.get_accounts()
                token,account=(accounts.register_invited(data['username'],data['password'],data['allow_storage'],data['lab_password']) if register
                               else accounts.login(data['username'],data['password']))
                self.end_identity();self.set_cookie('ata_account',token,SESSION_SECONDS)
                result={'authenticated':True,'account':account}
            elif self.path=='/api/account/logout':
                if data:raise ValueError()
                self.end_identity();result={'authenticated':False,'account':None}
            elif self.path=='/api/history':
                result=self.server.get_history().put(self.account_owner(),data)
            elif history_match:
                ident,action=history_match.groups()
                if action!='delete' or set(data)!={'expected_revision'}:raise ValueError()
                owner=self.account_owner()
                result=self.server.get_history().delete(owner,ident,data['expected_revision'])
                self.server.get_workflow().store.set_interaction(owner,ident,'qa',None)
                self.server.get_agent_memory().clear_conversation(owner,ident)
            elif self.path == '/api/admin/login':
                if set(data) != {'password'}: raise ValueError()
                token, error = self.server.admin_auth.login(data['password'])
                if error: raise TaskError(error)
                self.set_cookie('ata_admin', token, 3600); result = {'status':'ok'}
            elif self.path == '/api/admin/logout':
                if data: raise ValueError()
                self.server.admin_auth.logout(self.cookie('ata_admin'))
                self.set_cookie('ata_admin','',0); result = {'status':'ok'}
            elif self.path=='/api/experiences':
                result=self.server.get_experiences().submit(self.owner(),data)
            elif experience_match:
                admin,ident,action=experience_match.groups()
                if admin:self.require_admin();owner=None
                else:owner=self.owner()
                store=self.server.get_experiences()
                if action=='edit':
                    if set(data)!={'expected_hash','changes'} or not isinstance(data['changes'],dict):raise ValueError()
                    result=store.update(ident,owner,data['expected_hash'],data['changes'])
                elif admin and action=='review':
                    if set(data)!={'decision','expected_hash','note','regression_question','confirmed'}:raise ValueError()
                    if type(data['confirmed']) is not bool or data['decision']=='approve' and not data['confirmed']:raise ValueError()
                    result=store.review(ident,data['decision'],data['expected_hash'],data['note'],
                                        data['regression_question'],'authenticated-admin')
                elif admin and action in ('publish','withdraw'):
                    if set(data)!={'expected_hash'}:raise ValueError()
                    result=getattr(store,action)(ident,data['expected_hash'],'authenticated-admin')
                else:self.send(404,{'error':'接口不存在'});return
            elif self.path=='/api/task-comparison':
                if set(data)!={'before_task','after_task'}:raise ValueError()
                result=self.server.get_workflow().compare(self.owner(),data['before_task'],data['after_task'])
            elif mode_match:
                if set(data) != {'mode','scene'}: raise ValueError()
                conversation = validate_conversation_id(mode_match.group(1))
                service=self.server.get_workflow()
                result = service.store.set_interaction(self.owner(),conversation,data['mode'],data['scene'])
                service.audit.record(secrets.token_hex(16),0,'mode_selected',
                    {'conversation':audit_text(conversation),'mode':result['mode'],'scene':result['scene'],
                     'revision':result['revision'],'cancelled_tasks':result['cancelled_tasks']})
            elif match:
                admin, task_id, action = match.groups()
                service = self.server.get_workflow()
                if admin:
                    self.send(410, {'error':'诊断管理员接口已关闭，请在本人聊天中确认本次检查'});return
                elif action == 'decision':
                    if (set(data)-{'approved','data_use','background'} or type(data.get('approved')) is not bool
                            or type(data.get('background',False)) is not bool or background and not data['approved']):
                        raise ValueError()
                    owner=self.owner()
                    if background:
                        result=service.approve_in_background(owner,task_id,data.get('data_use'),self.identity_guard(owner))
                    else:
                        result=service.store.decide(task_id,data['approved'],'task-owner',data.get('data_use'),owner=owner)
                elif action == 'input':
                    if set(data)!={'message_id','text'}:raise ValueError()
                    result=service.add_task_input(self.owner(),task_id,data['message_id'],data['text'])
                elif action == 'start':
                    if data:raise ValueError()
                    owner=self.owner()
                    result=service.start_approved(owner,task_id,access_guard=self.identity_guard(owner))
                elif action == 'resume':
                    if set(data) != {'allow_context','allow_live_data'} or any(type(v) is not bool for v in data.values()):
                        raise ValueError()
                    owner=self.owner()
                    result = service.resume(owner,task_id,data['allow_context'] and data['allow_live_data'],
                                            access_guard=self.identity_guard(owner))
                elif action == 'cancel':
                    if data: raise ValueError()
                    result = service.cancel_task(self.owner(),task_id)
                else: self.send(404, {'error':'接口不存在'}); return
            else:
                question = data.get('question','')
                is_resume_pending = self.path == '/api/chat' and isinstance(data.get('pending_id'), str) and bool(data.get('pending_id'))
                if not is_resume_pending and (not isinstance(question,str) or not 1 <= len(question.strip()) <= 1000): raise ValueError()
                if self.path == '/api/chat':
                    if set(data)-{'question','history','allow_context','conversation_id','interaction_mode','interaction_revision','chat_space','material_id','allow_materials','use_task_memory','memory_revision','approval_flow','grant_devices','pending_id','deny_pending','legacy_mode'}: raise ValueError()
                    consent = data.get('allow_context',False)
                    if type(consent) is not bool: raise ValueError()
                    history,history_excerpted = prepare_history(data.get('history',[]))
                    owner=self.owner(create=True);material_info=None;material_context=None
                    service=self.server.get_workflow()
                    conversation=validate_conversation_id(data.get('conversation_id','default'))
                    use_agent_v2 = (
                        data.get('material_id') is None
                        and not data.get('legacy_mode')
                        and (self.server.agent_v2 is not None or self.server.agent_v2_enabled)
                    )
                    if use_agent_v2:
                        if consent is not True:
                            result = {'status':'consent_required','answer':'请先确认本次对话的数据使用范围。'}
                        else:
                            grant_devices = data.get('grant_devices')
                            if grant_devices is not None and not isinstance(grant_devices, list):
                                raise ValueError()
                            result = self.server.get_agent_v2().chat(
                                owner, conversation, question.strip() if isinstance(question, str) else '',
                                client_history=history,
                                grant_devices=grant_devices,
                                pending_id=data.get('pending_id'),
                                deny_pending=bool(data.get('deny_pending', False)),
                            )
                        self.send(200, result)
                        return
                    if data.get('material_id') is not None:
                        if data.get('allow_materials') is not True:raise MaterialError('material_consent_required')
                        if consent is not True:raise TaskError('consent_required')
                        material_info,material_context=self.server.get_materials().select(owner,data['material_id'],
                            data.get('conversation_id','default'),question.strip())
                    elif 'allow_materials' in data and data['allow_materials'] is not False:raise ValueError()
                    memory_context=None
                    use_memory=data.get('use_task_memory',False)
                    if type(use_memory) is not bool or not use_memory and 'memory_revision' in data:raise ValueError()
                    if use_memory:
                        owner=self.account_owner()
                        if consent is not True:raise TaskError('consent_required')
                        if material_info or data.get('interaction_mode','qa') not in ('qa','assistant'):raise MemoryError('memory_qa_only')
                        memory_context=self.server.get_task_memory().require(owner,data.get('conversation_id','default'),data.get('memory_revision'))
                    rolling_bundle=None
                    public_bundle=None
                    if (self.cookie('ata_account') and consent is True and material_info is None
                            and data.get('interaction_mode','qa') in ('qa','assistant')):
                        rolling_bundle=self.server.get_task_memory().rolling.for_request(
                            owner,data.get('conversation_id','default'),question.strip())
                        public_bundle=self.server.get_task_memory().public_evidence.background(
                            owner,data.get('conversation_id','default'),question.strip(),history)
                    identity_guard=self.identity_guard(owner)
                    def access_guard():
                        identity_guard()
                        if material_info:
                            try:self.server.get_materials().require_active(owner,material_info['id'],material_info['conversation_id'])
                            except MaterialError:raise unified_chat.ToolRejection('material_not_active') from None
                        if memory_context:
                            try:self.server.get_task_memory().require(owner,data.get('conversation_id','default'),memory_context['revision'])
                            except (MemoryError,HistoryError):raise unified_chat.ToolRejection('memory_not_active') from None
                        if rolling_bundle:
                            try:self.server.get_task_memory().rolling.assert_active(owner,data.get('conversation_id','default'),rolling_bundle['binding'])
                            except (RollingError,HistoryError):raise unified_chat.ToolRejection('rolling_context_not_active') from None
                        if public_bundle:
                            try:self.server.get_task_memory().public_evidence.assert_active(owner,data.get('conversation_id','default'),public_bundle['binding'])
                            except (PublicEvidenceError,HistoryError):raise unified_chat.ToolRejection('knowledge_source_inactive') from None
                    service=self.server.get_workflow()
                    conversation=validate_conversation_id(data.get('conversation_id','default'))
                    trace_id=secrets.token_hex(16)
                    safe_question, credentials=filter_log_output(question.strip())
                    chat_trace=(service.audit,trace_id)
                    service.audit.record(trace_id,0,'chat_request',{
                        'question':safe_question,'conversation':audit_text(conversation,credentials),
                        'mode':data.get('interaction_mode','qa') if data.get('interaction_mode','qa') in ('qa','diagnostic','assistant') else None,
                        'space':data.get('chat_space') if data.get('chat_space') in ('ha','general') else None,
                        'revision':data.get('interaction_revision') if type(data.get('interaction_revision')) is int else None,
                        'history_messages':len(history),'consent':consent})
                    token=request_trace.set(trace_id)
                    try:
                        result = service.ask(owner,question.strip(),history,consent,conversation,
                                                           data.get('interaction_mode','qa'),data.get('interaction_revision'),
                                                           material_context=material_context,access_guard=access_guard,task_memory=memory_context,
                                                           rolling_context=rolling_bundle['context'] if rolling_bundle else None,
                                                           public_background=public_bundle['context'] if public_bundle else None,
                                                           approval_flow=(SINGLE_APPROVAL if data.get('interaction_mode') in ('assistant','diagnostic') and data.get('approval_flow') is None else data.get('approval_flow')),
                                                           chat_space=data.get('chat_space'),
                                                           learn_from_history=bool(self.cookie('ata_account')))
                    finally:
                        request_trace.reset(token)
                    task=result.get('task')
                    linked_task=task.get('id') if isinstance(task,dict) else task
                    service.audit.record(trace_id,0,'chat_result',{
                        'status':result.get('status'),'route':result.get('route'),'reason':result.get('reason'),
                        'task':linked_task,'historical_task':result.get('historical_investigation_id'),
                        'answer':audit_text(result.get('answer'),credentials),
                        'refs':result.get('refs',[])})
                    result['trace_id']=trace_id
                    if self.cookie('ata_account') and material_info is None:
                        self.server.get_task_memory().public_evidence.save(owner,data.get('conversation_id','default'),result)
                    if history_excerpted:result['history_info']={'excerpted_messages':history_excerpted,
                        'note':'本次仅使用部分历史内容；完整原文仍保留在对话中。'}
                    if material_info:result['material_info']=material_info
                    if memory_context and result.get('route')!='diagnostic':result['memory_info']={k:v for k,v in memory_context.items() if k!='entries'}
                    if rolling_bundle and result.get('route') not in ('diagnostic','material_analysis'):
                        result['rolling_info']={'entries_used':len(rolling_bundle['context']['entries']),
                            'revision':rolling_bundle['context']['revision'],'note':'已使用本对话较早的相关用户原话作为背景；不恢复现场证据或批准。'}
                else:
                    mode, group = data.get('mode','unknown'), data.get('group','unknown')
                    allow = data.get('allow_new_topology',False)
                    if mode not in qa.MODES or group not in qa.GROUPS or type(allow) is not bool: raise ValueError()
                    route=classify(question.strip())
                    if route.get('intent')=='live':
                        result={'kind':'current_unverified','status':'current_unverified','device_executed':False,
                                'answer':'当前状态未核实。此旧知识入口只能解释资料，不能以快照确认现状；请通过聊天页面提交问题并申请本轮只读检查。'}
                    else: result = qa.answer(question.strip(),mode,group,allow)
            self.send(200, result)
        except (TaskError,ExperienceError,AccountError,HistoryError,MaterialError,MemoryError,RollingError) as error:
            if match and self.server.workflow is not None and not match.group(1):
                reason=str(error)
                self.server.workflow.audit.record(match.group(2),0,'http_rejected',
                    {'operation':match.group(3),'reason':reason if reason in ERRORS or reason=='not_found' else 'task_error'})
            if chat_trace:
                audit,trace_id=chat_trace
                reason=str(error)
                audit.record(trace_id,0,'http_rejected',{'operation':'chat','reason':reason if reason in ERRORS else 'task_error'})
            self.task_error(error)
        except (ValueError, TypeError, KeyError): self.send(400, {'error':'问题或操作参数无效，请刷新后重试'})
        except Exception: self.send(503, {'error':'服务暂不可用，请稍后重试'})
        finally:
            if model_slot:self.server.model_slots.release()

    def local_host(self):
        name, sep, port = self.headers.get('Host','').partition(':')
        return name in ('127.0.0.1','localhost') and (not sep or (port.isdigit() and 0 < int(port) < 65536))

    def send_selection_stream(self,text,guard,service):
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close');self.send_header('X-Accel-Buffering','no')
        for cookie in self.response_cookies:self.send_header('Set-Cookie',cookie)
        self.response_cookies=[];self.end_headers();self.close_connection=True
        metrics={'kind':'selection_explain','successful':False};task=secrets.token_hex(16)
        def emit(kind,value):
            guard()
            self.wfile.write(('event: '+kind+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode('utf-8'));self.wfile.flush()
        try:
            emit('start',{})
            value=stream_explanation(qa.model_config(),text,emit,guard,metrics,availability=service.model_availability)
            emit('done',value)
        except (BrokenPipeError,ConnectionResetError):metrics['cancelled']=True
        except Exception as error:
            code=error.code if isinstance(error,ModelFailure) else 'selection_unavailable'
            metrics['failure']=code
            messages={'model_timeout':'解释超时，请重试。','model_rate_limited':'模型繁忙，请稍后重试。','model_output_truncated':'解释未完成，请点击重试。'}
            try:emit('error',{'error':messages.get(code,'解释暂未完成，请稍后重试。')})
            except Exception:pass
        finally:service.audit.add(task,0,'selection_model',metrics)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self,address,handler=Handler,workflow=None,admin_auth=None,experiences=None):
        self.request_limiter=RequestLimiter()
        self.model_slots=threading.BoundedSemaphore(2)
        self.workflow = workflow; self.workflow_lock = threading.Lock()
        self.admin_auth = admin_auth or AdminAuth()
        self.experience_store=experiences
        self.experience_lock=threading.Lock()
        self.accounts=None;self.histories=None;self.identity_lock=threading.Lock()
        self.materials=None
        self.task_memory=None
        self.agent_memory=None
        self.agent_v2=None
        self.agent_v2_enabled=(workflow is None)
        super().__init__(address,handler)

    def get_workflow(self):
        with self.workflow_lock:
            fresh=self.workflow is None
            if self.workflow is None:
                path = ROOT/'data/workflow.sqlite3'; path.parent.mkdir(parents=True,exist_ok=True)
                store = ApprovalStore(path); store.recover_interrupted()
                self.workflow = WorkflowService(store,prefetch_overview=True,auto_read_only=True)
            self.workflow.experiences=self.get_experiences
            if self.workflow.repairs is None:
                self.workflow.repairs=RepairManager(Path(self.workflow.store.path).with_name('repairs.sqlite3'),
                    ROOT/'.secrets/repair-policy.json',self.workflow.get_probe(),getattr(self,'repair_executor',None),
                    inspector=getattr(self,'repair_inspector',None),task_loader=self.load_repair_task)
                if fresh:self.workflow.repairs.recover_interrupted()
            return self.workflow

    def load_repair_task(self,ident,owner):
        task=self.workflow.store.get(ident,owner)
        interaction=self.workflow.store.interaction(owner,task['conversation_id'])
        if interaction['mode'] not in ('assistant','diagnostic') or interaction['revision']!=task['interaction_revision']:
            raise TaskError('mode_changed')
        return task

    def get_experiences(self):
        workflow=self.get_workflow()
        with self.experience_lock:
            if self.experience_store is None:
                self.experience_store=ExperienceStore(Path(workflow.store.path).with_name('experiences.sqlite3'),ROOT)
            return self.experience_store

    def get_accounts(self):
        path=Path(self.get_workflow().store.path).with_name('accounts.sqlite3')
        with self.identity_lock:
            if self.accounts is None:self.accounts=AccountStore(path)
            return self.accounts

    def get_history(self):
        path=Path(self.get_workflow().store.path).with_name('chats.sqlite3')
        with self.identity_lock:
            if self.histories is None:self.histories=HistoryStore(path)
            return self.histories

    def get_materials(self):
        path=Path(self.get_workflow().store.path).with_name('materials.sqlite3')
        with self.identity_lock:
            if self.materials is None:self.materials=MaterialStore(path)
            return self.materials

    def get_task_memory(self):
        history=self.get_history()
        with self.identity_lock:
            if self.task_memory is None:self.task_memory=TaskMemory(history)
            return self.task_memory

    def get_agent_memory(self):
        path=Path(self.get_workflow().store.path).with_name('agent_memory.sqlite3')
        with self.identity_lock:
            if self.agent_memory is None:self.agent_memory=AgentMemoryStore(path)
            return self.agent_memory

    def get_agent_v2(self):
        workflow=self.get_workflow()
        memory=self.get_agent_memory()
        with self.identity_lock:
            if self.agent_v2 is None:
                self.agent_v2=AutonomousAgent(
                    memory,
                    probe_fn=lambda device,check: workflow.get_probe()(device,check),
                    config_fn=qa.model_config,
                    experiences_fn=self.get_experiences,
                    availability=workflow.model_availability,
                )
            return self.agent_v2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=17865)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535: parser.error('端口无效')
    server = Server(('127.0.0.1',args.port))
    server.get_workflow()
    print('ATA v'+VERSION+' listening on 127.0.0.1:'+str(args.port),flush=True)
    server.serve_forever()
