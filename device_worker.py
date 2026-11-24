"""独立只读执行进程：Unix socket仅接受同一服务用户及固定枚举。"""
import json
import os
import socket
import socketserver
import struct
from device_tools import probe, DEVICES, CHECKS
from device_registry import allows_check, checks_for

SOCKET = '/run/ata-diagnostics/worker.sock'

def dispatch(data):
    if data == {'operation':'capabilities'}:
        return {'ok':True,'kind':'registered_checks','devices':{device:list(checks_for(device)) for device in DEVICES},
                'device_reads':0}
    if not isinstance(data, dict) or set(data) != {'device', 'check'}:
        raise ValueError('Invalid request')
    if (not isinstance(data['device'], str) or not isinstance(data['check'], str) or
        not allows_check(data['device'],data['check'])):
        raise ValueError('Invalid scope')
    return probe(data['device'], data['check'])

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            line = self.rfile.readline(1025)
            if len(line) > 1024 or not line.endswith(b'\n'):
                return
            result = dispatch(json.loads(line.decode('utf-8')))
        except Exception:
            result = {'ok': False, 'output': '', 'error': 'worker_request_failed'}
        try:
            self.wfile.write(json.dumps(result).encode('utf-8') + b'\n')
        except OSError:
            pass

if __name__ == '__main__':
    os.umask(0o077)
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    with socketserver.UnixStreamServer(SOCKET, Handler) as server:
        server.serve_forever()
