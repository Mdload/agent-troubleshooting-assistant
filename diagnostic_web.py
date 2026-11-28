"""网页只负责规划，通过固定socket调用执行进程，不执行SSH或sudo。"""
import json
import socket
import threading
from pathlib import Path
from diagnostic_core import AuditStore, run_diagnosis
from diagnostic_model import make_planner
from device_registry import MAX_PARALLEL_READS, allows_probe

ROOT = Path(__file__).resolve().parent
SLOT = threading.BoundedSemaphore(1)
_CLIENT_SLOT = threading.BoundedSemaphore(MAX_PARALLEL_READS)

def remote_probe(device, check):
    return _request_worker({'device': device, 'check': check})


def remote_probe_device(device, kind, target):
    if not allows_probe(device, kind, target):
        raise ValueError('Invalid probe scope')
    return _request_worker({'device': device, 'kind': kind, 'target': target})


def _request_worker(request):
    with _CLIENT_SLOT, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(25)
        conn.connect('/run/ata-diagnostics/worker.sock')
        conn.sendall(json.dumps(request).encode() + b'\n')
        with conn.makefile('rb') as stream:
            line = stream.readline(65537)
        if len(line) > 65536 or not line.endswith(b'\n'):
            raise ValueError('Invalid worker response')
        return json.loads(line.decode('utf-8'))

def diagnose(question, devices, consent):
    if consent is not True:
        return {'status': 'consent_required', 'answer': '请先勾选允许本次问题和现场状态发送给模型。', 'evidence': []}
    if not SLOT.acquire(False):
        return {'status': 'busy', 'answer': '已有诊断正在运行，请稍后重试。', 'evidence': []}
    try:
        config = json.loads((ROOT/'.secrets/model.json').read_text(encoding='utf-8'))
        planner = make_planner(config, allow_live_data=True)
        return run_diagnosis(question, planner, remote_probe,
                             AuditStore(ROOT/'data/diagnostics.sqlite3'), devices, max_checks=4)
    except Exception:
        return {'status': 'incomplete', 'answer': '诊断暂不可用，请检查执行服务或模型配置。', 'evidence': []}
    finally:
        SLOT.release()
