"""新编验收题：准备输入范围、离线契约回放，或明确获批后的真实模型测试。"""
import argparse
import copy
from contextlib import closing
import datetime
import hashlib
from functools import partial
import json
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chat_model
import qa
from ha_case_test_support import case_probe, case_planner
from workflow_test_support import RunningApp
from unified_chat import search, prefetch_query
from approval_store import SINGLE_APPROVAL
from evaluation.ha_model import isolated_io, write_json, utc_now, SIMULATION_NOTICE
from version import VERSION
from model_trace import safe_metrics

DEFAULT_SUITE = ROOT / 'evaluation/acceptance-20261015/suite.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_suite(path):
    suite = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    cases = suite['cases']
    if not cases or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Empty suite or duplicate IDs')
    return suite


def fixture_probe(name):
    base = case_probe('owner_stopped' if name in ('historical_log', 'stale') else name)
    def probe(device, check):
        item = dict(base(device, check))
        if name == 'stale':
            item['collected_at'] = (datetime.datetime.utcnow()-datetime.timedelta(days=1)).isoformat()+'Z'
        if name == 'historical_log' and check == 'logs':
            item['output'] = 'LOG_SCOPE=historical events only\n[unit=croc-sar.service device_time_us=1] Started long ago; this is not current service state.'
        return item
    return probe


def prepare_manifest(path, cases):
    sources = {}
    fixtures = {}
    for case in cases:
        query = prefetch_query(case['question'], case.get('history', [])) or case['question']
        sources[case['id']] = [] if case.get('chat_space') == 'ha' else search(query)
        if case.get('fixture'):
            probe = fixture_probe(case['fixture'])
            fixtures[case['id']] = {}
            for check in ('services', 'interfaces', 'routes', 'logs'):
                item = probe(case['device'], check)
                item['collected_at'] = '<generated UTC; stale scenario deliberately uses one day earlier>'
                fixtures[case['id']][check] = item
    return {'kind': 'representative_evaluation_scope', 'version': VERSION,
            'suite_sha256': digest(path), 'case_ids': [case['id'] for case in cases],
            'destination': 'https://api.inferera.com/v1/chat/completions',
            'questions_and_synthetic_histories': [{key: case[key] for key in ('id', 'question', 'history', 'mode', 'chat_space', 'followups') if key in case} for case in cases],
            'fixed_sources_by_case': sources, 'synthetic_outputs_by_case': fixtures,
            'system_prompts': {'qa': chat_model.PROMPT, 'knowledge': chat_model.KNOWLEDGE_PROMPT,
                               'diagnostic': chat_model.DIAGNOSTIC_PROMPT,
                               'general_learning': chat_model.LEARNING_PROMPT,
                               'ha_intake': chat_model.ha_intake_prompt({'diagnostic_options': True})},
            'not_sent_to_model': ['expected findings, rubric, expected source IDs, expected read counts'],
            'limits': {'reads_per_task': 4, 'steps_per_task': 7, 'corrections_per_task': 1},
            'live_device_reads': 0, 'real_conversation_exports': False,
            'retrieval_policy': 'Only this case fixed source snapshot; model query rewrites cannot add new material.',
            'code_sha256': {name: digest(ROOT/name) for name in ('unified_chat.py', 'chat_model.py', 'model_trace.py',
                'conversation_topic.py', 'topic_scope.py',
                'diagnostic_context.py', 'device_tools.py', 'mechanism_guard.py', 'knowledge_coverage.py',
                'question_routing.py', 'ha_croc_case.py', 'evaluation/representative.py',
                'diagnostic_intake.py', 'lab_workflow.py', 'approval_store.py')}}


def offline_planner(context):
    """只演练协议，绝不读取题集预期或假装评判回答语义。"""
    if context.get('diagnostic_case'):
        return case_planner(context)
    coverage = context.get('knowledge_sufficiency', {})
    refs = coverage.get('supporting_refs', [])
    return {'action': 'answer', 'answer': '离线响应替身：已接收本轮输入；不评价实际模型的解释质量，未知设备状态没有确认。',
            'refs': refs, 'basis': 'evidence' if refs else 'limited'}, 0


def run_case(case, prepared_sources, adapter, state, real_model=False):
    inputs, calls, reads = [], [], []
    task_state = {}
    now = [1000]
    base_probe = fixture_probe(case['fixture']) if case.get('fixture') else None

    def probe(device, check):
        if base_probe is None or device != case.get('device'):
            raise RuntimeError('No fixture authorized for this target')
        reads.append({'device': device, 'check': check})
        item = base_probe(device, check)
        if len(reads) == 1:
            if case.get('control') == 'cancel_after_first':
                client.store.cancel(task_state['id'], task_state['owner'])
            elif case.get('control') == 'expire_after_first':
                now[0] += 301
        return item

    def planner(context):
        context['evaluation_context'] = {'kind': 'synthetic_known_input', 'live_device_reads': 0,
            'note': '问题、短历史及设备输出均为测试材料；说明模拟性质。不要声称已检查或修复真实设备。'}
        inputs.append(copy.deepcopy(chat_model.model_view(context)))
        call = {'outcome': 'pending', 'usage': None}
        calls.append(call)
        planner.last_metrics = {}
        state.model_call = real_model
        try:
            action, usage = adapter(context)
            call.update(outcome='response_received', usage=usage, action=copy.deepcopy(action))
            return action, usage
        except chat_model.ModelFailure as error:
            call.update(outcome=error.code, usage=error.usage)
            raise
        finally:
            state.model_call = False
            # wrapper不能丢失实际adapter的计时；只复制固定数字/类别。
            planner.last_metrics = safe_metrics(getattr(adapter, 'last_metrics', None))
            call['transport'] = copy.deepcopy(planner.last_metrics)

    began = time.monotonic()
    with RunningApp(planner, probe, lookup=lambda query: copy.deepcopy(prepared_sources), prefetch_overview=True) as client:
        client.call = partial(client.call, timeout=300)
        client.store.clock = lambda: now[0]
        ask = client.diagnostic_chat if case.get('mode') == 'diagnostic' else client.chat
        turns = []
        if case.get('chat_space') == 'ha':
            code, mode = client.enter_diagnostic('blind')
            if code != 200:
                raise RuntimeError('Could not enter isolated HA conversation')
            for question in [case['question']] + case.get('followups', []):
                code, requested = client.call('/api/chat', {'conversation_id': 'blind', 'chat_space': 'ha',
                    'interaction_mode': mode['mode'], 'interaction_revision': mode['revision'],
                    'question': question, 'allow_context': True, 'history': [],
                    'approval_flow': SINGLE_APPROVAL}, timeout=300)
                turns.append({'http': code, 'status': requested.get('status'),
                              'answer': requested.get('answer', ''), 'task': requested.get('task')})
        else:
            code, requested = ask(case['question'], history=case.get('history', []))
        result, finished, audit = requested, {}, []
        resume_code = None
        if requested.get('status') == 'approval_required':
            task_state['id'] = requested['task']['id']
            with closing(client.store.connect()) as db:
                task_state['owner'] = client.store.row(db, task_state['id'])['owner']
            if case.get('control') == 'unapproved' or case.get('chat_space') == 'ha':
                resume_code, _ = client.resume(task_state['id'])
                finished = client.call('/api/tasks/'+task_state['id'])[1]
                result = {'status': 'not_started', 'answer': '', 'evidence': []}
            else:
                client.login()
                client.approve(task_state['id'])
                resume_code, finished = client.call('/api/tasks/'+task_state['id']+'/resume',
                    {'allow_context': True, 'allow_live_data': True}, timeout=300)
                result = finished.get('result', {})
            audit = client.service.audit.read(task_state['id'])
        elapsed = time.monotonic()-began
    case_report = result.get('diagnostic_case', {})
    finding = next((node['finding'] for node in case_report.get('nodes', []) if node['device'] == case.get('device')), None)
    checks = {'request_http_ok': code == 200, 'read_budget': len(reads) <= 4,
              'no_duplicate_reads': len({(r['device'], r['check']) for r in reads}) == len(reads),
              'target_scope': all(r['device'] == case.get('device') for r in reads)}
    expectations = [('expected_request_status', requested.get('status'), 'request_status'),
                    ('expected_result_status', result.get('status'), 'result_status'),
                    ('expected_finding', finding, 'host_finding'),
                    ('expected_reads', len(reads), 'read_count'),
                    ('expected_attempts', finished.get('attempts'), 'attempt_count'),
                    ('expected_reason', result.get('reason'), 'stop_reason'),
                    ('expected_workflow_status', finished.get('status'), 'workflow_status')]
    for key, observed, label in expectations:
        if key in case: checks[label] = observed == case[key]
    if case.get('control') == 'unapproved': checks['unapproved_rejected'] = resume_code == 409
    if turns:
        checks['all_turns_http_ok'] = all(turn['http'] == 200 for turn in turns)
        if requested.get('status') == 'approval_required':
            checks['unapproved_rejected'] = resume_code == 409
        checks['no_reads_or_early_approval'] = not reads and not any(isinstance(turn['task'],dict) for turn in turns[:-1])
        questions = [turn['answer'].strip().rstrip('？?') for turn in turns if turn['status'] == 'clarify']
        checks['no_repeated_clarification'] = len(questions) == len(set(questions))
        if 'expected_goal' in case:
            observed_goal = (requested.get('task') or requested).get('diagnostic_goal')
            checks['diagnostic_goal'] = observed_goal == case['expected_goal']
        scope = (requested.get('task') or {}).get('scope', {})
        if 'allowed_targets' in case:
            checks['proposed_targets'] = bool(scope) and set(scope) <= set(case['allowed_targets'])
        if 'expected_scope' in case:
            checks['directed_scope'] = scope == case['expected_scope']
    if case.get('expected_knowledge_status'):
        checks['knowledge_limit'] = result.get('knowledge_status') in case['expected_knowledge_status']
    source_ids = [source['id'] for source in prepared_sources]
    if case.get('expected_source_ids'):
        checks['expected_sources_available'] = set(case['expected_source_ids']) <= set(source_ids)
    return {'id': case['id'], 'kind': 'real_model_synthetic_tools' if real_model else 'offline_protocol_fixture',
            'request_status': requested.get('status'), 'workflow_status': finished.get('status'),
            'elapsed_seconds': round(elapsed, 3), 'host_finding': finding,
            'automatic_checks': checks, 'automatic_checks_are_not_answer_accuracy': True,
            'semantic_review': {'reviewer': None, 'verdict': None, 'required_points': case['required_points'], 'notes': ''},
            'model_inputs': inputs, 'adapter_calls': calls, 'synthetic_reads': reads, 'audit': audit,
            'dialogue_turns': turns,
            'result': dict(result, answer=SIMULATION_NOTICE+'\n\n'+result.get('answer', ''))}


def run_suite(path, output, mode='prepare', ids=None, config_loader=None):
    if mode not in ('prepare', 'offline', 'model'):
        raise ValueError('Invalid mode')
    suite = load_suite(path)
    cases = suite['cases']
    if ids:
        if len(set(ids)) != len(ids) or set(ids)-{case['id'] for case in cases}:
            raise ValueError('Unknown or duplicate case IDs')
        cases = [case for case in cases if case['id'] in ids]
    if mode == 'offline' and any(case.get('chat_space') == 'ha' for case in cases):
        raise ValueError('HA blind cases require a real planner; use prepare to review the input scope.')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = prepare_manifest(path, cases)
    write_json(output/'scope.json', manifest)
    report = {'mode': mode, 'version': VERSION, 'suite_sha256': digest(path),
              'status': 'prepared', 'cases': [], 'unexecuted_cases': [case['id'] for case in cases],
              'model_api_calls': 0, 'known_total_tokens': 0, 'unknown_usage_calls': 0,
              'live_device_reads': 0, 'semantic_accuracy': None, 'created_at_utc': utc_now()}
    write_json(output/'run.json', report)
    if mode == 'prepare': return report
    real_model = mode == 'model'
    report['status'] = 'running'
    try:
        if real_model:
            config = (config_loader or qa.model_config)()
            opener = urllib.request.build_opener(chat_model.NoRedirect(), urllib.request.ProxyHandler({}))
            adapter = chat_model.make_model(config, opener=opener)
        else:
            adapter = offline_planner
        with isolated_io(real_model) as state:
            for case in cases:
                print(json.dumps({'phase': 'case_start', 'id': case['id'], 'mode': mode}), flush=True)
                row = run_case(case, manifest['fixed_sources_by_case'][case['id']], adapter, state, real_model)
                write_json(output/(case['id']+'.json'), row)
                report['cases'].append({'id': case['id'], 'status': row['result'].get('status'),
                                        'automatic_checks': row['automatic_checks']})
                report['unexecuted_cases'].remove(case['id'])
                if real_model:
                    report['model_api_calls'] += len(row['adapter_calls'])
                    for call in row['adapter_calls']:
                        if type(call['usage']) is int and call['usage'] >= 0: report['known_total_tokens'] += call['usage']
                        else: report['unknown_usage_calls'] += 1
                write_json(output/'run.json', report)
                print(json.dumps({'phase': 'case_finished', 'id': case['id'], 'status': row['result'].get('status')}), flush=True)
                if real_model and any(call['outcome'] != 'response_received' for call in row['adapter_calls'][-1:]):
                    report['status'] = 'stopped'
                    break
            else:
                report['status'] = 'completed'
    except Exception:
        report.update(status='stopped', error='evaluation_setup_or_io_failed')
    report['cases_with_all_automatic_checks'] = sum(all(case['automatic_checks'].values()) for case in report['cases'])
    report['total_tokens'] = report['known_total_tokens'] if real_model and not report['unknown_usage_calls'] else None
    report['finished_at_utc'] = utc_now()
    write_json(output/'run.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=DEFAULT_SUITE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('prepare', 'offline', 'model'), default='prepare')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.config and args.mode != 'model': parser.error('Only model mode may read config')
    loader = (lambda: json.loads(args.config.read_text(encoding='utf-8'))) if args.config else None
    report = run_suite(args.suite, args.output, args.mode, args.cases, loader)
    print(json.dumps({key: report.get(key) for key in ('mode', 'status', 'model_api_calls', 'live_device_reads',
        'cases_with_all_automatic_checks', 'known_total_tokens', 'unknown_usage_calls')}, ensure_ascii=True))
    return 0 if report['status'] in ('prepared', 'completed') else 1


if __name__ == '__main__':
    sys.exit(main())
