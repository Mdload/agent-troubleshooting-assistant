"""独立只读执行进程：Unix socket仅接受同一服务用户及固定枚举。"""
import json
import os
import socket
import socketserver
import struct
from device_tools import probe, DEVICES, CHECKS

SOCKET = '/run/ata-example/worker.sock'

def dispatch(data):
    if not isinstance(data, dict) or set(data) != {'device', 'check'}:
        raise ValueError('Invalid request')
    if (not isinstance(data['device'], str) or not isinstance(data['check'], str) or
        data['device'] not in DEVICES or data['check'] not in CHECKS):
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
