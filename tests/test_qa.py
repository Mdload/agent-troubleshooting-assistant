"""离线验证检索、模式边界与模型失败降级，不消耗API额度。"""
if not __package__:
    import _bootstrap

import unittest
from unittest.mock import patch
import qa

class QAEdges(unittest.TestCase):
    def test_retrieval(self):
        examples = [
            ("无线全断了，有线能ping通但没有VIP，应该核对什么？", "HA-001"),
            ("ppp5没有载波和IP，是DHCP坏了吗？", "HA-007"),
            ("备机没有VIP，Croc启动被跳过，可以删除ExecCondition吗？", "HA-004"),
            ("Croc重新拉起后tun16地址和业务路由缺失", "HA-005"),
            ("固定主机测试模式退出失败", "HA-006"),
        ]
        for query, expected in examples:
            with self.subTest(query=query):
                self.assertEqual(qa.retrieve(query)[0]["case"]["id"], expected)

    def test_unknown_does_not_call_model(self):
        with patch("qa.ask_model") as model:
            result=qa.answer("数据库连接池耗尽怎么处理？", allow_new_topology=True)
            self.assertEqual(result["kind"], "insufficient")
            model.assert_not_called()

    def test_mode_filter(self):
        self.assertNotIn("HA-005", [h["case"]["id"] for h in qa.retrieve("Croc重新拉起后tun16地址和业务路由缺失", "normal_ha")])

    def test_failure_falls_back_without_leaking(self):
        with patch("qa.ask_model", side_effect=RuntimeError("SECRET-SENTINEL")):
            result=qa.answer("ppp5没有载波和IP", allow_new_topology=True)
            self.assertEqual(result["kind"], "extract")
            self.assertIn("未修复", str(result["hits"]))
            self.assertIn("未完成回答", result["answer"])
            self.assertNotIn("SECRET-SENTINEL", str(result))

    def test_sources(self):
        self.assertEqual(len({c["id"] for c in qa.CASES}), len(qa.CASES))
        for c in qa.CASES:
            self.assertFalse(c["repair_enabled"])
            self.assertTrue(set(c["sources"]) <= set(qa.SOURCE_MAP))
if __name__=="__main__":
    unittest.main()
