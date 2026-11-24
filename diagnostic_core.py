"""可注入模型的有限诊断循环；不因知识检索零命中而终止。"""
import json
import sqlite3
import uuid
import time
import threading
from contextlib import closing, contextmanager
from device_tools import DEVICES, CHECKS, tool_catalog
from diagnostic_logs import echoes_log_credential, filter_log_output

class RequestTrace(threading.local):
    # HTTP 处理在同一线程内同步完成，后台使用已保存的任务 ID。
    value = None

    def get(self):
        return self.value

    def set(self, value):
        previous = self.value
        self.value = value
        return previous

    def reset(self, previous):
        self.value = previous


request_trace = RequestTrace()


def audit_text(value, credentials=()):
    if not isinstance(value,str):
        return ''
    text, _ = filter_log_output(value[:16000])
    if echoes_log_credential(text,credentials):
        return '[含请求中的敏感值，已隐藏]'
    return text

class AuditStore:
    """独立SQLite诊断记录，不连接业务数据库。路径由可信操作员配置。"""
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS events (task TEXT, step INTEGER, kind TEXT, body TEXT)')

    @contextmanager
    def connect(self):
        # sqlite3 的事务上下文不会关闭连接，需在提交或回滚后释放句柄。
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('PRAGMA busy_timeout=5000')
            yield db

    def add(self, task, step, kind, body):
        with self.connect() as db:
            db.execute('INSERT INTO events VALUES (?, ?, ?, ?)',
                       (task, step, kind, json.dumps(body, ensure_ascii=False)))

    def record(self, task, step, kind, body):
        """Optional trace fields stay independent of execution and recovery state."""
        try:
            self.add(task,step,kind,dict(body,at=time.time()))
        except sqlite3.Error:
            # A telemetry write cannot change an approval or re-run a tool.
            return False
        return True

    def read(self, task):
        with self.connect() as db:
            return [(r[0], r[1], json.loads(r[2])) for r in db.execute(
                'SELECT step,kind,body FROM events WHERE task=? ORDER BY rowid', (task,))]

def run_diagnosis(question, planner, probe, audit, allowed_devices, max_checks=4):
    """planner收到证据后选择check或finish；该轮设备范围由调用方授权。"""
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise ValueError('Invalid question')
    scope = set(allowed_devices)
    if not scope or not scope <= set(DEVICES) or not 1 <= max_checks <= 6:
        raise ValueError('Invalid scope or budget')
    task = uuid.uuid4().hex
    deadline = time.monotonic() + 150
    evidence, seen = [], set()
    audit.add(task, 0, 'start', {'devices': sorted(scope), 'max_checks': max_checks})
    # 不保存问题原文、SSH认证信息或模型原始输出，防止凭据被误写进记录。
    for step in range(max_checks + 1):
        if time.monotonic() >= deadline:
            reason = 'time_budget_reached'
            break
        try:
            context = json.loads(json.dumps({'question': question, 'devices': sorted(scope),
                'checks': sorted(CHECKS), 'tool_catalog': tool_catalog(sorted(scope)),
                'evidence': evidence, 'remaining_checks': max_checks-step}))
            action = planner(context)
            if time.monotonic() >= deadline:
                reason = 'time_budget_reached'
                break
            if not isinstance(action, dict):
                raise ValueError()
            if action.get('action') == 'finish':
                text, refs = action.get('answer'), action.get('evidence_ids')
                valid = {e['id'] for e in evidence if e['ok']}
                if not isinstance(text, str) or not 1 <= len(text) <= 700:
                    raise ValueError()
                if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in valid for r in refs):
                    raise ValueError()
                result = {'task': task, 'status': 'answered', 'answer': text,
                          'evidence_ids': refs, 'evidence': evidence}
                audit.add(task, step+1, 'finish', {'status': 'answered', 'evidence_ids': refs})
                return result
            if step == max_checks:
                reason = 'check_budget_reached'
                break
            if set(action) != {'action', 'device', 'check'} or action['action'] != 'check':
                raise ValueError()
            device, check = action['device'], action['check']
            if device not in scope or check not in CHECKS or (device, check) in seen:
                raise ValueError()
        except Exception:
            reason = 'invalid_or_unavailable_plan'
            break
        seen.add((device, check))
        audit.add(task, step+1, 'requested', {'device': device, 'check': check})
        try:
            item = dict(probe(device, check))
        except Exception:
            item = {'device': device, 'check': check, 'ok': False, 'error': 'tool_error', 'output': ''}
        item['id'] = 'OBS-' + str(len(evidence)+1)
        evidence.append(item)
        audit.add(task, step+1, 'observation', item)
        if not item.get('ok'):
            reason = item.get('error', 'check_failed')
            break
    result = {'task': task, 'status': 'incomplete', 'reason': reason,
              'answer': '证据尚不足以给出结论；已停止检查。', 'evidence': evidence}
    audit.add(task, len(evidence)+1, 'stop', {'reason': reason})
    return result
