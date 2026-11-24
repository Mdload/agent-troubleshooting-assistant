"""自主推理与多步工具调用 Agent 引擎（v2，纯标准库，兼容 Python 3.6+）。

核心设计：
- 把思考、规划、追查和总结的主动权交还给大模型；
- 首次访问某台设备做只读查询时征求用户确认，同意后本账号后续只读查询直通；
- 任何改配置/重启/修复类操作绝不自动执行，只生成变更建议卡由用户人工确认；
- 保留完整多轮对话与工具证据上下文，超长自动滚动摘要；
- 排障复盘与用户纠错自动沉淀长期记忆并在后续对话召回。
"""
import datetime
import hashlib
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from chat_model import (
    APPROVED_BASE_URLS,
    NoRedirect,
    model_endpoint,
    normalize_base_url,
    rate_limit_kind,
    retry_delay,
)
from device_registry import CHECK_LABELS, DEVICES, PROFILES, allows_check, checks_for, profile
from diagnostic_logs import filter_log_output
from model_availability import CircuitOpen, ModelAvailability
import qa


SYSTEM_PROMPT = """你是实验室智能技术助手与自主排障 Agent，使用自然、清晰、直接的中文交流。

## 工作方式
1. **通用技术与概念问题**：直接清晰回答，无需调用设备工具。
2. **实验室拓扑、机制、历史案例与排障经验**：优先调用 `search_knowledge` 或 `recall_memory` 查证，再结合推理回答。
3. **现场排障与状态核查**：
   - 先按需调用 `list_devices` 确认可查设备和检查项；
   - 自主调用 `inspect_device` 多步取证（可一轮查多项，拿到结果后自己分析异常线索，决定是否继续查对端设备、日志、策略路由、HA配置或定向ping）；
   - 收集到足够证据后，直接综合推理给出清晰结论、关键依据、可能原因和下一步建议。
4. **严格边界**：
   - 区分“历史记忆/知识文档”和“本次实时采集结果”，历史案例是排查罗盘与避坑参考，每次现场细节都可能不同，绝不把旧记录或猜测直接说成当前实时状态；
   - 日志只反映历史事件，不单独证明当前角色或根因；
   - **严禁假装或直接执行任何修改配置、重启服务、修复操作**。若排查后需要改配置或重启，必须调用 `propose_config_change` 输出清晰方案、影响和回滚步骤，等待用户人工确认。
5. **记忆与自学习归纳**：
   - 优先尊重下方【用户记忆与已学经验】中的用户偏好、历史纠错和实验室共享经验，同时主动对照本次现场实况是否有新变化；
   - 当用户明确纠正你、确认排障定位准确/问题解决，或你发现本次现场与历史认知存在新差异时，调用 `save_learning` 归纳成“适用现象 → 有效排查顺序 → 避坑点/前提边界”的通用经验，而不是流水账。

## 回答风格与排版规范（务必让用户一眼抓重点）
- **先给核心结论**：回复开头先用引用块 `> **结论**：...` 用 1~2 句话直接说清结果或核心判断，不要铺垫废话；
- **善用层级与结构**：内容较多时用 `###` 小标题划分块（如 `### 排查结果`、`### 原因分析`、`### 下一步建议`），每个要点开头**加粗关键词**，避免大段密密麻麻的纯文本；
- **多设备/多指标对比用标准 Markdown 表格**：比较 2 台及以上设备、多项状态或方案时，使用规范的 Markdown 表格（列数控制在 3~5 列，单元格简短清晰，每行以 `|` 分隔并带标准分隔行 `| --- | --- |`）；
- **保持清爽易读**：不要在正文里暴露内部编号（如 OBS-1、KB-001）；引用外部资料或文档链接用标准 Markdown 链接；除非用户主动问代码或命令细节，否则用清晰直白的运维和原理语言说明即可。"""


TOOL_SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'search_knowledge',
            'description': '检索实验室拓扑知识库、运维手册、历史故障案例和已发布的经验记录。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '中文或英文技术查询词'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_devices',
            'description': '列出实验室已登记可查询的设备清单、管理地址、静态拓扑角色及每台设备支持的只读检查项。',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'inspect_device',
            'description': '对已登记设备执行一项受控只读检查（如 services、interfaces、routes、policy_routes、logs、ha_config、ping_* 等）。绝不修改配置。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'device': {
                        'type': 'string',
                        'enum': list(DEVICES.keys()),
                        'description': '已登记设备 ID',
                    },
                    'check': {
                        'type': 'string',
                        'description': '该设备支持的只读检查项名称，可先通过 list_devices 查看',
                    },
                },
                'required': ['device', 'check'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'recall_memory',
            'description': '检索当前用户的跨对话长期记忆、历史纠错和排障自学习经验。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '需要回忆的主题或关键词'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'save_learning',
            'description': '将用户的明确偏好、纠错、已核实的拓扑规律或本次排障总结出的可复用经验保存到长期记忆库，供后续对话自动使用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'kind': {
                        'type': 'string',
                        'enum': ['preference', 'correction', 'topology_fact', 'troubleshooting_lesson'],
                        'description': '记忆类型：preference(用户偏好)、correction(用户纠错)、topology_fact(拓扑规律)、troubleshooting_lesson(排障经验)',
                    },
                    'title': {'type': 'string', 'description': '简短标题（20字以内）'},
                    'content': {'type': 'string', 'description': '清晰、可复用的具体内容（如症状→排查路径→结论与前提，或用户明确要求）'},
                    'scope': {'type': 'string', 'description': '适用范围，如 局端/远端/Keepalived/全局'},
                },
                'required': ['kind', 'title', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'propose_config_change',
            'description': '当排查认为需要修改配置、重启服务或执行修复操作时调用。不会自动执行，而是向用户展示变更方案与回滚步骤等待确认。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'device': {'type': 'string', 'description': '目标设备名称或 ID'},
                    'action': {'type': 'string', 'description': '建议执行的操作简述'},
                    'reason': {'type': 'string', 'description': '基于已采集证据的原因说明'},
                    'commands': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': '建议人工确认后执行的命令（可选）',
                    },
                    'rollback': {'type': 'string', 'description': '回滚或恢复方法'},
                },
                'required': ['device', 'action', 'reason'],
            },
        },
    },
]

REACT_FALLBACK_INSTRUCTION = """
如果需要使用工具，请严格只输出以下 JSON（不要加其他文字）：
{"tool_calls":[{"name":"工具名","arguments":{}}]}
可用工具：
- search_knowledge(query)
- list_devices()
- inspect_device(device, check)
- recall_memory(query)
- save_learning(kind, title, content, scope)
- propose_config_change(device, action, reason, commands, rollback)
如果已经可以直接回答用户，直接输出中文回答正文即可（不要包 JSON）。"""

CORRECTION_HINT = re.compile(
    r'不对|错了|记错|答错|说错|理解错|搞错|其实|以后记住|记住|别再|不要再|不用说|不要说|习惯|偏好|纠正|更正|不是这个|差一点'
)
RESOLUTION_HINT = re.compile(
    r'修好了|通了|解决了|恢复了|定位准确|就是这个原因|确实如此|明白了|总结一下|沉淀|记下来|学到了'
)


def _parse_json_object(raw):
    text = (raw or '').strip()
    if not text:
        return None
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def _approved_urls(config):
    approved = set(APPROVED_BASE_URLS)
    extra = config.get('LLM_APPROVED_BASE_URLS')
    if isinstance(extra, (list, tuple, set, frozenset)):
        for url in extra:
            approved.add(normalize_base_url(url))
    return approved


def _endpoints(config):
    if not isinstance(config, dict):
        raise ValueError('Model unavailable')
    approved = _approved_urls(config)
    fallbacks = config.get('LLM_FALLBACKS', [])
    if not isinstance(fallbacks, list):
        fallbacks = []
    result = []
    seen = set()
    for candidate in [config] + fallbacks:
        base_url = model_endpoint(candidate, approved)
        key = hashlib.sha256(json.dumps(
            [base_url, candidate['LLM_MODEL_ID'], candidate['LLM_API_KEY']]
        ).encode('utf-8')).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append((dict(candidate), base_url, key))
    return result


def default_llm_client(config, messages, tools=None, max_tokens=4096, timeout=90.0,
                       availability=None, opener=None):
    """调用 OpenAI 兼容 /chat/completions；支持原生 tools、备用端点切换与熔断隔离。"""
    endpoints = _endpoints(config)
    if opener is None:
        opener = urllib.request.build_opener(NoRedirect())
    circuits = [availability]
    if len(endpoints) > 1:
        if availability is None:
            availability = ModelAvailability()
            circuits = [availability]
        circuits.extend(availability.fallback_circuits([ep[2] for ep in endpoints[1:]]))

    last_error = None
    for index, ((cfg, base_url, key), circuit) in enumerate(zip(endpoints, circuits)):
        payload = {
            'model': cfg['LLM_MODEL_ID'],
            'stream': False,
            'max_tokens': max_tokens,
            'messages': messages,
        }
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        # 显式尊重 LLM_DISABLE_THINKING 配置；默认不强制关闭模型思考
        if cfg.get('LLM_DISABLE_THINKING') is True:
            payload['thinking'] = {'type': 'disabled'}

        req = urllib.request.Request(
            base_url + '/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + cfg['LLM_API_KEY'],
            },
        )
        permit = None
        outage = False
        delay = None
        limit_kind = None
        try:
            if circuit is not None:
                permit = circuit.reserve(key)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(2 * 1024 * 1024)
            data = json.loads(raw.decode('utf-8'))
            choice = (data.get('choices') or [{}])[0]
            msg = choice.get('message') or {}
            usage_obj = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            total_tokens = usage_obj.get('total_tokens')
            if type(total_tokens) is not int or total_tokens < 0:
                total_tokens = None
            tool_calls = []
            for idx, tc in enumerate(msg.get('tool_calls') or []):
                fn = tc.get('function') if isinstance(tc, dict) else None
                if not isinstance(fn, dict) or not isinstance(fn.get('name'), str):
                    continue
                raw_args = fn.get('arguments', '{}')
                if isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    try:
                        parsed_args = json.loads(raw_args or '{}')
                        if not isinstance(parsed_args, dict):
                            parsed_args = {}
                    except ValueError:
                        parsed_args = {}
                tool_calls.append({
                    'id': tc.get('id') or ('call_' + str(idx + 1)),
                    'name': fn['name'],
                    'arguments': parsed_args,
                })
            return {
                'content': msg.get('content') or '',
                'tool_calls': tool_calls,
                'usage': total_tokens,
                'finish_reason': choice.get('finish_reason'),
            }
        except CircuitOpen as error:
            last_error = RuntimeError('model_circuit_open')
            if error.limit_kind == 'quota' or index == len(endpoints) - 1:
                raise last_error from None
        except urllib.error.HTTPError as error:
            outage = error.code == 429 or 500 <= error.code <= 599
            if error.code == 429:
                delay = retry_delay(error.headers, fallback=circuit.cooldown if circuit else 30.0)
                limit_kind = rate_limit_kind(error)
            status_code = error.code
            if error.fp is not None:
                try:
                    error.close()
                except OSError:
                    pass
            # 若上游返回 400/422 且带了 tools，向上抛出特定信号以便自动降级到 ReAct JSON
            if tools and status_code in (400, 422):
                raise ToolsUnsupportedError('tools_unsupported') from None
            last_error = RuntimeError('model_rate_limited' if status_code == 429 else 'model_http_error')
            can_retry = (500 <= status_code <= 599) or (status_code == 429 and limit_kind != 'quota')
            if not can_retry or index == len(endpoints) - 1:
                raise last_error from None
        except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError, ssl.SSLError) as error:
            outage = True
            last_error = RuntimeError('model_timeout' if isinstance(error, (socket.timeout, TimeoutError)) else 'model_connection_failed')
            if index == len(endpoints) - 1:
                raise last_error from None
        finally:
            if permit is not None:
                circuit.finish(permit, failed=outage, retry_after=delay, limit_kind=limit_kind)
    raise last_error or RuntimeError('model_unavailable')


class ToolsUnsupportedError(RuntimeError):
    pass


def _parse_fallback_tool_calls(text):
    stripped = (text or '').strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', stripped, re.S | re.I)
    if fence:
        stripped = fence.group(1).strip()
    if not stripped.startswith('{'):
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get('tool_calls'), list):
        return None
    calls = []
    for idx, item in enumerate(obj['tool_calls']):
        if isinstance(item, dict) and isinstance(item.get('name'), str):
            args = item.get('arguments') if isinstance(item.get('arguments'), dict) else {}
            calls.append({'id': 'fb_' + str(idx + 1), 'name': item['name'], 'arguments': args})
    return calls or None


def _summarize_tool_output(name, args, result):
    if name == 'inspect_device':
        device = args.get('device', '')
        check = args.get('check', '')
        label = DEVICES.get(device, {}).get('label', device)
        check_label = CHECK_LABELS.get(check, check)
        if result.get('ok'):
            snippet = (result.get('output') or '').strip().replace('\n', ' | ')
            if len(snippet) > 220:
                snippet = snippet[:220] + '...'
            return '{} / {}（成功，{}）：{}'.format(label, check_label, result.get('collected_at', '')[:19], snippet)
        return '{} / {}（未成功：{}）'.format(label, check_label, result.get('error', 'unknown'))
    if name == 'search_knowledge':
        titles = [item.get('title', '') for item in (result.get('hits') or [])[:3]]
        return '检索知识「{}」：命中 {} 条（{}）'.format(
            args.get('query', '')[:40], len(result.get('hits') or []), '、'.join(titles) or '无'
        )
    if name == 'list_devices':
        return '查看实验室设备与只读检查目录'
    if name == 'recall_memory':
        return '回忆历史记忆「{}」：命中 {} 条'.format(args.get('query', '')[:40], len(result.get('memories') or []))
    if name == 'save_learning':
        return '保存记忆/经验：{}'.format(args.get('title', ''))
    if name == 'propose_config_change':
        return '提出配置变更建议（未执行）：{} - {}'.format(args.get('device', ''), args.get('action', ''))
    return name


LOG_KEY_EVENT = re.compile(
    r'master|backup|fault|error|warn|fail|vrrp|vip|down\b|up\b|stop|start|restart|'
    r'timeout|refused|kill|switch|transition|owner-fault|watchdog|no route|unreachable|carrier',
    re.I,
)
INTERNAL_REF_RE = re.compile(r'\s*[（(]?\b(?:OBS|KB|HA)-\d+\b[）)]?')
TABLE_DIVIDER_LINE_RE = re.compile(r'^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$', re.M)


def _excerpt_log_output(output, limit=6000):
    """长日志优先保留最新末尾，并从较早日志中提取关键故障/切换事件行。"""
    if not isinstance(output, str) or len(output) <= limit:
        return output
    lines = output.splitlines()
    if len(lines) <= 2:
        return '[日志较长，以下为最新末尾]\n' + output[-(limit - 40):]
    tail_budget = int(limit * 0.72)
    tail_lines = []
    tail_len = 0
    tail_start = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        add_len = len(line) + 1
        if tail_lines and tail_len + add_len > tail_budget:
            break
        tail_lines.append(line)
        tail_len += add_len
        tail_start = idx
    tail_lines.reverse()

    event_budget = max(200, limit - tail_len - 96)
    event_lines = []
    event_len = 0
    for line in reversed(lines[:tail_start]):
        if LOG_KEY_EVENT.search(line):
            add_len = len(line) + 1
            if event_lines and event_len + add_len > event_budget:
                break
            event_lines.append(line)
            event_len += add_len
    event_lines.reverse()

    parts = []
    if event_lines:
        parts.append('[日志较长，以下为较早关键事件摘录]\n' + '\n'.join(event_lines))
    parts.append('[以下为最新日志末尾]\n' + '\n'.join(tail_lines))
    return '\n\n'.join(parts)


def _strip_internal_refs(text):
    """清除正文里偶发泄露的内部编号（不影响代码块内容）。"""
    out = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r'^\s*(`{3,}|~{3,})', line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        cleaned = INTERNAL_REF_RE.sub('', line)
        cleaned = re.sub(r'[（(]\s*[）)]', '', cleaned)
        out.append(cleaned)
    return '\n'.join(out).strip()


def _polish_answer(answer, steps):
    """轻量规整回答：清理内部编号，多设备排查缺对比表时补充纯事实状态速览表。"""
    text = _strip_internal_refs(answer or '')
    if not text:
        return text
    dev_steps = [s for s in (steps or []) if s.get('tool') == 'inspect_device' and s.get('device')]
    distinct_devices = {s['device'] for s in dev_steps}
    if len(distinct_devices) >= 2 and not TABLE_DIVIDER_LINE_RE.search(text):
        rows = [
            '',
            '### 实查状态速览',
            '| 设备 | 检查项 | 状态 | 关键摘要 |',
            '| --- | --- | --- | --- |',
        ]
        for s in dev_steps[:12]:
            dev_label = (s.get('device_label') or s.get('device') or '').replace('|', '/')
            chk_label = (s.get('check_label') or s.get('check') or '').replace('|', '/')
            ok_txt = '成功' if s.get('ok') is not False else '未成功'
            brief = (s.get('summary') or '').replace('|', '/').replace('\n', ' ').strip()
            prefix = '{} {}：'.format(dev_label, chk_label)
            if brief.startswith(prefix):
                brief = brief[len(prefix):].strip()
            if len(brief) > 48:
                brief = brief[:47] + '…'
            rows.append('| {} | {} | {} | {} |'.format(dev_label, chk_label, ok_txt, brief or '已采集'))
        text = text.rstrip() + '\n' + '\n'.join(rows)
    return text


def _step_title(name, args):
    if name == 'inspect_device':
        dev = args.get('device', '')
        chk = args.get('check', '')
        return '{} · {}'.format(DEVICES.get(dev, {}).get('label', dev), CHECK_LABELS.get(chk, chk))
    return {
        'search_knowledge': '检索知识库与历史经验',
        'list_devices': '查看可查设备目录',
        'recall_memory': '回忆长期记忆',
        'save_learning': '沉淀新记忆/经验',
        'propose_config_change': '生成配置变更建议（未执行）',
    }.get(name, name)


class AutonomousAgent:
    def __init__(self, memory_store, probe_fn=None, config_fn=None, experiences_fn=None,
                 llm_client=None, availability=None, max_steps=8, background_learning=True):
        self.memory = memory_store
        self.probe_fn = probe_fn
        self.config_fn = config_fn or qa.model_config
        self.experiences_fn = experiences_fn
        self.llm_client = llm_client or default_llm_client
        self.availability = availability or ModelAvailability()
        self.max_steps = max_steps
        self.background_learning = background_learning
        self._progress_lock = threading.Lock()
        self._live_progress = {}

    def _set_progress(self, owner, conversation_id, run_id, stage, text, steps_snapshot=None):
        key = (owner, conversation_id)
        now = time.monotonic()
        with self._progress_lock:
            existing = self._live_progress.get(key)
            started_at = existing['started_at'] if existing and existing.get('run_id') == run_id else now
            self._live_progress[key] = {
                'run_id': run_id,
                'active': True,
                'stage': stage,
                'text': text,
                'started_at': started_at,
                'updated_at': now,
                'steps': list(steps_snapshot) if steps_snapshot is not None else (
                    list(existing.get('steps', [])) if existing and existing.get('run_id') == run_id else []
                ),
            }

    def _clear_progress(self, owner, conversation_id, run_id):
        key = (owner, conversation_id)
        with self._progress_lock:
            existing = self._live_progress.get(key)
            if existing and existing.get('run_id') == run_id:
                self._live_progress.pop(key, None)

    def get_progress(self, owner, conversation_id):
        key = (owner, conversation_id)
        now = time.monotonic()
        with self._progress_lock:
            entry = self._live_progress.get(key)
            if not entry or (now - entry.get('updated_at', now)) > 300:
                return {'active': False, 'stage': 'idle', 'text': '', 'elapsed_seconds': 0, 'steps': []}
            return {
                'active': bool(entry.get('active')),
                'stage': entry.get('stage', 'working'),
                'text': entry.get('text', ''),
                'elapsed_seconds': round(max(0.0, now - entry.get('started_at', now)), 1),
                'steps': list(entry.get('steps', [])),
            }

    def _call_llm(self, messages, use_tools=True, max_tokens=4096, timeout=90.0):
        cfg = self.config_fn()
        if not cfg or not all(cfg.get(k) for k in ('LLM_API_KEY', 'LLM_MODEL_ID', 'LLM_BASE_URL')):
            raise RuntimeError('model_not_configured')
        if use_tools:
            try:
                return self.llm_client(
                    cfg, messages, tools=TOOL_SCHEMAS, max_tokens=max_tokens,
                    timeout=timeout, availability=self.availability
                ), True
            except ToolsUnsupportedError:
                pass
        # 自动降级为简洁 ReAct JSON 协议
        fb_messages = list(messages)
        if fb_messages and fb_messages[0].get('role') == 'system':
            fb_messages[0] = dict(
                fb_messages[0],
                content=fb_messages[0]['content'] + '\n' + REACT_FALLBACK_INSTRUCTION
            )
        resp = self.llm_client(
            cfg, fb_messages, tools=None, max_tokens=max_tokens,
            timeout=timeout, availability=self.availability
        )
        if use_tools and not resp.get('tool_calls'):
            parsed = _parse_fallback_tool_calls(resp.get('content', ''))
            if parsed:
                resp = dict(resp, content='', tool_calls=parsed)
        return resp, False

    def _build_initial_messages(self, owner, conversation_id, question, client_history=None):
        now_str = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        granted = self.memory.granted_devices(owner)
        granted_text = '、'.join(
            '{}({})'.format(DEVICES[d]['label'], d) for d in granted
        ) if granted else '暂无（首次访问任何设备前会自动向用户请求一次只读确认）'

        sys_parts = [SYSTEM_PROMPT, '\n当前时间：{}。已授权只读直通设备：{}。'.format(now_str, granted_text)]

        # 注入自动召回的长期记忆与实验室共享经验
        recalled = self.memory.recall_memories(owner, question, limit=6, include_shared=True, record_hits=True)
        if recalled:
            kind_labels = {
                'preference': '用户偏好',
                'correction': '历史纠错',
                'topology_fact': '拓扑规律',
                'troubleshooting_lesson': '排障经验',
            }
            lines = ['\n## 用户记忆与实验室已学经验（排查参考，须结合本次现场实况验证差异）']
            for item in recalled:
                scope_txt = '（{}）'.format(item['scope']) if item.get('scope') else ''
                shared_txt = '·实验室共享' if item.get('shared') else ''
                lines.append('- [{}{}]{} {}：{}'.format(
                    kind_labels.get(item['kind'], item['kind']), shared_txt, scope_txt, item['title'], item['content']
                ))
            sys_parts.append('\n'.join(lines))

        # 注入本对话的滚动进展摘要
        summary_row = self.memory.get_summary(owner, conversation_id)
        if summary_row and summary_row.get('summary'):
            sys_parts.append('\n## 本对话较早进展摘要\n' + summary_row['summary'])

        messages = [{'role': 'system', 'content': '\n'.join(sys_parts)}]

        # 优先使用后端持久化的完整对话轨迹（含工具结果摘要与完整助手回答）
        turns = self.memory.recent_turns(owner, conversation_id, limit=8)
        if turns:
            for turn in turns:
                messages.append({'role': 'user', 'content': turn['user_text']})
                assistant_block = turn['assistant_text']
                if turn.get('tool_summary'):
                    assistant_block = '[本轮已执行工具与结果]\n' + turn['tool_summary'] + '\n\n' + assistant_block
                messages.append({'role': 'assistant', 'content': assistant_block})
        elif isinstance(client_history, list):
            # 兼容首次升级后尚未入后端轨迹的旧聊天窗口
            for msg in client_history[-8:]:
                if isinstance(msg, dict) and msg.get('role') in ('user', 'assistant') and isinstance(msg.get('content'), str):
                    messages.append({'role': msg['role'], 'content': msg['content'][:4000]})

        messages.append({'role': 'user', 'content': question})
        return messages

    def _run_search_knowledge(self, query):
        extra = []
        if self.experiences_fn is not None:
            try:
                store = self.experiences_fn() if callable(self.experiences_fn) else self.experiences_fn
                extra = store.records(query)
            except Exception:
                extra = []
        hits = qa.retrieve(query, extra_records=extra)[:5]
        items = []
        for hit in hits:
            case = hit['case']
            body = '\n'.join(filter(None, (
                case.get('symptom', ''),
                case.get('historical_finding', ''),
                case.get('historical_outcome', ''),
                case.get('verification_limits', ''),
            )))
            items.append({
                'id': case.get('id'),
                'title': case.get('title'),
                'kind': case.get('kind', 'knowledge'),
                'operating_modes': case.get('operating_modes', []),
                'device_groups': case.get('device_groups', []),
                'evidence_date': case.get('evidence_date'),
                'content': body[:1800],
            })
        return {'query': query, 'count': len(items), 'hits': items}

    def _run_list_devices(self, owner):
        granted = set(self.memory.granted_devices(owner))
        rows = []
        for dev_id, info in DEVICES.items():
            prof = profile(dev_id)
            rows.append({
                'id': dev_id,
                'label': info['label'],
                'address': info['address'],
                'group': prof['group'],
                'business_role': prof['business_role'],
                'expected_vips': dict(prof['ha_vips']),
                'read_only_granted': dev_id in granted,
                'checks': {c: CHECK_LABELS.get(c, c) for c in checks_for(dev_id)},
            })
        return {'devices': rows, 'note': '登记名中的主机/备机不代表当前实时 VIP 归属，需实查接口确认。'}

    def _run_inspect_device(self, device, check):
        if not allows_check(device, check):
            return {'ok': False, 'device': device, 'check': check, 'error': 'unsupported_device_or_check'}
        if self.probe_fn is None:
            return {'ok': False, 'device': device, 'check': check, 'error': 'probe_unavailable'}
        started = time.monotonic()
        try:
            raw = dict(self.probe_fn(device, check))
        except Exception:
            raw = {'ok': False, 'output': '', 'error': 'probe_transport_error'}
        duration_ms = round((time.monotonic() - started) * 1000)
        output = raw.get('output') or ''
        if check == 'logs' and isinstance(output, str) and output:
            output, _ = filter_log_output(output)
        collected_at = raw.get('collected_at') or (datetime.datetime.utcnow().isoformat() + 'Z')
        if check == 'logs':
            output = _excerpt_log_output(output, limit=6000)
        elif len(output) > 6000:
            output = output[:6000] + '\n...[输出已截断]'
        return {
            'ok': bool(raw.get('ok')),
            'device': device,
            'device_label': DEVICES[device]['label'],
            'check': check,
            'check_label': CHECK_LABELS.get(check, check),
            'collected_at': collected_at,
            'duration_ms': raw.get('duration_ms', duration_ms),
            'output': output,
            'error': raw.get('error'),
        }

    def chat(self, owner, conversation_id, question='', client_history=None,
             grant_devices=None, pending_id=None, deny_pending=False):
        started = time.monotonic()
        deadline = started + 220.0
        run_id = secrets.token_hex(8)

        # 1. 若恢复待确认任务，先严格校验待续请求本身，通过后再按需写入首次授权
        if pending_id:
            pending = self.memory.pop_pending(owner, conversation_id, pending_id)
            if not pending:
                return {
                    'status': 'incomplete',
                    'route': 'agent_v2',
                    'answer': '刚才的设备访问确认已过期或已处理，请直接重新发问即可。',
                }
            if deny_pending:
                answer = '好的，已取消进入设备检查，本次未访问任何设备。如需只做原理分析或查看知识库，可以直接告诉我。'
                self.memory.append_turn(owner, conversation_id, pending['question'], answer, '用户暂未授权进入设备')
                return {'status': 'answered', 'route': 'agent_v2', 'answer': answer, 'agent_steps': []}
            requested_devs = {
                tc.get('arguments', {}).get('device')
                for tc in (pending.get('pending_tool_calls') or [])
                if tc.get('name') == 'inspect_device' and isinstance(tc.get('arguments', {}).get('device'), str)
            }
            valid_grants = [d for d in (grant_devices or []) if d in requested_devs]
            if valid_grants:
                self.memory.grant_devices(owner, valid_grants)
            question = pending['question']
            messages = pending['messages']
            steps = pending['steps']
            tool_summaries = pending['tool_summaries']
            proposed_changes = pending['proposed_changes']
            saved_learnings = pending['saved_learnings']
            total_tokens = pending['total_tokens']
            tokens_complete = pending['tokens_complete']
            step_offset = pending['step_index']
            initial_tool_calls = pending['pending_tool_calls']
            native_tools = pending.get('native_tools', True)
        else:
            question = (question or '').strip()
            if not question:
                raise ValueError('Invalid question')
            messages = self._build_initial_messages(owner, conversation_id, question, client_history)
            steps = []
            tool_summaries = []
            proposed_changes = []
            saved_learnings = []
            total_tokens = 0
            tokens_complete = True
            step_offset = 0
            initial_tool_calls = None
            native_tools = True

        progress_steps = [
            {
                'title': _step_title(s.get('tool', ''), s.get('arguments') or {}),
                'ok': s.get('ok', True),
                'duration_ms': s.get('duration_ms'),
                'summary': s.get('summary', ''),
            }
            for s in steps
        ]
        self._set_progress(
            owner, conversation_id, run_id, 'thinking',
            '已获授权，准备开始只读排查…' if pending_id else '正在理解问题并规划下一步…',
            progress_steps,
        )

        try:
            # 2. 自主多步 ReAct / Tool-Calling 主循环
            final_answer = ''
            for step_index in range(step_offset, self.max_steps):
                if time.monotonic() >= deadline:
                    break

                if initial_tool_calls is not None:
                    tool_calls = initial_tool_calls
                    initial_tool_calls = None
                    assistant_content = ''
                else:
                    self._set_progress(
                        owner, conversation_id, run_id, 'thinking',
                        '正在结合已采集证据综合分析…' if steps else '正在思考并规划排查步骤…',
                        progress_steps,
                    )
                    try:
                        resp, native_tools = self._call_llm(
                            messages,
                            use_tools=(step_index < self.max_steps - 1),
                            timeout=min(90.0, max(15.0, deadline - time.monotonic())),
                        )
                    except Exception as error:
                        code = str(error)
                        friendly = {
                            'model_not_configured': '模型尚未配置，请先检查服务端模型配置。',
                            'model_rate_limited': '模型当前限流繁忙，请稍后再试。',
                            'model_circuit_open': '模型暂处故障冷却中，请稍后再试。',
                            'model_timeout': '模型响应超时，请稍后重试。',
                        }.get(code, '模型服务暂时不可用，请稍后重试。')
                        return {
                            'status': 'incomplete',
                            'route': 'agent_v2',
                            'reason': code,
                            'answer': friendly,
                            'agent_steps': steps,
                            'proposed_changes': proposed_changes,
                            'saved_learnings': saved_learnings,
                        }

                    usage = resp.get('usage')
                    if type(usage) is int and usage >= 0:
                        total_tokens += usage
                    else:
                        tokens_complete = False
                    assistant_content = (resp.get('content') or '').strip()
                    tool_calls = resp.get('tool_calls') or []

                    if not tool_calls:
                        final_answer = assistant_content
                        break

                    # 把模型的 tool_calls 消息写入上下文
                    if native_tools:
                        messages.append({
                            'role': 'assistant',
                            'content': assistant_content or None,
                            'tool_calls': [
                                {
                                    'id': tc['id'],
                                    'type': 'function',
                                    'function': {
                                        'name': tc['name'],
                                        'arguments': json.dumps(tc['arguments'], ensure_ascii=False),
                                    },
                                }
                                for tc in tool_calls
                            ],
                        })
                    else:
                        messages.append({
                            'role': 'assistant',
                            'content': json.dumps({'tool_calls': tool_calls}, ensure_ascii=False),
                        })

                # 检查是否包含尚未首次授权只读访问的设备
                granted = set(self.memory.granted_devices(owner))
                needed_devices = []
                planned_by_device = {}
                for tc in tool_calls:
                    if tc['name'] == 'inspect_device':
                        dev = tc['arguments'].get('device')
                        chk = tc['arguments'].get('check')
                        if isinstance(dev, str) and dev in DEVICES and dev not in granted:
                            if dev not in needed_devices:
                                needed_devices.append(dev)
                            if isinstance(chk, str) and allows_check(dev, chk):
                                planned_by_device.setdefault(dev, [])
                                if chk not in planned_by_device[dev]:
                                    planned_by_device[dev].append(chk)

                if needed_devices:
                    # 首次访问该设备：暂停并请用户确认一次，确认后后续只读直通
                    pid = self.memory.save_pending(owner, conversation_id, {
                        'question': question,
                        'messages': messages,
                        'steps': steps,
                        'tool_summaries': tool_summaries,
                        'proposed_changes': proposed_changes,
                        'saved_learnings': saved_learnings,
                        'total_tokens': total_tokens,
                        'tokens_complete': tokens_complete,
                        'step_index': step_index,
                        'pending_tool_calls': tool_calls,
                        'native_tools': native_tools,
                    })
                    device_items = [
                        {
                            'id': d,
                            'label': DEVICES[d]['label'],
                            'address': DEVICES[d]['address'],
                            'checks': [CHECK_LABELS.get(c, c) for c in planned_by_device.get(d, [])],
                        }
                        for d in needed_devices
                    ]
                    labels = '、'.join(item['label'] for item in device_items)
                    return {
                        'status': 'device_permission_required',
                        'route': 'agent_v2',
                        'pending_id': pid,
                        'requested_devices': device_items,
                        'answer': '需要首次进入 {} 做只读排查（不会修改任何配置）。同意后，后续对这些设备的只读查询将自动执行；涉及改配置仍会单独询问你。'.format(labels),
                        'agent_steps': steps,
                        'total_tokens': total_tokens,
                        'total_tokens_complete': tokens_complete,
                    }

                # 顺序执行本轮全部工具调用（兼顾 62→140 单槽桥接，不触发 bridge_busy）
                fallback_results = []
                for tc in tool_calls[:6]:
                    name = tc['name']
                    args = tc.get('arguments') or {}
                    title = _step_title(name, args)
                    self._set_progress(
                        owner, conversation_id, run_id, 'tool',
                        '正在执行：{}…'.format(title),
                        progress_steps,
                    )
                    if name == 'search_knowledge':
                        tool_result = self._run_search_knowledge(str(args.get('query', '')))
                    elif name == 'list_devices':
                        tool_result = self._run_list_devices(owner)
                    elif name == 'inspect_device':
                        tool_result = self._run_inspect_device(args.get('device', ''), args.get('check', ''))
                    elif name == 'recall_memory':
                        recalled = self.memory.recall_memories(owner, str(args.get('query', '')), limit=6)
                        tool_result = {'memories': recalled}
                    elif name == 'save_learning':
                        try:
                            saved = self.memory.save_memory(
                                owner,
                                str(args.get('kind', 'troubleshooting_lesson')),
                                str(args.get('title', '')),
                                str(args.get('content', '')),
                                str(args.get('scope', '')),
                                source_conversation=conversation_id,
                            )
                            saved_learnings.append(saved)
                            tool_result = {'ok': True, 'saved': saved}
                        except ValueError as err:
                            tool_result = {'ok': False, 'error': str(err)}
                    elif name == 'propose_config_change':
                        proposal = {
                            'device': str(args.get('device', '')),
                            'action': str(args.get('action', '')),
                            'reason': str(args.get('reason', '')),
                            'commands': [str(c) for c in (args.get('commands') or []) if c],
                            'rollback': str(args.get('rollback', '')),
                            'executed': False,
                        }
                        proposed_changes.append(proposal)
                        tool_result = {'ok': True, 'status': 'proposed_awaiting_human_confirmation', 'executed': False}
                    else:
                        tool_result = {'ok': False, 'error': 'unknown_tool'}

                    summary_line = _summarize_tool_output(name, args, tool_result)
                    tool_summaries.append(summary_line)
                    preview_output = None
                    if name == 'inspect_device':
                        raw_out = tool_result.get('output', '')
                        preview_output = _excerpt_log_output(raw_out, limit=2000) if args.get('check') == 'logs' else raw_out[:2000]
                    step_record = {
                        'tool': name,
                        'arguments': args,
                        'ok': tool_result.get('ok', True),
                        'summary': summary_line,
                        'device': args.get('device') if name == 'inspect_device' else None,
                        'device_label': DEVICES.get(args.get('device', ''), {}).get('label') if name == 'inspect_device' else None,
                        'check': args.get('check') if name == 'inspect_device' else None,
                        'check_label': CHECK_LABELS.get(args.get('check', ''), args.get('check')) if name == 'inspect_device' else None,
                        'collected_at': tool_result.get('collected_at'),
                        'duration_ms': tool_result.get('duration_ms'),
                        'output': preview_output,
                    }
                    steps.append(step_record)
                    progress_steps.append({
                        'title': title,
                        'ok': step_record['ok'],
                        'duration_ms': step_record['duration_ms'],
                        'summary': summary_line,
                    })
                    self._set_progress(
                        owner, conversation_id, run_id, 'tool_done',
                        '已完成：{}，继续下一步…'.format(title),
                        progress_steps,
                    )

                    serialized = json.dumps(tool_result, ensure_ascii=False)
                    if native_tools:
                        messages.append({
                            'role': 'tool',
                            'tool_call_id': tc['id'],
                            'content': serialized,
                        })
                    else:
                        fallback_results.append({'name': name, 'result': tool_result})

                if not native_tools:
                    messages.append({
                        'role': 'user',
                        'content': '工具返回结果：\n' + json.dumps(fallback_results, ensure_ascii=False) + '\n请继续推理或直接给出中文回答。',
                    })

            if not final_answer:
                self._set_progress(owner, conversation_id, run_id, 'summarizing', '正在汇总检查结果并整理回答…', progress_steps)
                # 步数或时间到达上限时，做一次无工具收尾总结
                try:
                    messages.append({'role': 'user', 'content': '请根据目前已经获取的全部信息，直接总结当前结论、依据和还待核实的部分。'})
                    resp, _ = self._call_llm(messages, use_tools=False, timeout=45.0)
                    final_answer = (resp.get('content') or '').strip()
                    if type(resp.get('usage')) is int and resp['usage'] >= 0:
                        total_tokens += resp['usage']
                except Exception:
                    pass
            if not final_answer:
                final_answer = '本轮已执行部分检查，但未能生成完整总结。你可以继续追问让我接着分析。'
            else:
                final_answer = _polish_answer(final_answer, steps)
        finally:
            self._clear_progress(owner, conversation_id, run_id)

        # 3. 写入完整上下文轨迹（同步完成，确保下一句追问立刻有完整上下文）
        tool_summary_text = '\n'.join(tool_summaries[-12:])
        self.memory.append_turn(owner, conversation_id, question, final_answer, tool_summary_text)

        # 4. 后台异步维护：滚动摘要 + 结构化自学习归纳（不拖慢本次响应）
        used_device_tools = any(s['tool'] == 'inspect_device' for s in steps)
        should_learn = (not saved_learnings) and (
            used_device_tools
            or bool(CORRECTION_HINT.search(question))
            or bool(RESOLUTION_HINT.search(question))
        )
        if self.background_learning:
            worker = threading.Thread(
                target=self._post_turn_maintenance,
                args=(owner, conversation_id, question, final_answer, tool_summary_text, should_learn),
                daemon=True,
            )
            worker.start()

        return {
            'status': 'answered',
            'route': 'agent_v2',
            'answer': final_answer,
            'agent_steps': steps,
            'proposed_changes': proposed_changes,
            'saved_learnings': saved_learnings,
            'granted_devices': self.memory.granted_devices(owner),
            'total_tokens': total_tokens,
            'total_tokens_complete': tokens_complete,
            'ui_elapsed_seconds': round(time.monotonic() - started, 3),
        }

    def _apply_induced_items(self, owner, conversation_id, items, default_shared=False, confidence_delta=0):
        saved_list = []
        for item in (items or [])[:3]:
            if not isinstance(item, dict):
                continue
            action = str(item.get('action') or 'create').strip().lower()
            if action == 'skip':
                continue
            kind = str(item.get('kind') or 'troubleshooting_lesson').strip()
            title = str(item.get('title') or '').strip()
            content = str(item.get('content') or '').strip()
            scope = str(item.get('scope') or '').strip()
            tags = item.get('tags') or ''
            if isinstance(tags, list):
                tags = ' '.join(str(t).strip() for t in tags if str(t).strip())
            else:
                tags = str(tags).strip()
            target_id = str(item.get('target_id') or '').strip() or None
            if not title or not content:
                continue
            merge_target_id = target_id if action == 'merge' else None
            supersede_id = target_id if action == 'supersede' else None
            saved = self.memory.save_memory(
                owner, kind, title, content, scope,
                source_conversation=conversation_id,
                shared=bool(default_shared and kind != 'preference'),
                tags=tags,
                merge_target_id=merge_target_id,
                supersede_id=supersede_id,
                confidence_delta=confidence_delta,
            )
            saved_list.append(saved)
        return saved_list

    def _existing_memory_context(self, owner, query_text, limit=5):
        existing = self.memory.recall_memories(owner, query_text, limit=limit, include_shared=False)
        if not existing:
            return '暂无相关已有个人记忆。'
        lines = []
        for row in existing:
            lines.append('- id={} | kind={} | title={} | scope={} | content={}'.format(
                row['id'], row['kind'], row['title'], row.get('scope', ''), row['content'][:240]
            ))
        return '\n'.join(lines)

    def learn_from_feedback(self, owner, conversation_id, verdict, question, answer,
                            note='', tool_summary='', share_to_lab=False):
        """闭环反馈学习：根据用户‘定位准确’或‘判断有误/纠正’即时总结归纳并更新记忆。"""
        question = (question or '').strip()[:1000]
        answer = (answer or '').strip()[:2500]
        note = (note or '').strip()[:800]
        tool_summary = (tool_summary or '').strip()[:1500]
        if verdict == 'incorrect' and not note:
            raise ValueError('请填写纠正说明，告诉我哪里判断有误或真实原因是什么')
        if not question and not answer and not note:
            raise ValueError('缺少可学习的对话内容')

        existing_ctx = self._existing_memory_context(owner, question + ' ' + note + ' ' + answer[:300], limit=5)
        if verdict == 'incorrect':
            sys_prompt = (
                '你正在做排障反思与纠错学习。用户指出助手刚才的回答有误并给出了真实纠正。'
                '请对比助手原判断与用户纠正，提炼一条高价值【防重犯规则】：明确写清“原误判点 → 真实规律/现场差异 → 下次优先核查项”。'
                '同时对照【已有个人记忆】：若与某条旧记忆矛盾，设 action="supersede" 并填 target_id；若是补充则设 action="merge"；否则设 action="create"。'
                '严格只返回 JSON：{"items":[{"action":"create|merge|supersede","target_id":"","kind":"correction","title":"简短标题(20字内)","content":"具体防重犯规则与适用前提","scope":"适用范围","tags":"关键词1 关键词2"}]}。'
            )
            user_prompt = (
                '【已有相关记忆】\n' + existing_ctx
                + '\n\n【用户原问题】\n' + (question or '（未提供）')
                + '\n\n【工具记录】\n' + (tool_summary or '无')
                + '\n\n【助手原回答】\n' + (answer or '（未提供）')
                + '\n\n【用户纠正说明】\n' + note
            )
        else:
            sys_prompt = (
                '你正在做排障成功复盘归纳。用户确认助手刚才的定位与分析准确。'
                '请把本次问答与工具排查过程提炼成一条可复用的通用排障经验（不要写成一次性流水账），包含：适用现象、有效排查顺序、关键结论与现场差异提醒。'
                '同时对照【已有个人记忆】：若同类经验已存在则设 action="merge" 并填 target_id 融合升级；否则设 action="create"。'
                '严格只返回 JSON：{"items":[{"action":"create|merge","target_id":"","kind":"troubleshooting_lesson","title":"简短标题(20字内)","content":"适用现象/排查思路/结论与边界","scope":"适用范围","tags":"关键词1 关键词2"}]}。'
            )
            user_prompt = (
                '【已有相关记忆】\n' + existing_ctx
                + '\n\n【用户问题】\n' + (question or '（未提供）')
                + '\n\n【工具记录】\n' + (tool_summary or '无')
                + '\n\n【已确认准确的回答】\n' + (answer or '（未提供）')
                + ('\n\n【用户补充说明】\n' + note if note else '')
            )

        saved_list = []
        try:
            resp, _ = self._call_llm(
                [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': user_prompt}],
                use_tools=False, max_tokens=700, timeout=30.0,
            )
            parsed = _parse_json_object(resp.get('content', ''))
            if parsed and isinstance(parsed.get('items'), list):
                saved_list = self._apply_induced_items(
                    owner, conversation_id, parsed['items'][:1],
                    default_shared=share_to_lab, confidence_delta=1,
                )
        except Exception:
            saved_list = []

        if not saved_list:
            # 兜底：即使模型繁忙，也能直接沉淀结构化记录不丢失用户反馈
            if verdict == 'incorrect':
                fallback_title = ('纠错：' + (question or note))[:28]
                fallback_content = '用户纠正：{}（原问题：{}；下次遇到同类情况优先按此核查，不沿用旧假设）'.format(
                    note, question[:120] or '现场排障'
                )
                saved = self.memory.save_memory(
                    owner, 'correction', fallback_title, fallback_content,
                    scope='纠错反思', source_conversation=conversation_id,
                    shared=share_to_lab, tags=question[:60], confidence_delta=1,
                )
            else:
                first_line = next((ln.strip(' >*#-') for ln in answer.splitlines() if ln.strip()), answer[:160])
                fallback_title = ('经验：' + (question or first_line))[:28]
                parts = ['适用场景：' + (question[:140] or '现场排障'), '核心结论：' + first_line[:300]]
                if tool_summary:
                    parts.append('有效检查：' + tool_summary[:260].replace('\n', '；'))
                parts.append('注：属于历史复盘经验，再次遇到时须结合现场实时状态核实细节差异。')
                saved = self.memory.save_memory(
                    owner, 'troubleshooting_lesson', fallback_title, '\n'.join(parts),
                    scope='已验证排障', source_conversation=conversation_id,
                    shared=share_to_lab, tags=question[:60], confidence_delta=1,
                )
            saved_list = [saved]
        return saved_list[0]

    def consolidate_conversation(self, owner, conversation_id):
        """一键总结整段对话，归纳 1~3 条结构化可复用经验并自动合并/替代旧记忆。"""
        turns = self.memory.recent_turns(owner, conversation_id, limit=16)
        if not turns:
            raise ValueError('当前对话还没有可总结的排障记录')
        transcript = []
        for idx, turn in enumerate(turns, 1):
            transcript.append('第{}轮 用户：{}'.format(idx, turn['user_text'][:350]))
            if turn.get('tool_summary'):
                transcript.append('第{}轮 工具：{}'.format(idx, turn['tool_summary'][:350]))
            transcript.append('第{}轮 助手：{}'.format(idx, turn['assistant_text'][:450]))
        joined = '\n'.join(transcript)
        existing_ctx = self._existing_memory_context(owner, joined[:500], limit=6)
        sys_prompt = (
            '请对整段实验室技术/排障对话做深度复盘归纳，提炼 1~3 条真正值得长期保留的通用规律、排障方法论或纠错避坑规则。'
            '要求：1) 抽离出可跨次复用的“现象 → 排查思路 → 根因/避坑点 → 适用边界”，提醒现场细节可能变化；'
            '2) 忽略纯闲聊或一次性瞬时读数；'
            '3) 对照【已有相关记忆】，同类则 action="merge" 填 target_id，推翻旧结论则 action="supersede" 填 target_id，全新则 action="create"。'
            '严格只返回 JSON：{"items":[{"action":"create|merge|supersede","target_id":"","kind":"troubleshooting_lesson|topology_fact|correction|preference","title":"简短标题","content":"结构化经验正文","scope":"适用范围","tags":"关键词"}]}。'
        )
        resp, _ = self._call_llm(
            [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': '【已有相关记忆】\n' + existing_ctx + '\n\n【完整对话轨迹】\n' + joined[:6000]},
            ],
            use_tools=False, max_tokens=900, timeout=40.0,
        )
        parsed = _parse_json_object(resp.get('content', ''))
        items = (parsed or {}).get('items') if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return []
        return self._apply_induced_items(owner, conversation_id, items, default_shared=False, confidence_delta=1)

    def _post_turn_maintenance(self, owner, conversation_id, question, answer, tool_summary, should_learn):
        # 1) 自动滚动压缩早期对话
        try:
            old_summary, older_turns = self.memory.turns_needing_summary(owner, conversation_id, keep_recent=6)
            if older_turns:
                transcript_lines = []
                for turn in older_turns:
                    transcript_lines.append('用户：' + turn['user_text'][:400])
                    if turn.get('tool_summary'):
                        transcript_lines.append('工具：' + turn['tool_summary'][:400])
                    transcript_lines.append('助手：' + turn['assistant_text'][:500])
                prompt_msgs = [
                    {
                        'role': 'system',
                        'content': '请把已有摘要和新增对话合并成一段简洁中文排障进展摘要（300字内），保留：用户目标、已查过的设备与关键结果、已确认结论或排除项、尚未解决的疑点。不要编造事实。',
                    },
                    {
                        'role': 'user',
                        'content': ('已有摘要：\n' + old_summary + '\n\n' if old_summary else '')
                                   + '新增较早对话：\n' + '\n'.join(transcript_lines),
                    },
                ]
                resp, _ = self._call_llm(prompt_msgs, use_tools=False, max_tokens=600, timeout=35.0)
                new_summary = (resp.get('content') or '').strip()
                if new_summary:
                    self.memory.save_summary(owner, conversation_id, new_summary, older_turns[-1]['id'])
        except Exception:
            pass

        # 2) 自动结构化提炼排障经验 / 纠错反思 / 偏好（带已有记忆去重与冲突替代）
        if not should_learn:
            return
        try:
            existing_ctx = self._existing_memory_context(owner, question + ' ' + answer[:300], limit=5)
            is_correction = bool(CORRECTION_HINT.search(question))
            extract_msgs = [
                {
                    'role': 'system',
                    'content': (
                        '分析这轮对话，判断是否包含值得长期记住的【用户偏好】、【历史纠错反思】、【拓扑规律】或【可复用排障经验】。'
                        + ('注意：用户本轮包含纠错或要求，若确实在纠正判断，请提炼为“原误判点 → 真实规律 → 下次优先核查”的防重犯规则。' if is_correction else '')
                        + '只提取可跨次复用的方法论/规律，绝不把一次性瞬时状态当成永久事实，并提醒现场细节差异。'
                        '对照【已有相关记忆】：完全无关或无新价值则不输出；若能丰富已有条目则设 action="merge" 并填 target_id；若新事实推翻旧记忆则设 action="supersede" 并填 target_id；全新则设 action="create"。'
                        '严格只返回 JSON：{"items":[{"action":"create|merge|supersede","target_id":"","kind":"preference|correction|topology_fact|troubleshooting_lesson","title":"简短标题","content":"具体可复用内容","scope":"适用范围","tags":"关键词1 关键词2"}]}。'
                        '如果没有值得记住的内容，返回 {"items":[]}。'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        '【已有相关记忆】\n' + existing_ctx
                        + '\n\n【本轮用户】\n' + question
                        + '\n\n【工具记录】\n' + (tool_summary or '无')
                        + '\n\n【本轮助手回答】\n' + answer[:2000]
                    ),
                },
            ]
            resp, _ = self._call_llm(extract_msgs, use_tools=False, max_tokens=700, timeout=35.0)
            parsed = _parse_json_object(resp.get('content', ''))
            if parsed and isinstance(parsed.get('items'), list):
                self._apply_induced_items(owner, conversation_id, parsed['items'][:2], default_shared=False)
        except Exception:
            pass
