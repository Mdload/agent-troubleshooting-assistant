import io
import json
import os
import socket
import struct
import subprocess
import unittest
import re
from unittest.mock import Mock, patch
from migration import bridge
from devices.device_registry import checks_for


class BridgeTests(unittest.TestCase):
    def test_only_registered_scope(self):
        for request in ({'device':'203.0.113.112','check':'services'},
                        {'device':'remote-backup','check':'reboot'},
                        {'device':'local-primary','check':'services','command':'id'},
                        {'device':True,'check':'services'},
                        {'operation':'health','device':'local-primary'}):
            with patch.object(bridge, 'call_remote') as call:
                self.assertFalse(bridge.dispatch(request)['ok'])
                call.assert_not_called()

    def test_framing_limit_and_json(self):
        for line in (b'{}', b'x'*1025+b'\n', b'not-json\n', b'[]\n'):
            with self.assertRaises(ValueError):
                bridge.decode_line(line)
        self.assertEqual(bridge.decode_line(b'{"operation":"health"}\n'), {'operation':'health'})

    def test_all_registered_checks_are_accepted(self):
        for device in bridge.DEVICES:
            for check in checks_for(device):
                self.assertTrue(bridge.valid_request({'device':device,'check':check}))

    def test_fixed_ssh_and_preserved_observation(self):
        request = {'device':'remote-backup','check':'services'}
        observation = dict(request, ok=True, output='checked',
                           collected_at='2026-10-09T01:00:00Z', clock_source='collector_utc')
        result = Mock(returncode=0, stdout=json.dumps(observation).encode()+b'\n', stderr=b'')
        with patch.object(bridge.subprocess, 'run', return_value=result) as run:
            self.assertEqual(bridge.dispatch(request), observation)
        command = run.call_args[0][0]
        self.assertEqual(command[0], '/usr/bin/ssh')
        self.assertEqual(command[-2], 'ubuntu@203.0.113.140')
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertEqual(json.loads(run.call_args[1]['input']), request)
        self.assertLessEqual(run.call_args[1]['timeout'], 26)

    def test_transport_failure_does_not_expose_stderr(self):
        result = Mock(returncode=255, stdout=b'', stderr=b'PRIVATE_FAKE_TEXT')
        with patch.object(bridge.subprocess, 'run', return_value=result):
            response = bridge.dispatch({'operation':'health'})
        self.assertFalse(response['ok'])
        self.assertNotIn('PRIVATE', str(response))

    def test_timeout_is_a_failed_transport(self):
        with patch.object(bridge.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh',24)):
            self.assertFalse(bridge.dispatch({'operation':'health'})['ok'])

    def test_invalid_remote_response_is_rejected(self):
        for output in (b'{}\n', b'{"ok":1}\n', b'{"ok":true}', b'x'*65537):
            with patch.object(bridge.subprocess, 'run', return_value=Mock(returncode=0,stdout=output)):
                self.assertFalse(bridge.dispatch({'device':'local-primary','check':'services'})['ok'])

    def test_health_requires_identity_and_zero_device_reads(self):
        good = {'ok':True,'kind':'worker_transport','host':'example-jump',
                'user':'ubuntu','device_reads':0}
        for update in ({'host':'wrong'}, {'user':'root'}, {'device_reads':1}, {'kind':'device_read'}):
            data = dict(good, **update)
            with patch.object(bridge.subprocess, 'run', return_value=Mock(
                    returncode=0, stdout=json.dumps(data).encode()+b'\n')):
                self.assertFalse(bridge.dispatch({'operation':'health'})['ok'])

    def handler(self, uid, line):
        handler = bridge.Handler.__new__(bridge.Handler)
        handler.connection = Mock()
        handler.connection.getsockopt.return_value = struct.pack('3i', 1, uid, uid)
        handler.rfile = io.BytesIO(line)
        handler.wfile = io.BytesIO()
        return handler

    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux socket peer credentials')
    def test_another_uid_cannot_dispatch(self):
        handler = self.handler(os.getuid()+1, b'{"operation":"health"}\n')
        with patch.object(bridge, 'dispatch') as dispatch:
            handler.handle()
            dispatch.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b'')

    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux socket peer credentials')
    def test_malformed_request_cannot_dispatch(self):
        handler = self.handler(os.getuid(), b'{"device":"unknown","check":"services"}\n')
        with patch.object(bridge, 'dispatch') as dispatch:
            handler.handle()
            dispatch.assert_not_called()
        self.assertFalse(json.loads(handler.wfile.getvalue())['ok'])

    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux socket peer credentials')
    def test_busy_does_not_dispatch(self):
        handler = self.handler(os.getuid(), b'{"operation":"health"}\n')
        slot = Mock()
        slot.acquire.return_value = False
        with patch.object(bridge, 'SLOT', slot), patch.object(bridge, 'dispatch') as dispatch:
            handler.handle()
            dispatch.assert_not_called()
        self.assertEqual(json.loads(handler.wfile.getvalue())['error'], 'bridge_busy')
        slot.release.assert_not_called()

    def test_remote_program_is_valid_python(self):
        compile(bridge.REMOTE_CODE, '<fixed-140-entry>', 'exec')

    def test_current_page_uses_model_without_extra_opt_in_control(self):
        page = (bridge.ROOT / 'web/index.html').read_text(encoding='utf-8')
        control = re.search(r'<input\b[^>]*\bid=[\"\x27]allow-context[\"\x27][^>]*>', page, re.I)
        self.assertIsNone(control)
        self.assertIn('allow_context:true',page)


if __name__ == '__main__':
    unittest.main()
