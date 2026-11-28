import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from agent.agent_memory import AgentMemoryStore
from agent.agent_v2 import AutonomousAgent


DEVICES = ('local-primary', 'local-backup', 'remote-primary')
CALLS = [{'id': str(i), 'name': 'inspect_device',
          'arguments': {'device': device, 'check': 'services'}}
         for i, device in enumerate(DEVICES)]
CONFIG = {'LLM_API_KEY': 'offline-test', 'LLM_MODEL_ID': 'mock',
          'LLM_BASE_URL': 'https://example.invalid'}


class ParallelInspectTests(unittest.TestCase):
    def make_agent(self, probe, calls=CALLS):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = AgentMemoryStore(Path(tmp.name) / 'agent.sqlite3')
        seen = []

        def model(config, messages, **kwargs):
            seen[:] = [(message['tool_call_id'], json.loads(message['content']))
                       for message in messages if message['role'] == 'tool']
            if not seen:
                return {'content': '', 'usage': 1, 'tool_calls': calls}
            return {'content': '已完成只读巡检。', 'usage': 1}

        return AutonomousAgent(memory, probe_fn=probe, config_fn=lambda: CONFIG,
                               llm_client=model, background_learning=False), seen

    def test_batch_faster_than_serial_and_keeps_order_after_failure(self):
        def probe(device, check):
            time.sleep({'local-primary': 0.23, 'local-backup': 0.13, 'remote-primary': 0.15}[device])
            if device == 'local-backup':
                raise OSError('simulated failure')
            return {'ok': True, 'output': device}

        list_call = {'id': 'list', 'name': 'list_devices', 'arguments': {}}
        agent, seen = self.make_agent(probe, CALLS[:1] + [list_call] + CALLS[1:])
        agent.memory.grant_devices('owner', DEVICES)
        start = time.monotonic()
        for call in CALLS:
            agent._inspect_call(call)
        serial = time.monotonic() - start
        start = time.monotonic()
        result = agent.chat('owner', 'thread', 'Run device checks')
        parallel = time.monotonic() - start
        print('\nInspect timing: serial {:.0f} ms, parallel {:.0f} ms'.format(serial * 1000, parallel * 1000))
        self.assertLess(parallel, serial * 0.8)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual([step['device'] for step in result['agent_steps'] if step['tool'] == 'inspect_device'], list(DEVICES))
        self.assertEqual([step['ok'] for step in result['agent_steps']], [True, True, False, True])
        self.assertEqual([ident for ident, _ in seen], ['0', 'list', '1', '2'])
        self.assertEqual(seen[2][1]['error'], 'probe_transport_error')

    def test_mixed_authorization_pauses_entire_batch(self):
        probes = []
        agent, _ = self.make_agent(lambda device, check: probes.append(device))
        agent.memory.grant_devices('owner', DEVICES[:1])
        result = agent.chat('owner', 'thread', 'Run device checks')
        self.assertEqual(result['status'], 'device_permission_required')
        self.assertEqual([row['id'] for row in result['requested_devices']], list(DEVICES[1:]))
        self.assertEqual(probes, [])
