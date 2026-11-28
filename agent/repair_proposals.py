"""Validated repair plans and their one-time approval record."""
import json
import re
import secrets
import shlex
import sqlite3
import time
from contextlib import closing, contextmanager

from server.approval_store import TaskError
from devices.device_registry import (
    APPROVAL_SERVICE_UNITS, DEVICES, IP_FORWARD_TARGET, REPAIR_SERVICE_VERBS,
    allows_auto_repair, allows_repair, run_command_actions, service_unit,
)
from devices.device_tools import repair_command_text


MAX_STEPS = 6
ACTION_LABELS = {
    'start_service': '启动服务',
    'restart_service': '重启服务',
    'reset_failed_service': '清除服务失败状态',
    'stop_service': '停止服务',
    'enable_ip_forward': '恢复 IPv4 转发',
    'add_route': '添加业务静态路由',
    'del_route': '删除业务静态路由',
    'bring_up_interface': '启用业务接口',
    'run_command': '执行运维命令',
}
ACTION_RISKS = {
    'start_service': '启动服务可能改变业务状态；启动 Keepalived 可能触发主备选举。',
    'restart_service': '重启业务可能短时中断；重启 Keepalived 可能触发主备切换。',
    'reset_failed_service': '清除失败标记和启动限速计数。',
    'stop_service': '停止服务会中断对应业务，停止 Keepalived 可能触发主备切换。',
    'enable_ip_forward': '恢复 IPv4 转发会改变经过该路由器的流量。',
    'add_route': '添加主路由表条目会改变相关业务网段的选路。',
    'del_route': '删除静态路由会改变相关业务网段的选路。',
    'bring_up_interface': '启用业务接口可能改变链路和业务转发状态。',
    'run_command': '高危守卫已通过，须人工批准。请核对命令、目标设备和回滚说明。',
}


def device_id(value):
    if not isinstance(value, str):
        raise ValueError('unknown_device')
    name = value.strip().lower().replace('_', '-').replace(' ', '')
    aliases = {
        '局端主': 'local-primary', '局端主节点': 'local-primary',
        '局端备': 'local-backup', '局端备用': 'local-backup',
        '远端主': 'remote-primary', '远端主节点': 'remote-primary',
        '远端备': 'remote-backup', '远端备用': 'remote-backup',
        '路由器1': 'router-1', '路由器2': 'router-2',
    }
    for key, device in DEVICES.items():
        if name in (key, device['label'].lower(), device['address']):
            return key
    if name in aliases:
        return aliases[name]
    matches = [key for key, device in DEVICES.items() if device['hostname'] == name]
    if len(matches) == 1:
        return matches[0]
    raise ValueError('unknown_device')


def command_actions(command):
    if not isinstance(command, str) or len(command) > 512:
        raise ValueError('invalid_command')
    words = shlex.split(command)
    if words[:1] == ['sudo']:
        words = words[1:]
    if len(words) >= 3 and words[0] == 'systemctl':
        for action, verb in REPAIR_SERVICE_VERBS.items():
            if words[1] == verb:
                return [(action, ' '.join(words[2:]))]
    if words == ['sysctl', '-w', IP_FORWARD_TARGET + '=1']:
        return [('enable_ip_forward', IP_FORWARD_TARGET)]
    if len(words) == 6 and words[:4] == ['ip', 'link', 'set', 'dev'] and words[-1] == 'up':
        return [('bring_up_interface', words[4])]
    route_words = ['ip', *words[2:]] if words[:2] == ['ip', '-4'] else words
    if route_words[:2] == ['ip', 'route'] and route_words[2:3] in (['add'], ['del']):
        parts = route_words[3:-2] if route_words[-2:] == ['proto', 'static'] else route_words[3:]
        if len(parts) == 5:
            return [(route_words[2] + '_route', ' '.join(parts))]
    raise ValueError('unsupported_command')


def normalized_steps(args):
    entries = args.get('steps', [args])
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_STEPS:
        raise ValueError('invalid_steps')
    steps = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('invalid_steps')
        device = device_id(entry.get('device') or args.get('device'))
        action = entry.get('action')
        target = entry.get('target')
        if action and (not isinstance(action, str) or action not in ACTION_LABELS):
            raise ValueError('action_not_allowed')
        if action == 'run_command':
            run_command_actions(device, target)
            steps.append({'device': device, 'action': action, 'target': target})
            continue
        if action in ('add_route', 'del_route') and target is None:
            target = '{} via {} dev {}'.format(entry.get('cidr'), entry.get('via'), entry.get('dev'))
        if isinstance(action, str) and isinstance(target, str) and target.strip():
            actions = [(action, target)]
        else:
            commands = entry.get('commands')
            if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_STEPS:
                raise ValueError('missing_action_parameters')
            actions = [item for command in commands for item in command_actions(command)]
        for action, target in actions:
            if action in REPAIR_SERVICE_VERBS:
                units = target.replace('，', ',').replace(',', ' ').split()
                targets = [service_unit(unit) for unit in units]
            elif action == 'enable_ip_forward' and target.strip() in (IP_FORWARD_TARGET, IP_FORWARD_TARGET+'=1'):
                targets = [IP_FORWARD_TARGET]
            else:
                targets = [' '.join(target.split())]
            if not targets:
                raise ValueError('missing_action_parameters')
            for value in targets:
                if not allows_repair(device, action, value):
                    raise ValueError('action_not_allowed')
                steps.append({'device': device, 'action': action, 'target': value})
                if len(steps) > MAX_STEPS:
                    raise ValueError('too_many_steps')
    if len(steps) > MAX_STEPS:
        raise ValueError('too_many_steps')
    return steps


def review_check(action, target):
    if action in REPAIR_SERVICE_VERBS and target in APPROVAL_SERVICE_UNITS:
        return 'service'
    if action == 'stop_test_server':
        return 'listening_ports'
    if action == 'list_firewall':
        return 'nat_firewall'
    if action in ('enable_ip_forward', 'add_route', 'del_route'):
        return 'routes'
    if action == 'bring_up_interface':
        return 'interfaces'
    return 'services'


def verification_actions(step):
    device, action, target = (step[key] for key in ('device', 'action', 'target'))
    if action == 'run_command':
        return [(device, parsed, value) for parsed, value in run_command_actions(device, target)]
    return [(device, action, target)]


def summarize_review(check, target, observation, action=None):
    review = {key: observation[key] for key in ('ok', 'error', 'collected_at') if key in observation}
    review['check'] = check
    if not review.get('ok'):
        review['summary'] = '未取得当前状态。'
        return review
    lines = observation.get('output', '').splitlines()
    if action == 'stop_test_server':
        listening = any(len(fields) >= 5 and fields[4].endswith(':' + target)
                        for fields in map(str.split, lines))
        review.update(ok=not listening, summary='TCP/UDP port ' + target)
    elif action == 'list_firewall':
        review['summary'] = 'NAT / iptables'
    elif action in ('add_route', 'del_route'):
        cidr, _, via, _, interface = target.split()
        expected = [cidr.split('/')[0] if cidr.endswith('/32') else cidr, 'via', via, 'dev', interface]
        present = any(
            line.split()[:5] in (expected, [cidr, *expected[1:]]) for line in lines
        )
        review.update(ok=present if action == 'add_route' else not present,
                      summary=('已读到目标路由：' if present else '主路由表未读到目标路由：') + target)
    elif check == 'interfaces':
        up = any(line.split()[:1] == [target] and
                 (flags := re.search(r'<([^>]+)>', line)) and 'UP' in flags[1].split(',')
                 for line in lines)
        review.update(ok=up, summary=target + (' 已启用' if up else ' 未读到启用标志'))
    elif check == 'routes':
        state = next((line for line in lines if line in ('IP_FORWARD=0', 'IP_FORWARD=1')), '')
        review['summary'] = 'IPv4 转发：' + {'IP_FORWARD=1': '已开启（1）', 'IP_FORWARD=0': '已关闭（0）'}.get(state, '未采集到')
        review['ok'] = state == 'IP_FORWARD=1'
    else:
        units = observation.get('output', '').strip().split('\n\n')
        fields = next((dict(line.split('=', 1) for line in unit.splitlines() if '=' in line)
                       for unit in units if check == 'service' or 'Id='+target in unit.splitlines()), {})
        labels = {'LoadState': '加载', 'ActiveState': '运行', 'SubState': '子状态', 'Result': '结果'}
        review['summary'] = target + ' · ' + '，'.join(
            label+'：'+fields[key] for key, label in labels.items() if key in fields
        ) if fields.get('ActiveState') else '未采集到 '+target+' 的当前状态。'
        state = fields.get('ActiveState')
        if action == 'reset_failed_service':
            review['ok'] = state not in (None, 'failed')
        else:
            review['ok'] = state == ('inactive' if action == 'stop_service' else 'active')
    return review


def combine_reviews(reviews):
    if len(reviews) == 1:
        return reviews[0]
    return {'ok': all(item['ok'] for item in reviews),
            'summary': '；'.join(item['summary'] for item in reviews)}


class RepairProposals:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS repair_proposals (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL,
                device TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
                command TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                approved_at REAL, finished_at REAL, execution TEXT, review TEXT
            )''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(repair_proposals)')}
            for column in ('steps', 'metadata', 'execution_mode', 'step_results', 'verification_started_at'):
                if column not in columns:
                    db.execute('ALTER TABLE repair_proposals ADD COLUMN ' + column + ' TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS repair_proposals_conversation '
                       'ON repair_proposals(owner, conversation, created_at)')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def create(self, owner, conversation, device=None, action=None, target=None, *,
               steps=None, reason='', rollback='', execution_mode='approval'):
        if steps is None:
            steps = [dict(device=device, action=action, target=target)]
        if execution_mode not in ('approval', 'auto') or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError('invalid_steps')
        steps = [dict(step, command=repair_command_text(step['device'], step['action'], step['target']))
                 for step in steps]
        if any(step['action'] == 'run_command' for step in steps):
            if not rollback.strip():
                raise ValueError('rollback_required')
        if execution_mode == 'auto' and not all(allows_auto_repair(
                step['device'], step['action'], step['target']) for step in steps):
            execution_mode = 'approval'
        first = steps[0]
        ident = secrets.token_hex(16)
        metadata = json.dumps({'reason': reason[:1000], 'rollback': rollback[:1000]}, ensure_ascii=False)
        with self.connect() as db:
            db.execute('''INSERT INTO repair_proposals
                (id, owner, conversation, device, action, target, command, status, created_at,
                 steps, metadata, execution_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       (ident, owner, conversation, first['device'], first['action'], first['target'],
                        first['command'], 'auto_pending' if execution_mode == 'auto' else 'pending_approval',
                        time.time(), json.dumps(steps), metadata, execution_mode))
            return self.card(self._row(db, ident, owner))

    @staticmethod
    def _row(db, ident, owner):
        row = db.execute('SELECT * FROM repair_proposals WHERE id=? AND owner=?', (ident, owner)).fetchone()
        if row is None:
            raise TaskError('not_found')
        return row

    @staticmethod
    def _steps(row):
        return json.loads(row['steps']) if row['steps'] else [
            {key: row[key] for key in ('device', 'action', 'target', 'command')}
        ]

    def for_conversation(self, owner, conversation):
        with self.connect() as db:
            return [self.card(row) for row in db.execute(
                'SELECT * FROM repair_proposals WHERE owner=? AND conversation=? ORDER BY created_at',
                (owner, conversation))]

    def claim_verification(self, ident, owner, conversation):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, ident, owner)
            if row['conversation'] != conversation:
                raise TaskError('not_found')
            if (row['execution_mode'] not in (None, 'approval') or row['status'] not in ('executed', 'failed')
                    or not row['approved_at'] or not row['step_results'] or row['verification_started_at']):
                raise TaskError('proposal_verification_unavailable')
            db.execute('UPDATE repair_proposals SET verification_started_at=? WHERE id=?', (time.time(), ident))
            return self.card(row)

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("""UPDATE repair_proposals SET status='failed', finished_at=?,
                       execution=?
                       WHERE status IN ('executing', 'auto_pending')""",
                       (time.time(), json.dumps({'ok': False, 'error': 'execution_interrupted'})))

    def reject(self, ident, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, ident, owner)
            if row['status'] == 'pending_approval':
                db.execute("UPDATE repair_proposals SET status='rejected', finished_at=? WHERE id=?",
                           (time.time(), ident))
                row = self._row(db, ident, owner)
            return self.result(row)

    def execute(self, ident, owner, repair, probe, *, mode='approval', guard=None, probe_target=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, ident, owner)
            expected = 'auto_pending' if mode == 'auto' else 'pending_approval'
            if row['status'] != expected or (row['execution_mode'] or 'approval') != mode:
                return self.result(row)
            steps = self._steps(row)
            first = {key: row[key] for key in ('device', 'action', 'target', 'command')}
            if steps[0] != first or any(
                (mode == 'auto' and not allows_auto_repair(step['device'], step['action'], step['target']))
                or not allows_repair(step['device'], step['action'], step['target'])
                or repair_command_text(step['device'], step['action'], step['target']) != step['command']
                for step in steps
            ):
                raise TaskError('proposal_scope_changed')
            devices = {step['device'] for step in steps}
            for running in db.execute("SELECT * FROM repair_proposals WHERE status='executing'"):
                if devices.intersection(step['device'] for step in self._steps(running)):
                    raise TaskError('proposal_device_busy')
            db.execute("UPDATE repair_proposals SET status='executing', approved_at=? WHERE id=?",
                       (time.time(), ident))

        step_results = []
        failure = None
        for step in steps:
            device, action, target = (step[key] for key in ('device', 'action', 'target'))
            if failure:
                step_results.append({'status': 'skipped'})
                continue
            try:
                if guard is not None:
                    guard()
            except Exception:
                outcome = {'ok': False, 'error': 'account_session_expired'}
            else:
                try:
                    result = repair(device, action, target)
                    outcome = {key: result[key] for key in
                               ('ok', 'error', 'return_code', 'duration_ms', 'collected_at') if key in result}
                except Exception:
                    outcome = {'ok': False, 'error': 'repair_transport_unconfirmed'}
            if outcome.get('ok') is not True:
                failure = outcome
            step_results.append({'status': 'failed' if failure else 'executed', 'execution': outcome})
            with self.connect() as db:
                db.execute('UPDATE repair_proposals SET step_results=? WHERE id=?',
                           (json.dumps(step_results), ident))

        observations = {}
        for step, outcome in zip(steps, step_results):
            reviews = []
            for device, action, target in verification_actions(step):
                check = review_check(action, target)
                key = (device, check, target if check == 'service' else None)
                if key not in observations:
                    try:
                        if guard is not None:
                            guard()
                        observations[key] = (probe_target(device, check, target) if check == 'service'
                                             else probe(device, check))
                    except Exception:
                        observations[key] = None
                observation = observations[key]
                if not isinstance(observation, dict):
                    observation = {'ok': False, 'error': 'review_transport_failed'}
                reviews.append(summarize_review(check, target, observation, action))
            outcome['review'] = combine_reviews(reviews)
        review = combine_reviews([outcome['review'] for outcome in step_results])
        execution = failure or (step_results[0]['execution'] if len(steps) == 1 else {'ok': True})
        status = 'failed' if failure else 'executed'
        with self.connect() as db:
            db.execute('''UPDATE repair_proposals SET status=?, finished_at=?, execution=?, review=?,
                          step_results=? WHERE id=?''',
                       (status, time.time(), json.dumps(execution), json.dumps(review, ensure_ascii=False),
                        json.dumps(step_results, ensure_ascii=False), ident))
            return self.result(self._row(db, ident, owner))

    def fail_auto(self, ident, owner, reason):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE repair_proposals SET status='failed', finished_at=?, execution=?
                          WHERE id=? AND owner=? AND status='auto_pending'""",
                       (time.time(), json.dumps({'ok': False, 'error': reason}), ident, owner))
            return self.result(self._row(db, ident, owner))

    @staticmethod
    def result(row):
        execution = json.loads(row['execution'] or 'null')
        mode = row['execution_mode'] or 'approval'
        status = 'executing' if row['status'] == 'auto_pending' else row['status']
        return {'proposal_id': row['id'], 'status': status, 'execution_mode': mode,
                'approval_button_rendered': status == 'pending_approval' and mode == 'approval',
                'executed': status == 'executed', 'execution': execution,
                'review': json.loads(row['review'] or 'null'),
                'step_results': json.loads(row['step_results'] or '[]')}

    def card(self, row):
        steps = self._steps(row)
        display = []
        for step in steps:
            action = step['action']
            display.append(dict(step, device_label=DEVICES[step['device']]['label'],
                                action_label=ACTION_LABELS[action], risk=ACTION_RISKS[action]))
        return dict(display[0], **json.loads(row['metadata'] or '{}'),
                    steps=display, commands=[step['command'] for step in display], **self.result(row))
