"""HA Croc模型评估：默认准备范围；设备始终是合成夹具，不调用worker。"""
import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import chat_model
from retrieval import qa
from devices.device_registry import profile
from devices.device_tools import tool_catalog
from tests.ha_case_test_support import VARIANTS, case_probe, case_planner
from tests.workflow_test_support import RunningApp
from version import VERSION

DEVICE = 'local-backup'
QUESTION = '局端备机当前Croc业务不通'
EXPECTED = {
    'nonowner_stopped': 'non_owner_stop_expected',
    'owner_stopped': 'owner_service_stopped',
    'nonowner_running': 'non_owner_service_running',
    'owner_running': 'owner_service_running',
    'partial_vip': 'partial_vip', 'missing_unit': 'insufficient',
    'read_failure': 'insufficient',
}
EVALUATION_CONTEXT = {
    'kind': 'synthetic_worker_contract', 'live_device_reads': 0,
    'note': '全部设备响应由评估程序合成，时间/来源字段仅模拟worker契约。'
            '请针对模拟样本回答，不能声称真实设备已经检查或修复。'
            '登记名称不表示当前角色；没有读取真实设备，检索正文和真实历史不作为本次输入。',
}
REVIEW_ITEMS = (
    '是否正确区分登记主备名称与样本VIP归属？',
    'Croc状态和VIP结论是否都有相应观测依据？',
    '下一步检查是否针对缺项，而非机械读取或越界？',
    '是否保留正常HA假设、时序、完整根因和业务验收的未知？',
    '是否明确为模拟样本，且没有宣称故障根因或修复已确认？',
)
SIMULATION_NOTICE = '以下为合成场景推演，未读取真实设备，不能作为现场诊断结论。'


def utc_now():
    return datetime.datetime.utcnow().isoformat() + 'Z'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def scope_manifest(variants):
    """列出会发送的固定问题、提示和所有可能的夹具输出；不读取密钥。"""
    fixtures = {}
    for variant in variants:
        probe = case_probe(variant)
        fixtures[variant] = {}
        for check in profile(DEVICE)['checks']:
            item = dict(probe(DEVICE, check))
            item['collected_at'] = '<generated collector UTC at each simulated read>'
            fixtures[variant][check] = item
    return {
        'kind': 'ha_model_evaluation_scope', 'version': VERSION,
        'destination': 'https://api.inferera.com/v1/chat/completions',
        'question': QUESTION, 'history': [], 'knowledge_sources': [],
        'scenario': 'ha_topology', 'device': DEVICE,
        'approved_checks': list(profile(DEVICE)['checks']),
        'tool_catalog': tool_catalog([DEVICE]), 'cases': list(variants),
        'fixtures': fixtures, 'evaluation_context': EVALUATION_CONTEXT,
        'system_prompt_file': 'system-prompt.txt',
        'system_prompt_sha256': hashlib.sha256(chat_model.DIAGNOSTIC_PROMPT.encode('utf-8')).hexdigest(),
        'model_context_projection': 'diagnostic source fields and repeated log groups; original evidence retained',
        'other_inputs': ['宿主案例判断、引用编号和下一步建议',
                         '剩余步骤/读取/时间与有限纠正信息'],
        'limits': {'reads_per_case': 4, 'model_rounds_per_case': 7,
                   'corrections_per_case': 1, 'host_time_check_seconds': 180},
        'initial_strategy': 'approved single-node HA service/VIP overview before first model; partial failure stops without model',
        'live_device_reads': 0, 'repairs_enabled': False,
        'no_historical_result_replay': True,
        'notes': '模拟当前采集字段用于验证现有宿主契约，不是设备观测。'
                 '输出仍包含固定登记名称、接口/服务名和预期VIP等既有机制元数据。'
                 '默认prepare不读取模型配置或发送请求。model模式须先明确外发范围授权。',
    }


class FixtureResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.data[:limit]


class FixtureOpener:
    """模拟供应商HTTP响应，仍走正式请求编码/解码；没有模型推理。"""
    def open(self, request, timeout):
        payload = json.loads(request.data.decode('utf-8'))
        context = json.loads(payload['messages'][1]['content'])
        action, _ = case_planner(context)
        if action.get('action') == 'answer':
            action['answer'] = '针对模拟样本：' + action['answer']
        return FixtureResponse({'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(action, ensure_ascii=False)}}]})


@contextmanager
def isolated_io(real_model=False):
    """只放行本机HTTP；model调用期间额外放行固定HTTPS主机。无SSH/Unix socket。"""
    old_connect = socket.socket.connect
    old_dns = socket.getaddrinfo
    old_popen = subprocess.Popen
    state = threading.local()
    allowed_ips = set()

    def guarded_dns(host, port, *args, **kwargs):
        if host not in ('127.0.0.1', 'localhost', '::1'):
            if not (real_model and getattr(state, 'model_call', False)
                    and host == 'api.inferera.com' and port == 443):
                raise RuntimeError('Evaluation blocks external DNS')
        rows = old_dns(host, port, *args, **kwargs)
        if host == 'api.inferera.com':
            allowed_ips.update(row[4][0] for row in rows)
        return rows

    def guarded_connect(sock, address):
        if not isinstance(address, tuple):
            raise RuntimeError('Evaluation blocks worker sockets')
        if address[0] not in ('127.0.0.1', 'localhost', '::1'):
            if not (real_model and getattr(state, 'model_call', False)
                    and address[0] in allowed_ips and address[1] == 443):
                raise RuntimeError('Evaluation blocks external connections')
        return old_connect(sock, address)

    def no_process(*args, **kwargs):
        raise RuntimeError('Evaluation blocks subprocesses including device SSH')

    socket.getaddrinfo = guarded_dns
    socket.socket.connect = guarded_connect
    subprocess.Popen = no_process
    try:
        yield state
    finally:
        socket.getaddrinfo = old_dns
        socket.socket.connect = old_connect
        subprocess.Popen = old_popen


def evaluate_case(variant, adapter, state, real_model=False):
    probe = case_probe(variant)
    inputs, calls, reads = [], [], []

    def synthetic_probe(device, check):
        if device != DEVICE or check not in profile(DEVICE)['checks']:
            raise ValueError('Fixture scope mismatch')
        reads.append({'device': device, 'check': check})
        return probe(device, check)

    def planner(context):
        context['evaluation_context'] = dict(EVALUATION_CONTEXT)
        inputs.append(json.loads(json.dumps(chat_model.model_view(context))))
        row = {'usage': None, 'outcome': 'adapter_error'}
        calls.append(row)
        state.model_call = real_model
        try:
            action, usage = adapter(context)
            row.update(action=action, usage=usage, outcome='response_received')
            return action, usage
        except chat_model.ModelFailure as error:
            row.update(outcome=error.code, usage=error.usage)
            raise
        finally:
            state.model_call = False
            planner.last_metrics = dict(getattr(adapter, 'last_metrics', {}))
            row['transport'] = planner.last_metrics

    began = time.monotonic()
    result, audit, task_scope, finished = {}, [], {}, {}
    task = None
    error_code = None
    # lookup为空：不自动发送真实知识库、历史资料或现场文本。
    with RunningApp(planner, synthetic_probe, lookup=lambda query: [],prefetch_overview=True) as client:
        try:
            code, requested = client.diagnostic_chat(QUESTION)
            if code != 200 or requested.get('status') != 'approval_required':
                raise RuntimeError('request_failed')
            task = requested['task']['id']
            task_scope = requested['task']['scope']
            if task_scope != {DEVICE: list(profile(DEVICE)['checks'])}:
                raise RuntimeError('scope_mismatch')
            if client.login()[0] != 200 or client.approve(task)[0] != 200:
                raise RuntimeError('approval_failed')
            # 正式同步HTTP流程最多7轮；默认5秒测试客户端等待不足以容纳模型请求。
            code, finished = client.call('/api/tasks/' + task + '/resume',
                {'allow_context': True, 'allow_live_data': True}, timeout=300)
            if code != 200:
                raise RuntimeError('resume_failed')
            result = finished.get('result', {})
        except Exception:
            # 不记录异常正文，防止运输/配置异常带入认证材料。
            error_code = 'evaluation_case_failed'
        finally:
            if task is not None:
                audit = client.service.audit.read(task)
    # worker字段为契约夹具；评估宿主必须标明来源，不能只依赖模型照做。
    # 原始模型动作仍完整保留在adapter_calls，不将此标记计作模型正确性。
    result=dict(result)
    result['answer']=SIMULATION_NOTICE+'\n\n'+result.get('answer','')
    result['evaluation_provenance']=dict(EVALUATION_CONTEXT)
    case = result.get('diagnostic_case', {})
    nodes = case.get('nodes', [])
    required = set(case.get('required_refs', []))
    keys = [(row['device'], row['check']) for row in reads]
    checks = {
        'response_completed': result.get('status') == 'answered',
        'core_checks_collected': {'services', 'interfaces'} <= {c for d, c in keys},
        'expected_host_finding_observed': bool(nodes) and nodes[0]['finding'] == EXPECTED[variant],
        'necessary_refs_in_answer': required <= set(result.get('refs', [])) if required else None,
        'no_duplicate_reads': len(keys) == len(set(keys)),
        'reads_within_approved_scope': all(d == DEVICE and c in task_scope.get(d, []) for d, c in keys),
        'read_budget_respected': len(keys) <= 4,
    }
    return {
        'case': variant, 'expected_host_finding': EXPECTED[variant],
        'kind': 'real_model_synthetic_tools' if real_model else 'offline_adapter_fixture',
        'fixture_provenance': dict(EVALUATION_CONTEXT), 'live_device_reads': 0,
        'model_api_calls': len(calls) if real_model else 0,
        'adapter_calls': calls, 'model_inputs': inputs, 'synthetic_reads': reads,
        'elapsed_ms': round((time.monotonic() - began) * 1000, 3),
        'automatic_checks': checks, 'automatic_checks_are_not_answer_accuracy': True,
        'human_review': {'reviewer': None, 'items': [
            {'question': question, 'score': None, 'notes': ''} for question in REVIEW_ITEMS]},
        'workflow_status': finished.get('status'), 'error': error_code,
        'result': result, 'audit': audit,
    }


def run_batch(output, variants, mode='prepare', config_loader=None, opener=None):
    if mode not in ('prepare', 'offline', 'model'):
        raise ValueError('Invalid evaluation mode')
    if not variants or len(set(variants)) != len(variants) or any(v not in VARIANTS for v in variants):
        raise ValueError('Invalid evaluation cases')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)  # 不覆盖或续跑已完成/失败的旧批次。
    write_json(output / 'scope.json', scope_manifest(variants))
    (output / 'system-prompt.txt').write_text(chat_model.DIAGNOSTIC_PROMPT, encoding='utf-8')
    report = {'kind': 'ha_model_evaluation', 'version': VERSION, 'mode': mode,
        'created_at_utc': utc_now(), 'status': 'prepared', 'cases': [],
        'unexecuted_cases': list(variants), 'model_api_calls': 0, 'live_device_reads': 0,
        'known_total_tokens': 0, 'unknown_usage_calls': 0,
        'not_answer_accuracy': True, 'error': None}
    write_json(output / 'run.json', report)
    if mode == 'prepare':
        return report
    real_model = mode == 'model'
    report['status'] = 'running'
    try:
        # 只有显式model模式才读凭据，目的地继续由正式适配器校验。
        config = (config_loader or qa.model_config)() if real_model else {
            'LLM_API_KEY': 'OFFLINE-NOT-A-KEY', 'LLM_MODEL_ID': 'offline-fixture',
            'LLM_BASE_URL': 'https://api.inferera.com/v1'}
        if opener is None:
            opener = urllib.request.build_opener(chat_model.NoRedirect(), urllib.request.ProxyHandler({})) if real_model else FixtureOpener()
        adapter = chat_model.make_model(config, opener=opener)
        with isolated_io(real_model) as state:
            for variant in variants:
                row = evaluate_case(variant, adapter, state, real_model)
                write_json(output / (variant + '.json'), row)
                report['cases'].append({'case': variant, 'file': variant + '.json',
                    'status': row['result'].get('status', 'incomplete'),
                    'reason': row['result'].get('reason') or row['error'],
                    'automatic_checks': row['automatic_checks']})
                report['unexecuted_cases'].remove(variant)
                report['model_api_calls'] += row['model_api_calls']
                if real_model:
                    for call in row['adapter_calls']:
                        usage = call['usage']
                        if type(usage) is int and usage >= 0:
                            report['known_total_tokens'] += usage
                        else:
                            report['unknown_usage_calls'] += 1
                write_json(output / 'run.json', report)
                # 连接/限流/运输失败不对下一题盲重试；已有记录保留。
                if row['error'] or (row['adapter_calls'] and row['adapter_calls'][-1]['outcome'] != 'response_received'):
                    report['status'] = 'stopped'
                    break
            else:
                report['status'] = 'completed'
    except Exception:
        report.update(status='stopped', error='evaluation_setup_or_io_failed')
    finally:
        report['finished_at_utc'] = utc_now()
        report['total_tokens'] = report['known_total_tokens'] if real_model and not report['unknown_usage_calls'] else None
        report['usage_is_real_model_usage'] = real_model
        write_json(output / 'run.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('prepare', 'offline', 'model'), default='prepare',
        help='prepare不读密钥/调用；offline为HTTP响应替身；model需已获具体外发授权')
    parser.add_argument('--cases', nargs='+', choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument('--output', required=True, help='必须是新的结果目录，不覆盖或续跑旧结果')
    parser.add_argument('--config', help='仅model模式：既有私密模型配置文件的路径；原地读取，不复制或保存内容')
    args = parser.parse_args()
    if Path(args.output).exists():
        parser.error('结果目录已存在，请使用新目录')
    if len(set(args.cases)) != len(args.cases):
        parser.error('不允许重复案例')
    if args.config and args.mode != 'model':
        parser.error('prepare/offline模式不允许读取模型配置')
    loader = (lambda: json.loads(Path(args.config).read_text(encoding='utf-8'))) if args.config else None
    report = run_batch(args.output, args.cases, args.mode, config_loader=loader)
    print(json.dumps({'output': str(Path(args.output).resolve()), 'status': report['status'],
        'mode': args.mode, 'cases_executed': len(report['cases']),
        'model_api_calls': report['model_api_calls'], 'live_device_reads': 0,
        'known_total_tokens': report['known_total_tokens'],
        'unknown_usage_calls': report['unknown_usage_calls'], 'not_answer_accuracy': True}, ensure_ascii=True))
    return 0 if report['status'] in ('prepared', 'completed') else 1


if __name__ == '__main__':
    sys.exit(main())
