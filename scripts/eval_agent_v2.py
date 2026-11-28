"""Agent v2 离线场景回归。运行 python scripts/eval_agent_v2.py。"""
import contextlib
import io
from itertools import count
import json
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PRIMARY = 'remote-primary'
BACKUP = 'remote-backup'
OWNER = 'eval-user'
TOOL_IDS = count(1)
CONFIG = {
    'LLM_API_KEY': 'offline-fixture',
    'LLM_MODEL_ID': 'offline-fixture',
    'LLM_BASE_URL': 'https://model.invalid/v1',
    'LLM_APPROVED_BASE_URLS': ['https://model.invalid/v1'],
}

INTERNAL_ID = re.compile(r'\b(?:OBS|KB|HA)-\d+\b', re.I)
TABLE_DIVIDER = re.compile(r':?-{3,}:?')


def expect(condition, detail):
    if not condition:
        raise AssertionError(detail)


def forbidden(*args, **kwargs):
    raise AssertionError('离线评测禁止访问网络、创建子进程或加载真实模型配置')


def model_reply(content='', calls=()):
    return {'content': content, 'tool_calls': list(calls), 'usage': 10}


def tool(name, **arguments):
    return {'id': 'eval-{}'.format(next(TOOL_IDS)), 'name': name, 'arguments': arguments}


def probe_call(device, check):
    return tool('inspect_device', device=device, check=check)


def tool_results(messages):
    return [json.loads(msg['content']) for msg in messages if msg['role'] == 'tool']


def answer_layout_issues(answer, comparison=False):
    """检查正文的结论、可扫读结构和常见 Markdown 表格错误。"""
    visible, fence = [], None
    for line in answer.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            char = marker.group(1)[0]
            if fence is None:
                fence = char
            elif fence == char:
                fence = None
        elif fence is None:
            visible.append(line.strip())
    issues = []
    if fence:
        issues.append('代码块未闭合')
    first = next((line for line in visible if line), '')
    if not re.match(r'^>\s*\*\*结论\*\*\s*[：:]\s*\S', first):
        issues.append('开头缺少结论引用块')
    if INTERNAL_ID.search('\n'.join(visible)):
        issues.append('正文出现内部编号')
    if comparison and not any(re.match(r'^#{2,4}\s+\S', line) for line in visible):
        issues.append('对比回答缺少小标题')
    tables = 0
    index = 0
    while index < len(visible):
        line = visible[index]
        if not ('|' in line and (line.startswith('|') or line.endswith('|'))):
            index += 1
            continue
        start = index
        rows = []
        while index < len(visible) and '|' in visible[index]:
            rows.append(visible[index])
            index += 1
        cells = [re.split(r'(?<!\\)\|', row[1:-1]) for row in rows]
        columns = len(cells[0])
        valid = (len(rows) >= 3 and 3 <= columns <= 5
                 and all(row.startswith('|') and row.endswith('|') for row in rows)
                 and all(len(row) == columns for row in cells)
                 and all(TABLE_DIVIDER.fullmatch(cell.strip()) for cell in cells[1]))
        if valid:
            tables += 1
        else:
            issues.append('表格第 {} 行附近的边界、分隔行或列数有误'.format(start + 1))
    if comparison and not tables:
        issues.append('对比回答缺少有效表格')
    return issues


class Replay:
    def __init__(self, memory, turns=(), outputs=None, client=None, **options):
        self.memory = memory
        self.turns = iter(turns)
        self.outputs = outputs or {}
        self.probes = []
        self.messages = []
        self.workers = queue.Queue()
        self.agent = agent_v2.AutonomousAgent(
            memory, probe_fn=self.probe, config_fn=lambda: CONFIG,
            llm_client=client or self.model, background_learning=False, **options
        )

    def model(self, config, messages, **kwargs):
        self.messages.append((list(messages), bool(kwargs.get('tools'))))
        try:
            reply = next(self.turns)
        except StopIteration:
            raise AssertionError('模型调用超出预设回放轮次') from None
        return reply(messages) if callable(reply) else reply

    def probe(self, device, check):
        pair = (device, check)
        self.probes.append(pair)
        result = self.outputs[pair]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict):
            return result
        return {'ok': True, 'output': result, 'collected_at': '2026-01-01T00:00:00Z'}

    def chat(self, question='', conversation='eval-conversation', owner=OWNER, **kwargs):
        return self.agent.chat(owner, conversation, question, **kwargs)

    def answer(self, question='', **kwargs):
        result = self.chat(question, **kwargs)
        expect(result['status'] == 'answered', '预期完成回答，实际：{}'.format(result))
        return result

    def enable_background(self):
        maintain = self.agent._post_turn_maintenance

        def observe_worker(*args):
            self.workers.put(threading.current_thread())
            maintain(*args)

        self.agent._post_turn_maintenance = observe_worker
        self.agent.background_learning = True

    def wait_background(self):
        try:
            worker = self.workers.get(timeout=5)
        except queue.Empty:
            raise AssertionError('后台维护线程未启动') from None
        expect(worker is not threading.current_thread(), '维护任务占用了前台线程')
        worker.join(timeout=5)
        expect(not worker.is_alive(), '后台维护线程未完成')


def diagnostic_chain(memory):
    outputs = {
        (PRIMARY, 'services'): 'keepalived active; croc inactive',
        (PRIMARY, 'interfaces'): 'eth0: VIP absent',
        (BACKUP, 'services'): 'keepalived active; croc active',
        (BACKUP, 'interfaces'): 'eth0: business VIP present',
        (BACKUP, 'logs'): 'Keepalived transitioned to MASTER',
        (BACKUP, 'policy_routes'): 'table 206: default dev ppp6',
    }
    expected = [(PRIMARY, 'services'), (PRIMARY, 'interfaces'),
                (BACKUP, 'services'), (BACKUP, 'interfaces'),
                (BACKUP, 'logs'), (BACKUP, 'policy_routes')]
    answer = ('> **结论**：模拟证据指向备机已接管。是否端到端可用仍待确认。\n\n'
              '### 主备对照\n\n| 设备 | VIP | Croc |\n| --- | --- | --- |\n'
              '| 远端主机 | 未见 | 停止 |\n| 远端备机 | 已持有 | 运行 |\n\n'
              '### 下一步\n\n从登记对端验证隧道连通性。')

    def synthesize(messages):
        results = tool_results(messages)
        expect(len(results) == 8 and results[0]['count'] > 0, '缺少实际检索结果或顺序不符')
        listed = {row['id']: row for row in results[1]['devices']}
        expect('policy_routes' in listed[BACKUP]['checks'], '目录未列出策略路由')
        expect([row['output'] for row in results[2:]] == [outputs[pair] for pair in expected],
               '综合阶段未收到各设备的完整模拟观测')
        return model_reply(answer)

    memory.grant_devices(OWNER, [PRIMARY, BACKUP])
    run = Replay(memory, [
        model_reply(calls=[tool('search_knowledge', query='远端 Keepalived VIP 接管')]),
        model_reply(calls=[tool('list_devices')]),
        model_reply(calls=[probe_call(*pair) for pair in expected[:4]]),
        model_reply(calls=[probe_call(*pair) for pair in expected[4:]]),
        synthesize,
    ], outputs)
    result = run.answer('远端还能 ping 通，为什么 Croc 业务断了？')
    expect(run.probes == expected, '主备探针执行顺序不符')
    expect(len(result['agent_steps']) == 8, '缺少用户可见的检索或设备步骤')
    expect(result['answer'] == answer, '模拟综合回答未原样交付')
    expect(not answer_layout_issues(result['answer'], comparison=True), '模拟综合回答结构异常')


def device_consent(memory):
    run = Replay(memory, [
        model_reply(calls=[probe_call(PRIMARY, 'services')]),
        model_reply('已取得主机服务状态。'),
        model_reply(calls=[probe_call(PRIMARY, 'policy_routes')]),
        model_reply('已读取已授权设备的策略路由。'),
        model_reply(calls=[probe_call(BACKUP, 'services')]),
        model_reply(calls=[probe_call(PRIMARY, 'services')]),
    ], {(PRIMARY, 'services'): 'croc active', (PRIMARY, 'policy_routes'): 'table 206'})

    def pending(result, device, expected_probes):
        expect(result['status'] == 'device_permission_required', '未拦截首次访问')
        expect([item['id'] for item in result['requested_devices']] == [device], '确认设备不符')
        expect(run.probes == expected_probes, '用户确认前执行了探针')
        return result['pending_id']

    first = pending(run.chat('先查远端主机服务'), PRIMARY, [])
    run.answer(pending_id=first, grant_devices=[PRIMARY])
    run.answer('再查这台的策略路由', conversation='follow-up-conversation')
    observed = [(PRIMARY, 'services'), (PRIMARY, 'policy_routes')]
    second = pending(run.chat('备机也看一下'), BACKUP, observed)
    run.answer(pending_id=second, deny_pending=True)
    expect(memory.granted_devices(OWNER) == [PRIMARY], '拒绝后备机被授权')
    memory.revoke_device(OWNER, PRIMARY)
    pending(run.chat('再查主机服务'), PRIMARY, observed)


def writes_are_proposals(memory):
    memory.grant_devices(OWNER, [PRIMARY])
    proposal = tool('propose_config_change', device=PRIMARY, action='审核服务配置并人工重启',
                    reason='演练配置需要核对', commands=['systemctl restart keepalived'],
                    rollback='恢复演练配置备份后由操作员重新加载')

    def synthesize(messages):
        rejected, unknown, proposed = tool_results(messages)
        expect(rejected.get('error') == 'unsupported_device_or_check', '写操作混入只读探针')
        expect(unknown.get('error') == 'unknown_tool', '执行型工具未拦截')
        expect(proposed.get('executed') is False, '建议被标记为已执行')
        return model_reply('已整理供人工审核的变更建议。')

    run = Replay(memory, [
        model_reply(calls=[probe_call(PRIMARY, 'restart'),
                           tool('run_command', command='systemctl restart keepalived'), proposal]),
        synthesize,
    ])
    result = run.answer('改服务配置然后重启 Keepalived')
    cards = result['proposed_changes']
    expect(len(cards) == 1 and cards[0]['executed'] is False, '缺少未执行的变更建议卡')
    expect(cards[0]['commands'] and cards[0]['rollback'] and not run.probes, '建议缺少命令或回滚，或意外进入探针')


def context_continuity(memory):
    run = Replay(memory, [
        model_reply(calls=[probe_call(BACKUP, 'interfaces')]),
        model_reply('上一轮结论：备机持有业务 VIP。'),
        model_reply('持有 VIP 仍需核对隧道。'),
        model_reply('下一步验证对端连通性。'),
    ], {(BACKUP, 'interfaces'): 'eth0: business VIP present'})
    memory.grant_devices(OWNER, [BACKUP])
    run.answer('目前哪台持有 VIP？')
    run.answer('那是不是已经恢复了？')
    run.answer('下一步呢？')
    followup = run.messages[-1][0]
    expect(any(msg['role'] == 'assistant'
               and '上一轮结论：备机持有业务 VIP' in msg['content']
               and 'eth0: business VIP present' not in msg['content'] for msg in followup),
           '助手历史应保留上一轮回答且剥离工具证据')
    expect(followup[0]['role'] == 'system' and 'eth0: business VIP present' in followup[0]['content'],
           '后续模型上下文丢失历史工具证据')
    expect(any(msg['role'] == 'assistant' and '仍需核对隧道' in msg['content'] for msg in followup),
           '最近一次结论未带入追问')


def rolling_summary(memory):
    conversation = 'long-conversation'
    ids = [memory.append_turn(OWNER, conversation, '早期问题 {}'.format(i), '答复 {}'.format(i),
                              '已见实验室模拟基线' if i == 0 else '') for i in range(8)]
    summary = '早期目标是定位业务不可达，已见实验室模拟基线。'

    def summarize(messages):
        expect('已见实验室模拟基线' in messages[-1]['content'], '摘要调用缺少早期工具证据')
        return model_reply(summary)

    run = Replay(memory, [model_reply('收到，继续查。'), summarize, model_reply('接着查业务路径。')])
    run.enable_background()
    run.answer('继续核查', conversation=conversation)
    run.wait_background()
    stored = memory.get_summary(OWNER, conversation)
    expect(stored and stored['summary'] == summary and stored['last_turn_id'] == ids[2],
           '未触发滚动摘要，或压缩边界不符')
    run.agent.background_learning = False
    run.answer('回到最早的目标', conversation=conversation)
    prompt = run.messages[-1][0]
    expect(summary in prompt[0]['content'], '摘要没有注入新一轮模型上下文')
    expect(all(msg.get('content') != '早期问题 0' for msg in prompt), '最旧原文未滚出窗口')


def explicit_learning(memory):
    save = tool('save_learning', kind='preference', title='简洁沟通',
                content='先给两句结论', scope='全局')
    run = Replay(memory, [model_reply(calls=[save]), model_reply('已记录偏好。'),
                          model_reply('按偏好简答。'), model_reply('其他人的新对话。')])
    result = run.answer('以后记住先给两句结论')
    expect(len(result['saved_learnings']) == 1 and len(memory.list_memories(OWNER)) == 1, '显式记忆未保存')
    run.answer('总结一下', conversation='brand-new')
    expect('先给两句结论' in run.messages[-1][0][0]['content'], '新对话未自动召回显式记忆')
    run.answer('总结一下', owner='other-user')
    expect('先给两句结论' not in run.messages[-1][0][0]['content'], '召回串账号')


def background_learning(memory):
    memory.grant_devices(OWNER, [PRIMARY])
    title = '远端 PPP 策略路由'
    lesson = {'kind': 'troubleshooting_lesson', 'title': title,
              'content': '远端 PPP 应检查策略表和规则', 'scope': '远端'}

    def extract(messages):
        expect('table 206' in messages[-1]['content'], '后台排障经验缺少工具证据')
        correction = {'kind': 'correction', 'title': '管理可达与业务可达',
                      'content': '管理链路可达不能证明业务可达', 'scope': '远端'}
        return model_reply(json.dumps({'items': [lesson, correction]}, ensure_ascii=False))

    updated = dict(lesson, title='  远端   ppp 策略路由  ',
                   content='远端 PPP 应核对源地址和策略表')

    def update(messages):
        expect('不对' in messages[-1]['content'], '单纯用户纠错未传入后台提取')
        return model_reply(json.dumps({'items': [updated]}, ensure_ascii=False))

    run = Replay(memory, [
        model_reply(calls=[probe_call(PRIMARY, 'policy_routes')]),
        model_reply('应核对策略表。'), extract,
        model_reply('收到，补充检查选源。'), update,
        model_reply('根据此前经验核对选源。'),
    ], {(PRIMARY, 'policy_routes'): 'table 206: default dev ppp6'})
    run.enable_background()
    run.answer('远端 PPP 策略路由怎么定位？')
    run.wait_background()
    rows = {row['kind']: row for row in memory.list_memories(OWNER)}
    expect(set(rows) == {'correction', 'troubleshooting_lesson'}, '后台经验或纠错缺失')
    run.answer('不对，还要核对源地址和策略表')
    run.wait_background()
    rows_after = memory.list_memories(OWNER)
    lessons = [row for row in rows_after if row['kind'] == 'troubleshooting_lesson']
    expect(len(rows_after) == 2 and len(lessons) == 1 and
           lessons[0]['id'] == rows['troubleshooting_lesson']['id'] and
           lessons[0]['content'] == updated['content'], '同类同标题记忆没有更新去重')
    run.agent.background_learning = False
    run.answer('远端 PPP 策略路由怎么检查？', conversation='new-learning-conversation')
    prompt = run.messages[-1][0][0]['content']
    expect(updated['content'] in prompt and rows['correction']['content'] in prompt,
           '新对话未召回更新后的经验与纠错')


def react_fallback(memory):
    class OfflineUpstream:
        rejected_tools = False
        seen_json = False

        def open(self, request, timeout):
            payload = json.loads(request.data.decode('utf-8'))
            if payload.get('tools'):
                self.rejected_tools = True
                raise urllib.error.HTTPError(request.full_url, 400, 'tools unsupported', {}, None)
            if not self.seen_json:
                self.seen_json = True
                content = '{"tool_calls":[{"name":"list_devices","arguments":{}}]}'
            else:
                result = payload['messages'][-1]['content']
                expect('工具返回结果' in result and BACKUP in result, 'ReAct 回包缺少登记设备')
                content = '已取得设备目录。'
            envelope = {'choices': [{'message': {'content': content}}], 'usage': {'total_tokens': 10}}
            return io.BytesIO(json.dumps(envelope, ensure_ascii=False).encode('utf-8'))

    upstream = OfflineUpstream()

    def client(config, messages, **kwargs):
        return agent_v2.default_llm_client(config, messages, opener=upstream, **kwargs)

    result = Replay(memory, client=client).answer('有哪些实验室设备可以查？')
    expect(upstream.rejected_tools and upstream.seen_json
           and [step['tool'] for step in result['agent_steps']] == ['list_devices'],
           'HTTP 400 后未通过 ReAct JSON 完成目录工具调用')


def probe_failure(memory):
    memory.grant_devices(OWNER, [PRIMARY, BACKUP])

    def reroute(messages):
        result = tool_results(messages)[-1]
        expect(not result['ok'] and result['error'] == 'probe_transport_error', '超时被误报为成功')
        return model_reply(calls=[probe_call(BACKUP, 'services')])

    def synthesize(messages):
        expect(tool_results(messages)[-1]['output'] == 'croc active', '缺少备机的替代观测')
        return model_reply('主机检查超时，备机 Croc 在运行，主机状态待核实。')

    run = Replay(memory, [model_reply(calls=[probe_call(PRIMARY, 'services')]), reroute, synthesize],
                 {(PRIMARY, 'services'): TimeoutError(), (BACKUP, 'services'): 'croc active'})
    result = run.answer('两台远端设备的 Croc 服务怎么样？')
    expect([step['ok'] for step in result['agent_steps']] == [False, True], '失败和成功证据未同时保留')
    expect('主机状态待核实' in result['answer'], '回放结果没有交代缺失证据')


def unknown_tool(memory):
    def recover(messages):
        expect(tool_results(messages)[-1]['error'] == 'unknown_tool', '未知工具未返回可恢复错误')
        return model_reply(calls=[tool('list_devices')])

    run = Replay(memory, [model_reply(calls=[tool('invented_tool')]), recover,
                          model_reply('已取得支持范围。')])
    result = run.answer('有哪些只读检查可用？')
    expect([step['ok'] for step in result['agent_steps']] == [False, True], '未知工具后未恢复')


def step_limit(memory):
    def conclude(messages):
        evidence = json.loads(messages[-1]['content'].split('\n', 1)[1])
        expect(any('"devices"' in item for item in evidence['本轮证据摘录'])
               and all(message['role'] != 'tool' for message in messages)
               and '还待核实' in messages[-1]['content'],
               '收尾总结没有带上当前证据或待核实项')
        return model_reply('已查到设备目录；服务和链路状态待核实。')

    run = Replay(memory, [model_reply(calls=[tool('list_devices')]), model_reply(), conclude],
                 max_steps=2)
    result = run.answer('继续核对所有可检查设备')
    expect(len(result['agent_steps']) == 1 and [native for _, native in run.messages] == [True, False, False],
           '达到步数预算后未关闭工具或重复执行了工具')
    expect('服务和链路状态待核实' in result['answer'], '收尾丢失待核实项')


def empty_summary(memory):
    run = Replay(memory, [model_reply(calls=[tool('list_devices')]), model_reply(), model_reply()],
                 max_steps=2)
    result = run.chat('继续核对所有可检查设备')
    expect(result['status'] == 'incomplete' and result['reason'] == 'model_empty_answer'
           and '未能生成完整总结' in result['answer'] and len(result['agent_steps']) == 1,
           '收尾为空时缺少可读说明或丢失已执行步骤')


def kb_check(question, target=None):
    def check(memory):
        hits = qa.retrieve(question)
        ids = [hit['case']['id'] for hit in hits[:5]]
        if target:
            expect(target in ids, '前五条缺少 {}，实际 {}'.format(target, ids))
        else:
            expect(not hits, '无关问题误召回 {}'.format(ids))
    return check


def layout_check(memory):
    good = ('> **结论**：备机已取得 VIP。\n\n### 状态对比\n\n'
            '| 设备 | VIP | 备注 |\n| --- | :---: | --- |\n'
            '| 备机 | 有 | 有效\\|活动 |\n| 主机 | 无 | 待查 |\n\n'
            '```text\n| 代码中的竖线不属于表格 |\n```')
    expect(not answer_layout_issues(good, comparison=True), '规范表格被误判')
    bad = '请看 OBS-1 和 KB-001。\n\n| 设备 | VIP | 状态 |\n| --- | --- |\n| 备机 | 有 |'
    issues = answer_layout_issues(bad, comparison=True)
    for key in ('结论', '内部编号', '小标题', '列数'):
        expect(any(key in issue for issue in issues), '未检测到问题：' + key)


CASES = [
    ('主备排障与综合推理', diagnostic_chain),
    ('首次确认、续跑、直通、新设备确认和撤销', device_consent),
    ('改配置及重启仅生成建议卡', writes_are_proposals),
    ('多轮追问保留结论与工具证据', context_continuity),
    ('长对话触发滚动摘要并带入追问', rolling_summary),
    ('显式学习、跨对话召回和账号隔离', explicit_learning),
    ('后台提取纠错与经验、更新去重和召回', background_learning),
    ('模拟 HTTP 400 后降级为 ReAct JSON', react_fallback),
    ('设备探针超时后转向备机', probe_failure),
    ('未知工具名后继续使用有效工具', unknown_tool),
    ('耗尽步数后无工具收尾', step_limit),
    ('模型收尾为空时保留步骤并解释', empty_summary),
    ('真实知识库：双机热备', kb_check('这套双机热备的业务路径和主备分工是什么？', 'KB-001')),
    ('真实知识库：Croc 内外层', kb_check('Croc 外层传输和 tun16 内层有什么区别？', 'KB-002')),
    ('真实知识库：VIP', kb_check('远端备机没有业务 VIP，为什么 Croc 没启动？', 'KB-004')),
    ('真实知识库：Keepalived', kb_check('局端 Keepalived 怎么选主和切换？', 'KB-013')),
    ('真实知识库：策略路由', kb_check('远端 PPP 探测为什么要用指定源地址和策略路由？', 'KB-007')),
    ('真实知识库：无关问题不误召回', kb_check('企鹅为什么不会飞？')),
    ('回答排版正反例', layout_check),
]


def main():
    global agent_v2, qa
    results = []
    ranking_note = ''
    with contextlib.ExitStack() as guards:
        guards.enter_context(patch.object(socket.socket, 'connect', forbidden))
        guards.enter_context(patch.object(socket.socket, 'connect_ex', forbidden))
        guards.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
        guards.enter_context(patch.object(subprocess, 'Popen', forbidden))
        guards.enter_context(patch.object(urllib.request, 'urlopen', forbidden))
        import agent.agent_v2 as agent_v2
        from agent.agent_memory import AgentMemoryStore
        import retrieval.qa as qa
        guards.enter_context(patch.object(qa, 'model_config', forbidden))

        for name, scenario in CASES:
            started = time.monotonic()
            failure = ''
            try:
                with tempfile.TemporaryDirectory(prefix='ata-v2-eval-') as temp:
                    scenario(AgentMemoryStore(Path(temp) / 'memory.sqlite3'))
            except Exception:
                failure = traceback.format_exc()
            results.append((name, time.monotonic() - started, failure))

        # 开放问法用于观察当前排序，暂不作为回归通过门槛。
        query = '远端主备如何迁移业务 VIP？'
        ids = [hit['case']['id'] for hit in qa.retrieve(query)[:5]]
        if 'KB-004' not in ids:
            ranking_note = '排序提示：{} 前五条缺少远端主备机制章节，实际 {}'.format(query, ', '.join(ids))

    print('Agent v2 离线评测（模拟模型与探针、真实检索和临时 SQLite）')
    print('| # | 场景 | 结果 | 耗时 ms |')
    print('| --- | --- | --- | ---: |')
    for index, (name, elapsed, failure) in enumerate(results, 1):
        print('| {} | {} | {} | {:.0f} |'.format(index, name, 'FAIL' if failure else 'PASS', elapsed * 1000))
    failures = [(name, failure) for name, _, failure in results if failure]
    print('\n通过 {} / {}，失败 {}。'.format(len(results) - len(failures), len(results), len(failures)))
    if ranking_note:
        print(ranking_note)
    for name, failure in failures:
        print('\n{}\n{}'.format(name, failure.rstrip()))
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
