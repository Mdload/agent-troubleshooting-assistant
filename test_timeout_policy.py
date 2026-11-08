"""较长单次等待仍受宿主剩余时间约束；不重试。"""
import socket,unittest
from unittest.mock import patch
from chat_model import make_model,ModelFailure


class TimeoutPolicyTests(unittest.TestCase):
    def execute(self,context):
        with patch('chat_model.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=socket.timeout()
            planner=make_model({'LLM_BASE_URL':'https://api.inferera.com/v1','LLM_API_KEY':'test','LLM_MODEL_ID':'test'})
            with self.assertRaises(ModelFailure):planner(context)
            self.assertEqual(factory.return_value.open.call_count,1)
            self.assertEqual(planner.last_metrics['timeout_seconds'],factory.return_value.open.call_args[1]['timeout'])
            return planner.last_metrics['timeout_seconds']
    def test_default_and_long_remaining_use_sixty_five_seconds(self):
        self.assertEqual(self.execute({}),65)
        self.assertEqual(self.execute({'remaining_seconds':180}),65)
    def test_short_remaining_caps_the_request(self):
        self.assertEqual(self.execute({'remaining_seconds':12.5}),12.5)
        self.assertEqual(self.execute({'remaining_seconds':0.001}),0.001)


if __name__=='__main__':unittest.main()
