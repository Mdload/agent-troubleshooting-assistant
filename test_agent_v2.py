"""Agent v2 核心能力离线回归测试：自主多步推理、首次授权后直通、写操作保护、连续上下文、长期记忆与自学习。"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent_memory import AgentMemoryStore
from agent_v2 import AutonomousAgent, ToolsUnsupportedError
import app


FAKE_CONFIG = {
    'LLM_BASE_URL': 'https://api.inferera.com/v1',
    'LLM_MODEL_ID': 'test-reasoning-model',
    'LLM_API_KEY': 'offline-test-key',
}


class AgentV2CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = AgentMemoryStore(Path(self.tmp.name) / 'agent_memory.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_device_access_asks_once_then_direct_read_only_and_multi_step_reasoning(self):
        probe_calls = []

        def fake_probe(device, check):
            probe_calls.append((device, check))
            if device == 'local-primary' and check == 'services':
                return {'ok': True, 'output': 'Id=keepalived.service ActiveState=active\nId=croc-sar.service ActiveState=inactive', 'duration_ms': 40}
            if device == 'local-primary' and check == 'interfaces':
                return {'ok': True, 'output': 'eth2 UP 192.0.2.13/24', 'duration_ms': 35}
            if device == 'local-primary' and check == 'logs':
                return {'ok': True, 'output': '[unit=keepalived.service device_time_us=1] Entering BACKUP STATE', 'duration_ms': 30}
            return {'ok': True, 'output': 'ok', 'duration_ms': 20}

        llm_turns = iter([
            # 第1步：自主决定先查局端主机的服务和接口
            {'content': '', 'usage': 100, 'tool_calls': [
                {'id': 'c1', 'name': 'inspect_device', 'arguments': {'device': 'local-primary', 'check': 'services'}},
                {'id': 'c2', 'name': 'inspect_device', 'arguments': {'device': 'local-primary', 'check': 'interfaces'}},
            ]},
            # 授权恢复并拿到 services/interfaces 后，第2步：模型自主推理继续查 logs
            {'content': '', 'usage': 120, 'tool_calls': [
                {'id': 'c3', 'name': 'inspect_device', 'arguments': {'device': 'local-primary', 'check': 'logs'}},
            ]},
            # 第3步：综合推理给出自然回答，不被宿主模板覆盖
            {'content': '局端主机目前未持有 VIP 且 Keepalived 日志显示进入了 BACKUP 状态，因此 Croc 停止符合备机待机预期。', 'usage': 80, 'tool_calls': []},
            # 第二轮新提问：再次查 local-primary（已授权，应该零弹窗直通！）
            {'content': '', 'usage': 50, 'tool_calls': [
                {'id': 'c4', 'name': 'inspect_device', 'arguments': {'device': 'local-primary', 'check': 'routes'}},
            ]},
            {'content': '已直接查看局端主机路由，主路由正常。', 'usage': 40, 'tool_calls': []},
        ])

        agent = AutonomousAgent(
            self.memory, probe_fn=fake_probe, config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: next(llm_turns), background_learning=False
        )

        # 首次访问 local-primary -> 应暂停询问一次，且未调用任何设备探针
        first = agent.chat('user1', 'conv1', '帮我查一下局端主机为什么 Croc 停了')
        self.assertEqual(first['status'], 'device_permission_required')
        self.assertEqual([d['id'] for d in first['requested_devices']], ['local-primary'])
        self.assertEqual(probe_calls, [])

        # 用户点“同意只读进入（后续直通）” -> 续跑并自主完成 3 项检查（含后续自主追查 logs）
        resumed = agent.chat('user1', 'conv1', pending_id=first['pending_id'], grant_devices=['local-primary'])
        self.assertEqual(resumed['status'], 'answered')
        self.assertIn('BACKUP', resumed['answer'])
        self.assertEqual(probe_calls, [
            ('local-primary', 'services'),
            ('local-primary', 'interfaces'),
            ('local-primary', 'logs'),
        ])

        # 第二轮继续查 local-primary -> 已授权，不再询问直接查！
        second = agent.chat('user1', 'conv1', '再看看它的路由表')
        self.assertEqual(second['status'], 'answered')
        self.assertEqual(probe_calls[-1], ('local-primary', 'routes'))

        # 撤销授权后，再次访问需重新确认
        self.memory.revoke_device('user1', 'local-primary')
        llm_after_revoke = iter([
            {'content': '', 'usage': 30, 'tool_calls': [
                {'id': 'c5', 'name': 'inspect_device', 'arguments': {'device': 'local-primary', 'check': 'services'}},
            ]},
        ])
        agent.llm_client = lambda cfg, msgs, **kw: next(llm_after_revoke)
        third = agent.chat('user1', 'conv1', '再查一次服务')
        self.assertEqual(third['status'], 'device_permission_required')

    def test_continuous_context_preserves_assistant_replies_and_tool_summary(self):
        captured_messages = []

        def fake_llm(cfg, messages, **kw):
            captured_messages.append(messages)
            if len(captured_messages) == 1:
                return {'content': '第一轮已经确认局端主机当前是备机角色。', 'usage': 20, 'tool_calls': []}
            return {'content': '承接上一轮，现在我来解释备机如何接管。', 'usage': 25, 'tool_calls': []}

        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=fake_llm, background_learning=False
        )
        agent.chat('u1', 'c1', '局端主机现在是什么角色？')
        agent.chat('u1', 'c1', '那它什么时候会接管？')

        second_msgs = captured_messages[1]
        assistant_texts = [m['content'] for m in second_msgs if m['role'] == 'assistant']
        self.assertTrue(any('第一轮已经确认局端主机当前是备机角色' in t for t in assistant_texts))

    def test_self_learning_memory_recall_and_write_proposal_guard(self):
        # 1) Agent 主动保存纠错/排障经验 + 提出配置修改建议（绝不执行写操作）
        turns = iter([
            {'content': '', 'usage': 40, 'tool_calls': [
                {'id': 'l1', 'name': 'save_learning', 'arguments': {
                    'kind': 'preference', 'title': '简洁沟通', 'content': '交流时不展开代码细节，只说改了什么。', 'scope': '全局'
                }},
                {'id': 'p1', 'name': 'propose_config_change', 'arguments': {
                    'device': 'remote-backup', 'action': '启动 Keepalived', 'reason': 'Keepalived 未运行',
                    'commands': ['systemctl start keepalived'], 'rollback': 'systemctl stop keepalived'
                }},
            ]},
            {'content': '已记住你的沟通偏好，并整理了远端备机启动 Keepalived 的建议方案供你确认。', 'usage': 30, 'tool_calls': []},
        ])
        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: next(turns), background_learning=False
        )
        res = agent.chat('u1', 'c1', '以后记住不要展开代码细节，另外远端备机 Keepalived 停了怎么处理')
        self.assertEqual(res['status'], 'answered')
        self.assertEqual(len(res['saved_learnings']), 1)
        self.assertEqual(len(res['proposed_changes']), 1)
        self.assertFalse(res['proposed_changes'][0]['executed'])

        # 2) 全新对话自动召回刚刚学到的记忆进入 system prompt
        seen_sys = []
        agent.llm_client = lambda cfg, msgs, **kw: (seen_sys.append(msgs[0]['content']), {'content': '好的，直接说重点。', 'usage': 10, 'tool_calls': []})[1]
        res2 = agent.chat('u1', 'brand-new-conv', '帮我总结一下情况')
        self.assertEqual(res2['status'], 'answered')
        self.assertIn('简洁沟通', seen_sys[0])
        self.assertIn('不展开代码细节', seen_sys[0])

    def test_fallback_to_react_json_when_model_rejects_native_tools(self):
        calls = []

        def fake_llm(cfg, messages, tools=None, **kw):
            calls.append(bool(tools))
            if tools:
                raise ToolsUnsupportedError('400 tools unsupported')
            if len(calls) == 2:
                return {'content': '{"tool_calls":[{"name":"list_devices","arguments":{}}]}', 'usage': 15, 'tool_calls': []}
            return {'content': '当前已登记四台主备设备。', 'usage': 15, 'tool_calls': []}

        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=fake_llm, background_learning=False
        )
        res = agent.chat('u1', 'c2', '有哪些设备可以查？')
        self.assertEqual(res['status'], 'answered')
        self.assertIn('四台主备设备', res['answer'])
        self.assertEqual(len(res['agent_steps']), 1)
        self.assertEqual(res['agent_steps'][0]['tool'], 'list_devices')

    def test_rolling_summary_and_auto_learning_maintenance(self):
        for i in range(1, 10):
            self.memory.append_turn('u1', 'long-conv', '问题' + str(i), '回答' + str(i), '')

        maintenance_responses = iter([
            {'content': '摘要：前 3 轮已排查局端接口与服务，暂未定位远端问题。', 'usage': 20, 'tool_calls': []},
            {'content': '{"items":[{"kind":"troubleshooting_lesson","title":"远端无路由先查策略表","content":"ping 报无路由时优先检查 policy_routes","scope":"远端"}]}', 'usage': 25, 'tool_calls': []},
        ])
        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: next(maintenance_responses), background_learning=False
        )
        agent._post_turn_maintenance('u1', 'long-conv', '不对，其实应该先查策略表', '明白，先查策略路由。', '检查完成', True)

        summary = self.memory.get_summary('u1', 'long-conv')
        self.assertIsNotNone(summary)
        self.assertIn('前 3 轮', summary['summary'])
        recalled = self.memory.recall_memories('u1', '远端策略表')
        self.assertTrue(any(m['title'] == '远端无路由先查策略表' for m in recalled))

    def test_http_end_to_end_chat_grant_and_memory_management(self):
        from approval_store import ApprovalStore
        from lab_workflow import WorkflowService

        store = ApprovalStore(Path(self.tmp.name) / 'workflow.sqlite3')
        workflow = WorkflowService(store, probe=lambda d, c: {'ok': True, 'output': 'ACTIVE', 'duration_ms': 10})
        server = app.Server(('127.0.0.1', 0), app.Handler, workflow=workflow)
        llm_seq = iter([
            {'content': '', 'usage': 10, 'tool_calls': [
                {'id': 't1', 'name': 'inspect_device', 'arguments': {'device': 'local-backup', 'check': 'services'}}
            ]},
            {'content': '局端备机服务状态为 ACTIVE。', 'usage': 12, 'tool_calls': []},
        ])
        server.agent_memory = self.memory
        server.agent_v2 = AutonomousAgent(
            self.memory, probe_fn=lambda d, c: workflow.get_probe()(d, c),
            config_fn=lambda: FAKE_CONFIG, llm_client=lambda cfg, msgs, **kw: next(llm_seq),
            background_learning=False,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            def req(method, path, body=None, cookie=''):
                conn = http.client.HTTPConnection(host, port, timeout=10)
                headers = {'Host': '{}:{}'.format(host, port)}
                if cookie:
                    headers['Cookie'] = cookie
                if method == 'POST':
                    headers.update({
                        'Content-Type': 'application/json',
                        'Origin': 'http://{}:{}'.format(host, port),
                        'X-ATA-Token': app.CSRF_TOKEN,
                    })
                conn.request(method, path, body=json.dumps(body).encode('utf-8') if body is not None else None, headers=headers)
                resp = conn.getresponse()
                data = json.loads(resp.read().decode('utf-8'))
                set_cookie = resp.getheader('Set-Cookie') or ''
                conn.close()
                return resp.status, data, set_cookie.split(';', 1)[0]

            code, _, cookie = req('GET', '/api/session')
            self.assertEqual(code, 200)

            code, first, _ = req('POST', '/api/chat', {
                'question': '查一下局端备机服务', 'history': [], 'allow_context': True, 'conversation_id': 'c-http'
            }, cookie)
            self.assertEqual(code, 200)
            self.assertEqual(first['status'], 'device_permission_required')

            code, done, _ = req('POST', '/api/chat', {
                'question': '', 'history': [], 'allow_context': True, 'conversation_id': 'c-http',
                'pending_id': first['pending_id'], 'grant_devices': ['local-backup'],
            }, cookie)
            self.assertEqual(code, 200)
            self.assertEqual(done['status'], 'answered')
            self.assertIn('ACTIVE', done['answer'])

            # 通过既有 /api/conversations/<id>/memory 接口管理记忆和撤销授权
            code, mem, _ = req('GET', '/api/conversations/c-http/memory', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual([d['id'] for d in mem['agent_memory']['granted_devices']], ['local-backup'])

            code, after_add, _ = req('POST', '/api/conversations/c-http/memory', {
                'action': 'add_memory', 'kind': 'preference', 'title': '简短回复', 'content': '回答保持简短'
            }, cookie)
            self.assertEqual(code, 200)
            self.assertEqual(len(after_add['agent_memory']['memories']), 1)

            code, after_revoke, _ = req('POST', '/api/conversations/c-http/memory', {
                'action': 'revoke_device', 'device': 'local-backup'
            }, cookie)
            self.assertEqual(code, 200)
            self.assertEqual(after_revoke['agent_memory']['granted_devices'], [])
        finally:
            server.shutdown()
            server.server_close()

    def test_expired_and_mismatched_confirmation_do_not_grant_devices(self):
        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: {'content': '', 'usage': 10, 'tool_calls': [
                {'id': 'c1', 'name': 'inspect_device', 'arguments': {'device': 'remote-primary', 'check': 'services'}}
            ]},
            background_learning=False,
        )
        # 1) 过期/不存在的 pending_id 不应悄悄写入授权
        expired = agent.chat('u1', 'c1', pending_id='nonexistent-id', grant_devices=['remote-primary'])
        self.assertEqual(expired['status'], 'incomplete')
        self.assertEqual(self.memory.granted_devices('u1'), [])

        # 2) 待确认的是 remote-primary，若伪造传入 local-backup 也不应被写入授权
        first = agent.chat('u1', 'c1', '查远端主机服务')
        self.assertEqual(first['status'], 'device_permission_required')
        mismatch = agent.chat('u1', 'c1', pending_id=first['pending_id'], grant_devices=['local-backup'])
        self.assertEqual(mismatch['status'], 'device_permission_required')
        self.assertEqual(self.memory.granted_devices('u1'), [])

    def test_long_log_keeps_tail_and_earlier_key_events_and_polish_adds_table(self):
        filler = '\n'.join('2026-11-24 10:00:{:02d} INFO heartbeat tick {}'.format(i % 60, i) for i in range(260))
        long_log = (
            '2026-11-24 09:00:01 Keepalived_vrrp: Entering FAULT STATE early_event_marker\n'
            + filler
            + '\n2026-11-24 11:59:59 Keepalived_vrrp: Entering MASTER STATE latest_tail_marker'
        )
        self.memory.grant_devices('u1', ['remote-primary', 'remote-backup'])
        seen_outputs = []

        def fake_probe(device, check):
            if check == 'logs':
                return {'ok': True, 'output': long_log, 'duration_ms': 25}
            return {'ok': True, 'output': 'ACTIVE ' + device, 'duration_ms': 15}

        turns = iter([
            {'content': '', 'usage': 20, 'tool_calls': [
                {'id': 't1', 'name': 'inspect_device', 'arguments': {'device': 'remote-primary', 'check': 'logs'}},
                {'id': 't2', 'name': 'inspect_device', 'arguments': {'device': 'remote-backup', 'check': 'services'}},
            ]},
            {'content': '> **结论**：远端主备状态已核查完成（参考 KB-004 与 OBS-12）。', 'usage': 20, 'tool_calls': []},
        ])

        def fake_llm(cfg, msgs, **kw):
            for m in msgs:
                if m.get('role') == 'tool':
                    seen_outputs.append(m.get('content', ''))
            return next(turns)

        agent = AutonomousAgent(
            self.memory, probe_fn=fake_probe, config_fn=lambda: FAKE_CONFIG,
            llm_client=fake_llm, background_learning=False,
        )
        res = agent.chat('u1', 'c-log', '对比远端主备日志和服务')
        self.assertEqual(res['status'], 'answered')
        joined_tool = '\n'.join(seen_outputs)
        self.assertIn('latest_tail_marker', joined_tool)
        self.assertIn('early_event_marker', joined_tool)
        # 回答正文清理内部编号并自动补齐多设备速览表
        self.assertNotIn('KB-004', res['answer'])
        self.assertNotIn('OBS-12', res['answer'])
        self.assertIn('| 设备 | 检查项 | 状态 | 关键摘要 |', res['answer'])

    def test_live_progress_visible_during_slow_probe_and_clears_after(self):
        self.memory.grant_devices('u1', ['remote-primary'])
        probe_entered = threading.Event()
        probe_release = threading.Event()

        def slow_probe(device, check):
            probe_entered.set()
            probe_release.wait(timeout=5)
            return {'ok': True, 'output': 'eth4 UP 198.51.100.133', 'duration_ms': 120}

        turns = iter([
            {'content': '', 'usage': 10, 'tool_calls': [
                {'id': 'p1', 'name': 'inspect_device', 'arguments': {'device': 'remote-primary', 'check': 'interfaces'}}
            ]},
            {'content': '> **结论**：远端主机接口正常。', 'usage': 10, 'tool_calls': []},
        ])
        agent = AutonomousAgent(
            self.memory, probe_fn=slow_probe, config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: next(turns), background_learning=False,
        )
        holder = {}
        worker = threading.Thread(target=lambda: holder.setdefault('res', agent.chat('u1', 'c-prog', '查远端主机接口')))
        worker.start()
        self.assertTrue(probe_entered.wait(timeout=3))
        prog = agent.get_progress('u1', 'c-prog')
        self.assertTrue(prog['active'])
        self.assertIn('远端主机', prog['text'])
        probe_release.set()
        worker.join(timeout=5)
        self.assertEqual(holder['res']['status'], 'answered')
        self.assertFalse(agent.get_progress('u1', 'c-prog')['active'])

    def test_memory_recall_keeps_preference_always_and_correction_by_topic(self):
        self.memory.save_memory('u1', 'preference', '回答简洁', '先给结论再列要点', '全局')
        self.memory.save_memory('u1', 'correction', '局端策略路由纠正', '检查局端路由时不要漏看 policy_routes', '局端')
        unrelated = self.memory.recall_memories('u1', '今天天气和无关通用概念', limit=6)
        self.assertTrue(any(m['title'] == '回答简洁' for m in unrelated))
        self.assertFalse(any(m['title'] == '局端策略路由纠正' for m in unrelated))
        related = self.memory.recall_memories('u1', '局端 policy_routes 路由异常', limit=6)
        self.assertTrue(any(m['title'] == '局端策略路由纠正' for m in related))

    def test_memory_shared_promotion_supersede_and_semantic_merge(self):
        # 1) 旧规律被新规律 supersede 后不再被召回
        old = self.memory.save_memory('u1', 'topology_fact', '旧回程下一跳', '局端回程走 192.0.2.23', '局端')
        new = self.memory.save_memory(
            'u1', 'topology_fact', '新回程下一跳', 'Router 七 VIP 部署后局端回程走 192.0.2.25', '局端',
            supersede_id=old['id'],
        )
        recalled = self.memory.recall_memories('u1', '局端回程下一跳 192.0.2.23 192.0.2.25', limit=6)
        ids = [m['id'] for m in recalled]
        self.assertIn(new['id'], ids)
        self.assertNotIn(old['id'], ids)

        # 2) 个人偏好不允许共享给全实验室，排障经验共享后其他账号可直接召回
        pref = self.memory.save_memory('u1', 'preference', '先说结论', '不要展开代码细节', '全局')
        with self.assertRaises(ValueError):
            self.memory.set_shared('u1', pref['id'], True)
        lesson = self.memory.save_memory(
            'u1', 'troubleshooting_lesson', '四台同时离线先查251入口',
            '监控四台同时 UNKNOWN 时先核对 140 到 251 管理隧道，不急于重启四台设备。', '管理入口',
            tags='UNKNOWN 251 管理隧道',
        )
        self.assertTrue(self.memory.set_shared('u1', lesson['id'], True))
        u2_recalled = self.memory.recall_memories('u2', '四台设备同时 UNKNOWN 离线怎么查', limit=6, record_hits=True)
        self.assertTrue(any(m['id'] == lesson['id'] and m['shared'] for m in u2_recalled))
        self.assertFalse(any(m['id'] == pref['id'] for m in u2_recalled))

        # 3) 语义近重复自动合并并提升验证次数，不产生重复碎片
        merged = self.memory.save_memory(
            'u1', 'troubleshooting_lesson', '四台同时离线先查251管理入口',
            '更新：先核对 140 到 251 的 tun5 隧道及远端独立内层探测。', '管理入口',
        )
        self.assertEqual(merged['id'], lesson['id'])
        self.assertGreaterEqual(merged['confidence'], 2)

    def test_feedback_learning_reflection_and_conversation_consolidation(self):
        # 1) 反馈“判断有误”走模型反思并替代旧假设；反馈“定位准确”沉淀可共享经验
        old_wrong = self.memory.save_memory('u1', 'topology_fact', '旧判断认为VIP丢失', '以为是局端VIP故障', '局端')
        llm_seq = iter([
            {'content': json.dumps({'items': [{
                'action': 'supersede', 'target_id': old_wrong['id'],
                'kind': 'correction', 'title': '有线不通先核Router回程',
                'content': '原误判为VIP故障；真实规律：先查Router是否有卫星源回程路由。',
                'scope': 'Router回程', 'tags': '有线 卫星 回程路由',
            }]}, ensure_ascii=False), 'usage': 30, 'tool_calls': []},
            {'content': json.dumps({'items': [{
                'action': 'create', 'target_id': '',
                'kind': 'troubleshooting_lesson', 'title': '普通ping不通要对比绑定源',
                'content': '现象：普通ping失败；排查顺序：先查默认选路再对比绑定源有线探测；边界：现场路由可能变化。',
                'scope': '选路排障', 'tags': '普通ping 绑定源 主路由',
            }]}, ensure_ascii=False), 'usage': 30, 'tool_calls': []},
            {'content': json.dumps({'items': [{
                'action': 'create', 'target_id': '',
                'kind': 'troubleshooting_lesson', 'title': '整段排障复盘：区分管理与业务通路',
                'content': '管理入口中断时业务内层可能仍通，需独立探测确认。',
                'scope': '全拓扑', 'tags': '管理入口 业务内层',
            }]}, ensure_ascii=False), 'usage': 30, 'tool_calls': []},
        ])
        agent = AutonomousAgent(
            self.memory, probe_fn=Mock(), config_fn=lambda: FAKE_CONFIG,
            llm_client=lambda cfg, msgs, **kw: next(llm_seq), background_learning=False,
        )
        corrected = agent.learn_from_feedback(
            'u1', 'c-fb', 'incorrect', '三条有线都通为什么回不来？', '可能是VIP掉了',
            note='不是VIP问题，是Router缺卫星源回程路由',
        )
        self.assertEqual(corrected['kind'], 'correction')
        self.assertIn('Router', corrected['title'])
        # 旧记忆已被替代，不再出现在列表中
        self.assertFalse(any(m['id'] == old_wrong['id'] for m in self.memory.list_memories('u1')))

        confirmed = agent.learn_from_feedback(
            'u1', 'c-fb', 'accurate', '普通ping失败但绑定源通了怎么回事？', '默认选路走了管理PPP',
            share_to_lab=True,
        )
        self.assertTrue(confirmed['shared'])
        self.assertIn('绑定源', confirmed['title'])

        # 2) 一键总结整段对话沉淀经验
        self.memory.append_turn('u1', 'c-fb', '四台都UNKNOWN了', '先查251管理入口和远端内层。', '检查完成')
        consolidated = agent.consolidate_conversation('u1', 'c-fb')
        self.assertEqual(len(consolidated), 1)
        self.assertIn('区分管理与业务通路', consolidated[0]['title'])


if __name__ == '__main__':
    unittest.main()
