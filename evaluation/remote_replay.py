"""用固定合成观测走真实宿主循环；规划器是测试替身，不调用模型/SSH。"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from devices.device_tools import build_command
from devices.diagnostic_core import AuditStore
from legacy.unified_chat import run, search


def load_cases():
    return json.loads((ROOT / 'evaluation/remote-fixtures.json').read_text(encoding='utf-8'))['cases']


def unit_state(output, unit):
    for section in output.split('\n\n'):
        fields = dict(line.split('=', 1) for line in section.splitlines() if '=' in line)
        if fields.get('Id') == unit:
            return fields.get('ActiveState', 'unknown')
    return 'unknown'


def fixture_planner(device):
    """明确的确定性测试替身；只用于检查观测驱动分支及引用/预算接口。"""
    def plan(context):
        observations = context['observations']
        if not observations:
            return {'action': 'read', 'device': device, 'check': 'services'}, 0
        if any(item.get('ok') is not True for item in observations):
            return {'action': 'answer', 'answer': '本次读取超时，未核实现场状态。连接失败不能证明设备或业务故障。',
                    'refs': [], 'basis': 'limited'}, 0
        if len(observations) == 1:
            return {'action': 'read', 'device': device, 'check': 'interfaces'}, 0
        service = unit_state(observations[0]['output'], 'example-croc-smr.service')
        owner = '198.51.100.29/24' in observations[1]['output']
        refs = [item['id'] for item in observations]
        if owner and service == 'inactive':
            text = ('观测显示本机持有业务VIP而Croc服务未运行，存在角色与服务状态不一致，'
                    '可作为业务路径异常线索。尚未检查进程、失败日志及对端，不能确定停止原因或全部业务影响。')
            return {'action': 'answer', 'answer': text, 'refs': refs, 'basis': 'evidence'}, 0
        if not owner and service == 'inactive':
            text = ('本机未见业务VIP，Croc未运行与备机分工相符，不能仅此判故障。'
                    'PPP有地址只说明观测到地址，接管条件、对端和端到端业务仍未验收。')
            return {'action': 'answer', 'answer': text, 'refs': refs, 'basis': 'evidence'}, 0
        if owner and service == 'active' and len(observations) == 2:
            return {'action': 'read', 'device': device, 'check': 'routes'}, 0
        return {'action': 'answer', 'answer': '已有服务、接口与主表路由观测；这些信息不能证明端到端业务可用。'
                '仍需局端接收、必要Router、策略表及实际业务证据，目前不能确认根因。',
                'refs': refs, 'basis': 'limited'}, 0
    return plan


def replay(case):
    calls = []
    def fixture_probe(device, check):
        build_command(device, check)  # 复用宿主枚举校验，不执行生成的命令。
        if device != case['device'] or check not in case['observations']:
            raise ValueError('Fixture scope mismatch')
        calls.append(check)
        result = dict(case['observations'][check])
        result.update(collected_at='SIMULATION', clock_source='synthetic')
        result['output'] = '[合成测试快照，不是真实设备状态]\n' + result.get('output', '')
        return result
    with tempfile.TemporaryDirectory() as temp:
        audit = AuditStore(Path(temp) / 'audit.sqlite3')
        result = run(case['question'], [], fixture_planner(case['device']), search, fixture_probe, audit)
        trajectory = audit.read(result['task'])
    assert calls == case['expected_checks'], (case['id'], calls)
    assert result['status'] == 'answered'
    assert result['basis'] == case['expected_basis']
    valid = {item['id'] for item in result['evidence'] if item.get('ok') is True}
    assert set(result['refs']) <= valid
    assert result['evidence_scope']['kind'] == ('synthetic' if valid else 'failed')
    return {'case': case['id'], 'title': case['title'], 'question': case['question'],
            'planner_kind': 'deterministic_fixture_planner', 'not_model_evaluation': True,
            'live_device_reads': 0, 'model_calls': 0, 'checks': calls,
            'engineering_assertions_passed': True, 'result': result, 'audit': trajectory}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'evaluation/results/remote-replay-20261001.json'))
    args = parser.parse_args()
    rows = [replay(case) for case in load_cases()]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'scope': 'synthetic_engineering_replay',
        'not_model_accuracy': True, 'model_calls': 0, 'live_device_reads': 0, 'cases': rows},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'replayed': len(rows), 'engineering_assertions_passed': len(rows),
        'model_calls': 0, 'live_device_reads': 0, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
