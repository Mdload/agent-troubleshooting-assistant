"""使用桥接取证传输的 Web 入口。"""
import devices.diagnostic_web as diagnostic_web
from migration.bridge import remote_probe, remote_probe_device, remote_approved_repair

diagnostic_web.remote_probe = remote_probe
diagnostic_web.remote_probe_device = remote_probe_device
import app
from migration.repair_bridge import execute_repair,inspect_repair

if __name__ == '__main__':
    print('ATA migration trial listening on 127.0.0.1:17865', flush=True)
    server=app.Server(('127.0.0.1',17865),app.Handler)
    server.repair_executor=execute_repair
    server.repair_inspector=inspect_repair
    server.approved_repair=remote_approved_repair
    server.get_repair_proposals().recover_interrupted()
    server.serve_forever()
