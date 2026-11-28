"""公网网关的真实HTTP认证/来源/流式转发；不连外网、模型或设备。"""
if not __package__:
    import _bootstrap

import http.client,importlib.util,json,socket,threading,unittest
from concurrent.futures import wait
from pathlib import Path
from unittest.mock import Mock, patch
from tests.workflow_test_support import RunningApp,TEST_LAB_PASSWORD

spec=importlib.util.spec_from_file_location('public_http',Path(__file__).resolve().parents[1]/'deploy/public_http.py')
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
AUTHORITY='203.0.113.83:18890'

class PublicHTTPTests(unittest.TestCase):
 def setUp(self):
  self.backend=RunningApp(planner=Mock(),probe=Mock());self.backend.__enter__()
  self.server=gateway.Server(('127.0.0.1',0),AUTHORITY,upstream=('127.0.0.1',self.backend.server.server_port),require_proxy=False)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.thread.join();self.backend.__exit__()
 def call(self,path='/',data=None,cookie=None,origin=None,host=AUTHORITY,token=True):
  connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
  headers={'Host':host}
  if cookie:headers['Cookie']=cookie
  if data is not None:
   headers.update({'Origin':origin or 'http://'+AUTHORITY,'Content-Type':'application/json'})
   if token:headers['X-ATA-Token']=self.backend.server and __import__('app').CSRF_TOKEN
  connection.request('GET' if data is None else 'POST',path,body=None if data is None else json.dumps(data).encode(),headers=headers)
  response=connection.getresponse();body=response.read();result=(response.status,body,response.getheaders());connection.close();return result
 def register_local(self,name):
  code,result=self.backend.call('/api/account/register',{'username':name,'password':'test-public-http-password','allow_storage':True})
  self.assertEqual(code,200,result)
  for handler in self.backend.opener.handlers:
   jar=getattr(handler,'cookiejar',None)
   if jar:
    token=next(c.value for c in jar if c.name=='ata_account');return 'ata_account='+token
  self.fail('missing test account cookie')
 def test_public_login_shell_and_no_anonymous_model_device_or_source_access(self):
  code,body,_=self.call();self.assertEqual(code,200);self.assertIn('继续你的对话'.encode(),body);self.assertNotIn(b'__CSRF__',body)
  for path,data in [('/api/chat',{'question':'test'}),('/api/tasks',None),('/api/devices',None),('/api/sources',None),('/api/history',None),('/api/repairs/'+'a'*32,None)]:
   with self.subTest(path=path):self.assertEqual(self.call(path,data)[0],401)
  self.backend.service.planner.assert_not_called();self.backend.service.probe.assert_not_called()
  health=json.loads(self.call('/api/health')[1]);self.assertNotIn('admin_configured',health);self.assertEqual(health['entry'],'public-http')
 def test_account_login_uses_original_store_and_logout_removes_access(self):
  original=self.register_local('gateway-alice')
  code,body,headers=self.call('/api/account/login',{'username':'gateway-alice','password':'test-public-http-password'})
  self.assertEqual(code,200,body)
  cookie=[value.split(';')[0] for name,value in headers if name.lower()=='set-cookie' and value.startswith('ata_account=')][-1]
  code,page,_=self.call(cookie=cookie)
  self.assertEqual(code,200);self.assertIn(b'function renderRepair(',page)
  self.assertIn(b'id="space-general"',page);self.assertIn(b'id="space-ha"',page)
  self.assertTrue(json.loads(self.call('/api/account',cookie=cookie)[1])['authenticated'])
  self.assertEqual(self.call('/api/account/logout',{},cookie=cookie)[0],200)
  self.assertEqual(self.call('/api/tasks',cookie=cookie)[0],401)
 def test_foreign_origins_unknown_hosts_fake_cookies_and_registration_do_not_bypass(self):
  cookie=self.register_local('gateway-bob')
  self.assertEqual(self.call(host='evil.example')[0],403)
  self.assertEqual(self.call('/api/account/login',{},origin='http://evil.example')[0],403)
  self.assertEqual(self.call('/api/chat',{},cookie='ata_account='+'a'*43)[0],401)
  self.assertEqual(self.call('/api/chat',{},cookie=cookie,token=False)[0],403)
  for path in ('/.secrets/model.json','//evil.example/','http://evil.example/','/api/anything'):
   self.assertEqual(self.call(path,cookie=cookie)[0],404)
 def test_public_register_checks_lab_password_and_login_does_not_request_it(self):
  self.register_local('gateway-owner')
  metadata=json.loads(self.call('/api/account/registration')[1]);self.assertTrue(metadata['configured'])
  data={'username':'中','password':'1','allow_storage':True,'lab_password':'wrong'}
  self.assertEqual(self.call('/api/account/register',data)[0],409)
  data['lab_password']=TEST_LAB_PASSWORD
  code,body,headers=self.call('/api/account/register',data);self.assertEqual(code,200,body)
  cookie=[value.split(';')[0] for name,value in headers if name.lower()=='set-cookie' and value.startswith('ata_account=')][-1]
  self.assertEqual(self.call('/api/account/registration',{'password':'changed-by-new-user','expected_revision':metadata['revision']},cookie=cookie)[0],200)
  self.assertEqual(self.call('/api/account/login',{'username':'中','password':'1'})[0],200)
  # 名称超过旧网关8KB响应缓冲时，注册后仍能通过会话验证进入页面。
  long_name='n'*9000;data.update(username=long_name,lab_password='changed-by-new-user')
  code,body,headers=self.call('/api/account/register',data);self.assertEqual(code,200,body)
  long_cookie=[value.split(';')[0] for name,value in headers if name.lower()=='set-cookie' and value.startswith('ata_account=')][-1]
  self.assertTrue(json.loads(self.call('/api/account',cookie=long_cookie)[1])['authenticated'])
  self.assertEqual(self.call(cookie=long_cookie)[0],200)
 def test_permission_and_reconciliation_routes_retain_authentication_over_public_gateway(self):
  self.assertEqual(self.call('/api/repair-windows')[0],401)
  self.assertEqual(self.call('/api/repairs/'+'a'*32+'/reconcile',{})[0],401)
  cookie=self.register_local('gateway-permission-user')
  self.assertEqual(self.call('/api/repair-windows',cookie=cookie)[0],200)
  self.assertEqual(self.call('/api/admin/repair-windows',cookie=cookie)[0],401)
  self.assertEqual(self.call('/api/admin/repair-windows/'+'a'*32+'/decision',{'approved':True,'duration_minutes':10},cookie=cookie)[0],401)
  inbox='/api/tasks/'+'a'*32+'/input'
  payload={'message_id':'fixture','text':'continue'}
  self.assertEqual(self.call(inbox,payload)[0],401)
  code,body,_=self.call(inbox,payload,cookie=cookie)
  self.assertEqual(code,404);self.assertEqual(json.loads(body)['reason'],'not_found')
 def test_personal_history_isolation_survives_public_forwarding(self):
  alice=self.register_local('gateway-carol')
  body={'id':'private-chat','title':'private','messages':[{'role':'user','content':'my local fixture','context':True}],'expected_revision':0}
  self.assertEqual(self.call('/api/history',body,cookie=alice)[0],200)
  self.assertEqual(self.call('/api/history/private-chat',cookie=alice)[0],200)
  # A second browser/account does not gain the first account's saved history.
  other=self.backend.new_browser();self.backend.call('/api/session',browser=other)
  code,result=self.backend.call('/api/account/register',{'username':'gateway-dave','password':'test-public-http-password','allow_storage':True},browser=other)
  self.assertEqual(code,200,result)
  jar=next(h.cookiejar for h in other.handlers if hasattr(h,'cookiejar'))
  bob='ata_account='+next(c.value for c in jar if c.name=='ata_account')
  self.assertEqual(self.call('/api/history/private-chat',cookie=bob)[0],404)
 def test_proxy_protocol_is_parsed_before_http_and_cannot_be_client_header_spoofed(self):
  self.server.require_proxy=True
  with socket.create_connection(('127.0.0.1',self.server.server_port),timeout=3) as connection:
   connection.sendall(('PROXY TCP4 203.0.113.1 203.0.113.83 12345 18890\r\nGET /api/health HTTP/1.0\r\nHost: '+AUTHORITY+'\r\nX-Forwarded-For: 198.51.100.46\r\n\r\n').encode())
   response=http.client.HTTPResponse(connection);response.begin();self.assertEqual(response.status,200);response.read()
  self.assertIn(('203.0.113.1',False),self.server.limiter.entries)
  self.assertNotIn(('198.51.100.46',False),self.server.limiter.entries)
  for invalid in [b'GET / HTTP/1.1\r\n',b'PROXY TCP4 999.1.1.1 127.0.0.1 80 90\r\n',b'PROXY TCP4 198.51.100.45 198.51.100.47 0 90\r\n']:
   with self.assertRaises(ValueError):gateway.proxy_address(invalid)
 def test_chat_forwards_start_and_heartbeats_before_result(self):
  cookie=self.register_local('gateway-chat-stream')
  released=threading.Event()
  answer={'answer':'这是真实传输测试正文。','status':'answered','route':'agent_v2','agent_steps':[]}
  def chat(**kwargs):
   if not released.wait(3):raise RuntimeError('test_chat_timeout')
   kwargs['on_model']({'phase':'agent','finish_reason':'stop','content_chars':len(answer['answer'])})
   return answer
  self.backend.server.agent_v2=Mock()
  self.backend.server.agent_v2.chat.side_effect=chat
  connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
  try:
   with patch('app.wait',side_effect=lambda futures,timeout:wait(futures,timeout=0.05)):
    connection.request('POST','/api/chat',body=json.dumps({'question':'test','allow_context':True}),
                       headers={'Host':AUTHORITY,'Origin':'http://'+AUTHORITY,
                                'Content-Type':'application/json','Cookie':cookie,
                                'Accept':'text/event-stream','X-ATA-Token':__import__('app').CSRF_TOKEN})
    response=connection.getresponse()
    self.assertEqual(response.status,200)
    self.assertEqual(response.readline(),b'event: start\n')
    self.assertEqual(response.readline(),b'data: {}\n')
    self.assertEqual(response.readline(),b'\n')
    self.assertEqual(response.readline(),b'event: heartbeat\n')
    released.set()
    stream=response.read().decode()
   result_line=stream.split('event: result\ndata: ',1)[1].splitlines()[0]
   result=json.loads(result_line)
   self.assertEqual(result['answer'],answer['answer'])
   audit=self.backend.server.get_workflow().audit.read(result['trace_id'])
   self.assertEqual([kind for _,kind,_ in audit],['agent_chat_request','agent_model','agent_chat_result'])
  finally:released.set();connection.close()

 def test_stream_passes_first_chunk_without_waiting_for_completion(self):
  from http.server import BaseHTTPRequestHandler,HTTPServer
  release=threading.Event();started=threading.Event()
  class Stream(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):
    if self.path=='/api/account':
     raw=b'{"authenticated":true}';self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
   def do_POST(self):
    self.rfile.read(int(self.headers['Content-Length']));self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(b'event: text\ndata: first\n\n');self.wfile.flush();started.set();release.wait(4);self.wfile.write(b'event: done\ndata: end\n\n')
  source=HTTPServer(('127.0.0.1',0),Stream);worker=threading.Thread(target=source.serve_forever,daemon=True);worker.start();self.server.upstream=('127.0.0.1',source.server_port)
  connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
  try:
   connection.request('POST','/api/selection/explain',body=b'{}',headers={'Host':AUTHORITY,'Origin':'http://'+AUTHORITY,'Content-Type':'application/json','Cookie':'ata_account='+'a'*43})
   response=connection.getresponse();self.assertEqual(response.status,200);self.assertTrue(started.wait(1));self.assertEqual(response.readline(),b'event: text\n');self.assertFalse(release.is_set())
  finally:release.set();connection.close();source.shutdown();source.server_close();worker.join()

if __name__=='__main__':unittest.main()
