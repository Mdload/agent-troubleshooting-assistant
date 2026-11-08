"""可注入模型的有限诊断循环；不因知识检索零命中而终止。"""
import json
import sqlite3
import uuid
import time
from device_tools import DEVICES, CHECKS, tool_catalog

class AuditStore:
    """独立SQLite诊断记录，不连接业务数据库。路径由可信操作员配置。"""
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS events (task TEXT, step INTEGER, kind TEXT, body TEXT)')

    def add(self, task, step, kind, body):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO events VALUES (?, ?, ?, ?)',
                       (task, step, kind, json.dumps(body, ensure_ascii=False)))

    def read(self, task):
        with sqlite3.connect(self.path) as db:
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
