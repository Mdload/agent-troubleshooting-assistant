"""实验室口令仅用于注册；真实临时HTTP/SQLite，无模型或设备调用。"""
import hashlib,unittest
from unittest.mock import Mock,patch
from tests.workflow_test_support import RunningApp
from server.user_accounts import AccountStore,AccountError

class LabRegistrationTests(unittest.TestCase):
 def setUp(self):
  self.patch=patch('server.user_accounts.ITERATIONS',1000);self.patch.start()
  self.client=RunningApp(planner=Mock(),probe=Mock(),registration_password=None).__enter__()
  self.accounts=self.client.server.get_accounts()
  self.original_token,self.original=self.accounts.register('原有用户','1',True)
 def tearDown(self):self.client.__exit__();self.patch.stop()
 def login(self,name='原有用户',password='1',browser=None):return self.client.call('/api/account/login',{'username':name,'password':password},browser=browser)
 def register(self,name='新同学',password='字',lab='实验室口令',browser=None):
  return self.client.call('/api/account/register',{'username':name,'password':password,'lab_password':lab,'allow_storage':True},browser=browser)
 def set_lab(self,password='实验室口令',revision=0,browser=None):
  return self.client.call('/api/account/registration',{'password':password,'expected_revision':revision},browser=browser)
 def test_unset_registration_is_closed_and_existing_login_needs_no_lab_password(self):
  self.assertEqual(self.client.call('/api/account/registration')[1],{'configured':False,'revision':0})
  self.assertEqual(self.register()[1]['reason'],'lab_password_not_configured')
  self.assertEqual(self.set_lab()[0],401);self.assertEqual(self.login()[0],200)
  self.assertEqual(self.set_lab()[0],200)
  self.assertEqual(self.client.call('/api/account')[1]['account']['id'],self.original['id'])
 def test_wrong_missing_and_forged_registration_data_never_create_accounts(self):
  self.accounts.configure_registration('实验室口令',0)
  for lab in ('','wrong'):self.assertIn(self.register(lab=lab)[1]['reason'],('lab_password_required','lab_password_invalid'))
  payload={'username':'新同学','password':'字','allow_storage':True}
  self.assertEqual(self.client.call('/api/account/register',payload)[0],400)
  self.assertEqual(self.client.call('/api/account/register',dict(payload,lab_password='实验室口令',role='admin'))[0],400)
  with self.accounts.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM user_accounts').fetchone()[0],1)
 def test_unicode_short_long_and_punctuation_credentials_register_and_login(self):
  self.accounts.configure_registration('实验室口令',0)
  for name,password in [('中','1'),('空 格😀/','密'*300),('n'*100,'p'*300)]:
   code,result=self.register(name,password);self.assertEqual(code,200,result)
   self.assertEqual(self.login(name,password)[1]['account']['id'],result['account']['id'])
  self.client.service.planner.assert_not_called();self.client.service.probe.assert_not_called()
 def test_any_logged_in_user_can_change_and_old_accounts_and_sessions_still_work(self):
  self.login();self.assertEqual(self.set_lab()[0],200)
  other=self.client.new_browser();self.client.call('/api/session',browser=other)
  self.assertEqual(self.register(browser=other)[0],200)
  self.assertEqual(self.set_lab('新口令',1,browser=other)[0],200)
  self.assertEqual(self.register('old-code-user',lab='实验室口令')[1]['reason'],'lab_password_invalid')
  self.assertEqual(self.register('新口令同学',lab='新口令')[0],200)
  self.assertIsNotNone(self.accounts.resolve(self.original_token));self.assertEqual(self.login()[0],200)
 def test_revision_guard_rejects_stale_updates_and_metadata_hides_secret_hashes(self):
  self.login();self.assertEqual(self.set_lab()[0],200)
  self.assertEqual(self.set_lab('second',0)[1]['reason'],'lab_password_changed')
  self.assertEqual(set(self.client.call('/api/account/registration')[1]),{'configured','revision'})
  with self.accounts.connect() as db:dump='\n'.join(db.iterdump())
  self.assertNotIn('实验室口令',dump)
 def test_rotation_between_verification_and_insert_refuses_stale_code(self):
  self.accounts.configure_registration('old-code',0);real=hashlib.pbkdf2_hmac;changed=[False]
  def calculate(*args):
   if args[1]==b'new-account-password' and not changed[0]:changed[0]=True;self.accounts.configure_registration('new-code',1)
   return real(*args)
  with patch('server.user_accounts.hashlib.pbkdf2_hmac',side_effect=calculate):
   with self.assertRaisesRegex(AccountError,'lab_password_changed'):self.accounts.register_invited('race','new-account-password',True,'old-code')
  with self.accounts.connect() as db:self.assertIsNone(db.execute('SELECT id FROM user_accounts WHERE name=?',('race',)).fetchone())
 def test_settings_require_live_account_csrf_and_same_origin(self):
  self.login();payload={'password':'lab','expected_revision':0}
  self.assertEqual(self.client.call('/api/account/registration',payload,token=False)[0],403)
  self.assertEqual(self.client.call('/api/account/registration',payload,origin='http://evil.example')[0],403)
  self.client.call('/api/account/logout',{});self.assertEqual(self.client.call('/api/account/registration',payload)[0],401)
 def test_old_database_keeps_existing_account_when_settings_table_is_added(self):
  with self.accounts.connect() as db:db.execute('DROP TABLE lab_registration')
  reopened=AccountStore(self.accounts.path)
  self.assertEqual(reopened.login('原有用户','1')[1]['id'],self.original['id']);self.assertFalse(reopened.registration()['configured'])
 def test_logout_during_password_hashing_prevents_settings_write(self):
  real=hashlib.pbkdf2_hmac
  def calculate(*args):
   if args[1]==b'new-lab-code':self.accounts.logout(self.original_token)
   return real(*args)
  with patch('server.user_accounts.hashlib.pbkdf2_hmac',side_effect=calculate):
   with self.assertRaisesRegex(AccountError,'account_session_expired'):
    self.accounts.configure_registration('new-lab-code',0,self.original_token)
  self.assertFalse(self.accounts.registration()['configured'])

if __name__=='__main__':unittest.main()
