"""固定公网入口：原账号登录后转发到本机助手，不提供任意代理。"""
import argparse
import http.client
import ipaddress
import json
import logging
import re
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

ACCOUNT=re.compile(r'[A-Za-z0-9_-]{43}')
ALLOWED_GET=re.compile(r'^/(?:api/(?:health|repair-windows|admin/repair-windows|(?:admin/)?repair-windows/[a-f0-9]{32}|account|account/registration|session|diagnostic-scenes|devices|cases|sources|tasks|knowledge-libraries|experience-references|experiences|history|admin/(?:status|experiences)|(?:admin/)?experiences/[a-f0-9]{32}|repairs/[a-f0-9]{32}|tasks/[a-f0-9]{32}|history/[A-Za-z0-9_-]{1,80}|materials/[a-f0-9]{32}|knowledge-libraries/[a-z_]+|conversations/[A-Za-z0-9_-]{1,80}/(?:mode|materials|memory(?:/rolling)?|investigations(?:/[a-f0-9]{32})?)))?$')
ALLOWED_POST=re.compile(r'^/api/(?:chat|ask|repair-windows|(?:admin/)?repair-windows/[a-f0-9]{32}/(?:decision|revoke)|selection/explain|account/(?:login|logout|register|registration)|admin/(?:login|logout)|history|materials|experiences|task-comparison|tasks/[a-f0-9]{32}/(?:decision|start|resume|cancel|input)|repairs/[a-f0-9]{32}/(?:decision|execute|cancel|reconcile)|history/[A-Za-z0-9_-]{1,80}/delete|materials/[a-f0-9]{32}/delete|(?:admin/)?experiences/[a-f0-9]{32}/(?:edit|review|publish|withdraw)|conversations/[A-Za-z0-9_-]{1,80}/(?:mode|memory(?:/(?:draft|withdraw|rolling))?))$')

def cookies(raw):
    jar=SimpleCookie()
    try:jar.load(raw or '')
    except Exception:return ''
    return '; '.join(name+'='+jar[name].value for name in ('ata_account','ata_session','ata_admin')
                    if name in jar and re.fullmatch('[A-Za-z0-9_-]{1,128}',jar[name].value))

def proxy_address(line):
    """只解析FRP发来的第一条PROXY v1行；不采信浏览器转发头。"""
    if len(line)>108 or not line.endswith(b'\r\n'):raise ValueError('invalid_proxy_line')
    fields=line.decode('ascii').strip().split(' ')
    if len(fields)!=6 or fields[0]!='PROXY' or fields[1] not in ('TCP4','TCP6'):raise ValueError('invalid_proxy_line')
    source=ipaddress.ip_address(fields[2]);destination=ipaddress.ip_address(fields[3])
    version=4 if fields[1]=='TCP4' else 6
    if source.version!=version or destination.version!=version:raise ValueError('invalid_proxy_family')
    if any(not p.isdigit() or not 0<int(p)<65536 for p in fields[4:]):raise ValueError('invalid_proxy_port')
    return str(source),int(fields[4])

class Limiter:
    def __init__(self):self.lock=threading.Lock();self.entries={}
    def allow(self,ip,login=False):
        now=time.monotonic();key=(ip,login);limit=8 if login else 120
        with self.lock:
            for old in list(self.entries):
                self.entries[old]=[stamp for stamp in self.entries[old] if now-stamp<60]
                if not self.entries[old]:del self.entries[old]
            if key not in self.entries and len(self.entries)>=4096:return False
            recent=self.entries.setdefault(key,[])
            if len(recent)>=limit:return False
            recent.append(now);return True

class Server(ThreadingMixIn,HTTPServer):
    daemon_threads=True
    request_queue_size=16
    def __init__(self,address,authority,upstream=('127.0.0.1',17865),require_proxy=True):
        if not re.fullmatch(r'(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{2,5}',authority):raise ValueError('invalid_authority')
        ipaddress.ip_address(authority.split(':')[0])
        if not 0<int(authority.split(':')[1])<65536:raise ValueError('invalid_port')
        self.authority=authority;self.origin='http://'+authority;self.upstream=upstream
        self.require_proxy=require_proxy;self.limiter=Limiter();self.slots=threading.BoundedSemaphore(24)
        self.bootstrap_lock=threading.Lock();self.bootstrap_cookie=''
        self.template=(Path(__file__).with_name('public_login.html')).read_text(encoding='utf-8')
        super().__init__(address,Handler)
    def process_request(self,request,client_address):
        if not self.slots.acquire(False):
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()
    def handle_error(self,request,client_address):pass

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(10);self.headers_sent=False
        if self.server.require_proxy:
            try:self.client_address=proxy_address(self.rfile.readline(109))
            except (ValueError,UnicodeError):raise ConnectionError('invalid_proxy_header') from None
    def log_message(self,*args):pass  # 不记录账号、口令、Cookie或提问正文。
    def reply(self,code,value,kind='application/json; charset=utf-8'):
        body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src http://127.0.0.1:17875; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.headers_sent=True;self.wfile.write(body)
    def upstream(self,path,method='GET',body=None,extra=None):
        connection=http.client.HTTPConnection(*self.server.upstream,timeout=300 if method=='POST' else 8)
        headers={'Host':'127.0.0.1:'+str(self.server.upstream[1]),'Cookie':cookies(self.headers.get('Cookie')),'Connection':'close'}
        if method=='POST':headers.update({'Content-Type':'application/json','Origin':'http://'+headers['Host'],'X-ATA-Token':self.headers.get('X-ATA-Token','')})
        if path=='/api/chat' and self.headers.get('Accept')=='text/event-stream':
            headers['Accept']='text/event-stream'
        if extra:headers.update(extra)
        try:
            connection.request(method,path,body=body,headers=headers)
            return connection,connection.getresponse()
        except Exception:
            connection.close()
            raise
    def authenticated(self):
        jar=SimpleCookie();jar.load(cookies(self.headers.get('Cookie')))
        if 'ata_account' not in jar or not ACCOUNT.fullmatch(jar['ata_account'].value):return False
        connection,response=self.upstream('/api/account')
        try:
            value=json.loads(response.read(400001));return response.status==200 and value.get('authenticated') is True
        finally:connection.close()
    def do_GET(self):self.dispatch(False)
    def do_POST(self):self.dispatch(True)
    def dispatch(self,post):
        started=time.monotonic()
        try:
            hosts=self.headers.get_all('Host',[])
            if hosts!=[self.server.authority]:return self.reply(403,{'error':'入口地址无效'})
            if not self.server.limiter.allow(self.client_address[0]):return self.reply(429,{'error':'请求较多，请稍后重试'})
            if not self.path.startswith('/') or self.path.startswith('//') or len(self.path)>512:return self.reply(404,{'error':'接口不存在'})
            path=self.path.split('?',1)[0]
            if not (ALLOWED_POST if post else ALLOWED_GET).fullmatch(path):return self.reply(404,{'error':'接口不存在'})
            if post and (self.headers.get_all('Origin',[])!=[self.server.origin] or self.headers.get('Sec-Fetch-Site')=='cross-site'):
                return self.reply(403,{'error':'不接受跨站请求'})
            if post and (self.headers.get_all('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1):
                return self.reply(400,{'error':'需要完整JSON请求'})
            authenticated=self.authenticated()
            if not authenticated:
                if not post and path=='/':return self.login_page()
                if not post and path=='/api/health':return self.health()
                if not post and path=='/api/account/registration':return self.forward(path,'GET',None)
                if not post and path=='/api/account':return self.reply(200,{'authenticated':False,'account':None,'expired':bool(cookies(self.headers.get('Cookie')))})
                if not (post and path in ('/api/account/login','/api/account/register')):return self.reply(401,{'error':'请先登录个人账号','reason':'account_required'})
            body=None
            if post:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.reply(415,{'error':'需要JSON请求'})
                try:length=int(self.headers.get('Content-Length','0'))
                except ValueError:return self.reply(400,{'error':'请求长度无效'})
                limit=400000 if path in ('/api/history','/api/materials') else 100000
                if path in ('/api/account/login','/api/account/register'):
                    if not self.server.limiter.allow(self.client_address[0],True):return self.reply(429,{'error':'登录尝试较多，请稍后重试'})
                if not 0<length<=limit:return self.reply(413,{'error':'请求内容过长'})
                body=self.rfile.read(length)
                if len(body)!=length:return self.reply(400,{'error':'请求内容不完整'})
            self.forward(path,'POST' if post else 'GET',body)
        except (OSError,ValueError,http.client.HTTPException) as error:
            if post and self.path.split('?',1)[0]=='/api/chat':
                logging.warning('chat forwarding failed after %.3fs: %s (headers_sent=%s)',
                                time.monotonic()-started,type(error).__name__,self.headers_sent)
            if not self.headers_sent:
                try:self.reply(503,{'error':'助手暂时不可用，请稍后重试'})
                except OSError:pass
    def login_page(self):
        with self.server.bootstrap_lock:
            connection,response=self.upstream('/',extra={'Cookie':self.server.bootstrap_cookie})
            try:
                page=response.read(1500000).decode('utf-8')
                token=re.search(r"const csrf='([A-Za-z0-9_-]{43})'",page)
                if response.status!=200 or not token:raise ValueError('upstream_not_ready')
                for name,value in response.getheaders():
                    if name.lower()=='set-cookie' and value.startswith('ata_session='):
                        self.server.bootstrap_cookie=value.split(';')[0]
                rendered=self.server.template.replace('__CSRF__',token.group(1))
            finally:connection.close()
        self.reply(200,rendered.encode('utf-8'),'text/html; charset=utf-8')
    def health(self):
        connection,response=self.upstream('/api/health')
        try:
            value=json.loads(response.read(8193))
            if response.status!=200 or value.get('status')!='ok':raise ValueError('upstream_not_ready')
            self.reply(200,{'project':'Agent-Troubleshooting-Assistant','version':value['version'],'status':'ok','entry':'public-http'})
        finally:connection.close()
    def forward(self,path,method,body):
        connection,response=self.upstream(path,method,body)
        try:
            self.send_response(response.status)
            for name,value in response.getheaders():
                if name.lower() in ('content-type','content-length','set-cookie','cache-control','x-content-type-options','content-security-policy','x-accel-buffering','retry-after'):
                    self.send_header(name,value)
            self.send_header('Connection','close');self.send_header('Referrer-Policy','same-origin')
            self.end_headers();self.headers_sent=True
            while True:
                chunk=response.read1(8192)
                if not chunk:break
                self.wfile.write(chunk);self.wfile.flush()
        finally:connection.close();self.close_connection=True

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    arguments=parser.parse_args();config=json.loads(Path(arguments.config).read_text())
    assert config=={'authority':'203.0.113.83:18890','listen_port':17866,'upstream_port':17865}
    with Server(('127.0.0.1',config['listen_port']),config['authority']) as server:server.serve_forever()
