"""正常连续使用、隔离、限额和取消能力；仅临时HTTP及确定性替身。"""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from agent.agent_memory import AgentMemoryStore
from agent.agent_v2 import AutonomousAgent
from server.request_limits import RequestLimiter
from tests.test_agent_v2 import FAKE_CONFIG
from tests.workflow_test_support import RunningApp


def general(context):
    return {'action':'answer','answer':'列表按顺序保存元素，可以修改。','refs':[],'basis':'general'},0


class RequestLimitTests(unittest.TestCase):
    def test_window_and_bounded_bucket_storage_keep_protection(self):
        now=[0.0]
        limiter=RequestLimiter(clock=lambda:now[0],max_buckets=4)
        for _ in range(12):self.assertTrue(limiter.allow('A','model'))
        self.assertFalse(limiter.allow('A','model'))
        self.assertTrue(limiter.allow('B','model'))
        self.assertTrue(limiter.allow('C','model'))
        self.assertFalse(limiter.allow('D','model'))
        now[0]=60.0
        self.assertTrue(limiter.allow('D','model'))
        self.assertEqual(len(limiter.buckets),2)

    def test_storage_and_control_expire_at_their_own_window(self):
        for use, capacity, window in (('storage', 120, 600), ('control', 30, 120)):
            with self.subTest(category=use):
                now = [0.0]
                limiter = RequestLimiter(clock=lambda: now[0])
                for _ in range(capacity):
                    self.assertTrue(limiter.allow('owner', use))
                global_capacity = RequestLimiter.LIMITS[use][1]
                for index in range(capacity, global_capacity):
                    self.assertTrue(limiter.allow('extra-' + str(index), use))
                for elapsed in (60, window - 0.001):
                    now[0] = elapsed
                    self.assertTrue(limiter.allow('owner', 'auth'))
                    self.assertFalse(limiter.allow('owner', use))
                    self.assertFalse(limiter.allow('another', use))
                now[0] = window
                self.assertTrue(limiter.allow('owner', use))

    def test_same_account_conversations_overlap_until_http_model_limit(self):
        entered = threading.Barrier(7)
        release = threading.Event()

        def llm(config, messages, **kwargs):
            entered.wait(5)
            if not release.wait(5):
                raise AssertionError('相互独立的请求没有同时进入模型')
            return {'content': '模型回答', 'usage': 1, 'tool_calls': []}

        with RunningApp() as client:
            client.server.agent_v2 = AutonomousAgent(
                AgentMemoryStore(client.root / 'test-agent.db'), config_fn=lambda: FAKE_CONFIG,
                llm_client=llm, background_learning=False,
            )
            code, _ = client.call('/api/account/register', {
                'username': 'parallel-user', 'password': 'test-only-password', 'allow_storage': True,
            })
            self.assertEqual(code, 200)
            payload = {'question': 'Python列表是什么', 'history': [], 'allow_context': True}
            with ThreadPoolExecutor(max_workers=6) as pool:
                pending = [pool.submit(client.call, '/api/chat',
                                       dict(payload, conversation_id='chat-{}'.format(index)), timeout=10)
                           for index in range(6)]
                try:
                    entered.wait(5)
                    self.assertEqual(client.call('/api/chat', dict(payload, conversation_id='chat-extra'))[0], 429)
                finally:
                    release.set()
                for request in pending:
                    code, result = request.result(timeout=5)
                    self.assertEqual((code, result['status']), (200, 'answered'))

    def test_multiple_owners_still_have_a_global_model_limit(self):
        limiter=RequestLimiter()
        for user in range(5):
            for _ in range(12):self.assertTrue(limiter.allow(str(user),'model'))
        self.assertFalse(limiter.allow('next','model'))
        self.assertTrue(limiter.allow('next','control'))

    def test_storage_and_conversation_initialization_do_not_consume_chat_quota(self):
        planner=Mock(side_effect=general);probe=Mock()
        with RunningApp(planner,probe,lookup=lambda q:[]) as client:
            for _ in range(20):
                self.assertEqual(client.call('/api/conversations/rate/mode',{'mode':'assistant','scene':None})[0],200)
            self.assertEqual(client.chat('Python列表是什么',conversation_id='rate')[0],200)
        self.assertEqual(planner.call_count,1)
        probe.assert_not_called()

    def test_one_browser_at_model_limit_does_not_block_another_browser(self):
        planner=Mock(side_effect=general)
        with RunningApp(planner,Mock(),lookup=lambda q:[]) as client:
            other=client.new_browser()
            client.call('/api/session',browser=other)
            for _ in range(12):self.assertEqual(client.chat('Python列表是什么')[0],200)
            code,result=client.chat('Python列表是什么')
            self.assertEqual(code,429)
            self.assertEqual(result['code'],'rate_limited')
            data={'question':'Python列表是什么','history':[],'allow_context':True}
            self.assertEqual(client.call('/api/chat',data,browser=other)[0],200)
            self.assertEqual(planner.call_count,13)

    def test_two_sessions_of_same_account_share_model_quota(self):
        with RunningApp(Mock(side_effect=general),Mock(),lookup=lambda q:[]) as client:
            data={'username':'ratetest','password':'test-only-long-password','allow_storage':True}
            self.assertEqual(client.call('/api/account/register',data)[0],200)
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/account/login',{'username':data['username'],'password':data['password']},browser=other)[0],200)
            for _ in range(12):self.assertEqual(client.chat('Python列表是什么')[0],200)
            code,result=client.call('/api/chat',{'question':'Python列表是什么','history':[],'allow_context':True},browser=other)
            self.assertEqual(code,429)
            self.assertEqual(result['code'],'rate_limited')

    def test_exhausted_model_slots_still_allow_cancel_and_logout(self):
        with RunningApp(Mock(side_effect=general),Mock(),lookup=lambda q:[]) as client:
            task=client.diagnostic_chat('局端备机当前Croc状态')[1]['task']
            for _ in range(6):
                self.assertTrue(client.server.model_slots.acquire(timeout=2))
            try:
                self.assertEqual(client.chat('Python列表是什么')[0],429)
                code,result=client.call('/api/tasks/'+task['id']+'/cancel',{})
                self.assertEqual(code,200)
                self.assertEqual(result['status'],'cancelled')
                self.assertEqual(client.call('/api/account/logout',{})[0],200)
            finally:
                for _ in range(6):
                    client.server.model_slots.release()


if __name__=='__main__':unittest.main()
