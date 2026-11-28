"""同用户本机socket -> 固定SSH -> 登记的只读 worker；健康检查不读设备。"""
import json
import os
from pathlib import Path
import shlex
import socket
import socketserver
import stat
import struct
import subprocess
import threading

from device_tools import DEVICES, CHECKS
from device_registry import HOPS, MAX_PARALLEL_READS, allows_check, allows_probe

ROOT = Path(__file__).resolve().parents[1]
SOCKET = ROOT / 'data/migration-worker.sock'
SLOT = threading.BoundedSemaphore(MAX_PARALLEL_READS)
_CLIENT_SLOT = threading.BoundedSemaphore(MAX_PARALLEL_READS)
from migration.protocol import REMOTE_CODE


def valid_request(request):
    if not isinstance(request, dict):
        return False
    if request in ({'operation': 'health'}, {'operation': 'capabilities'}):
        return True
    if set(request) == {'device', 'kind', 'target'}:
        return allows_probe(**request)
    return set(request) == {'device', 'check'} and allows_check(**request)


def decode_line(line):
    if len(line) > 1024 or not line.endswith(b'\n'):
        raise ValueError('invalid_frame')
    request = json.loads(line.decode('utf-8'))
    if not valid_request(request):
        raise ValueError('invalid_scope')
    return request


def call_remote(request):
    command = ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes',
               '-o', 'ConnectTimeout=6', '-o', 'ConnectionAttempts=1',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
               '-o', 'ForwardAgent=no', '-o', 'IdentitiesOnly=yes',
               HOPS['jump_host']['user']+'@'+HOPS['jump_host']['address'],
               '/usr/bin/python3 -I -B -c ' + shlex.quote(REMOTE_CODE)]
    result = subprocess.run(command, input=json.dumps(request).encode() + b'\n',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=24)
    if result.returncode != 0:
        raise ValueError('transport_failed')
    if len(result.stdout) > 65536 or not result.stdout.endswith(b'\n'):
        raise ValueError('invalid_response')
    response = json.loads(result.stdout.decode('utf-8'))
    if not isinstance(response, dict) or type(response.get('ok')) is not bool:
        raise ValueError('invalid_response')
    if request == {'operation': 'health'}:
        if (response.get('kind') != 'worker_transport' or response.get('device_reads') != 0 or
                response.get('host') != HOPS['jump_host']['hostname'] or
                response.get('user') != HOPS['jump_host']['user']):
            raise ValueError('identity_unconfirmed')
    return response


def dispatch(request):
    if not valid_request(request):
        return {'ok': False, 'output': '', 'error': 'bridge_invalid_scope'}
    try:
        return call_remote(request)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return {'ok': False, 'output': '', 'error': 'bridge_transport_failed'}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        acquired = False
        try:
            self.connection.settimeout(3)
            _, uid, _ = struct.unpack('3i', self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            request = decode_line(self.rfile.readline(1025))
            acquired = SLOT.acquire(False)
            result = dispatch(request) if acquired else {
                'ok': False, 'output': '', 'error': 'bridge_busy'}
        except (OSError, ValueError, UnicodeError):
            result = {'ok': False, 'output': '', 'error': 'bridge_invalid_request'}
        finally:
            if acquired:
                SLOT.release()
        try:
            self.wfile.write(json.dumps(result).encode() + b'\n')
        except OSError:
            pass


def request_worker(request):
    if not valid_request(request):
        raise ValueError('invalid_scope')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(26)
        connection.connect(str(SOCKET))
        connection.sendall(json.dumps(request).encode() + b'\n')
        with connection.makefile('rb') as stream:
            response = stream.readline(65537)
    if len(response) > 65536 or not response.endswith(b'\n'):
        raise ValueError('invalid_frame')
    result = json.loads(response.decode('utf-8'))
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise ValueError('invalid_response')
    return result


def remote_probe(device, check):
    with _CLIENT_SLOT:
        return request_worker({'device': device, 'check': check})


def remote_probe_device(device, kind, target):
    with _CLIENT_SLOT:
        return request_worker({'device': device, 'kind': kind, 'target': target})


def main():
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    os.umask(0o077)
    SOCKET.parent.mkdir(mode=0o700, exist_ok=True)
    if SOCKET.exists():
        info = SOCKET.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('Unexpected socket owner or type')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            try:
                connection.connect(str(SOCKET))
            except ConnectionRefusedError:
                SOCKET.unlink()
            else:
                raise RuntimeError('Bridge already running')
    with Server(str(SOCKET), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
