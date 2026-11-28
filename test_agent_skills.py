"""Isolated file store, model tool and authenticated HTTP checks."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import agent_skills
from agent_memory import AgentMemoryStore
from agent_v2 import AutonomousAgent
import app
from test_agent_v2 import FAKE_CONFIG
from workflow_test_support import RunningApp


SAMPLE = {'slug': 'example', 'title': 'Test title', 'trigger': 'Test trigger', 'content': 'Test body'}


class AgentSkillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        override = patch.object(agent_skills, 'ROOT', self.root / 'agent_skills')
        override.start()
        self.addCleanup(override.stop)

    def test_write_update_index_and_delete(self):
        self.assertEqual(agent_skills.render_index_summary(), '')
        self.assertEqual(agent_skills.list_skills(), [])
        self.assertEqual(agent_skills.write_skill(**SAMPLE), SAMPLE)
        self.assertEqual(agent_skills.read_skill('example'), SAMPLE)
        self.assertEqual(agent_skills.render_index_summary(), '- example | Test title | Test trigger')
        updated = dict(SAMPLE, title='Updated')
        agent_skills.write_skill(**updated)
        self.assertEqual(agent_skills.list_skills(), [updated])
        self.assertEqual(agent_skills.render_index_summary(), '- example | Updated | Test trigger')
        self.assertTrue(agent_skills.delete_skill('example'))
        self.assertFalse(agent_skills.delete_skill('example'))
        self.assertEqual((agent_skills.ROOT / 'INDEX.md').read_bytes(), b'')

    def test_reject_paths_and_oversized_utf8(self):
        for slug in ('../example', 's/t', r's\t', '/tmp/test', 'a.md', 'A', 'a' * 65, 'a\n'):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    agent_skills.write_skill(**dict(SAMPLE, slug=slug))
                with self.assertRaises(ValueError):
                    agent_skills.read_skill(slug)
                with self.assertRaises(ValueError):
                    agent_skills.delete_skill(slug)
        with self.assertRaises(ValueError):
            agent_skills.write_skill(**dict(SAMPLE, content='字' * 6000))
        self.assertFalse(agent_skills.ROOT.exists())

    def test_reject_linked_file(self):
        agent_skills.write_skill(**SAMPLE)
        outside = self.root / 'outside.md'
        outside.write_text('outside', encoding='utf-8')
        linked = agent_skills.ROOT / 'skills' / 'linked.md'
        try:
            linked.symlink_to(outside)
        except OSError:
            os.link(str(outside), str(linked))
        for invoke in (lambda: agent_skills.read_skill('linked'),
                       lambda: agent_skills.write_skill(**dict(SAMPLE, slug='linked')),
                       lambda: agent_skills.delete_skill('linked')):
            with self.assertRaises(ValueError):
                invoke()
        self.assertEqual(outside.read_text(encoding='utf-8'), 'outside')

    def test_agent_tool_rounds_and_index(self):
        def tool(name, arguments):
            return {'content': '', 'usage': 1, 'tool_calls': [
                {'id': name, 'name': name, 'arguments': arguments},
            ]}
        answer = {'content': '完成', 'usage': 1, 'tool_calls': []}
        turns = iter([tool('write_skill', SAMPLE), answer, tool('read_skill', {'slug': 'example'}), answer])
        seen = []
        def llm(config, messages, **kwargs):
            seen.append(list(messages))
            self.assertTrue({'read_skill', 'write_skill'}.issubset(
                schema['function']['name'] for schema in kwargs['tools']))
            return next(turns)
        agent = AutonomousAgent(AgentMemoryStore(self.root / 'memory.db'),
                                config_fn=lambda: FAKE_CONFIG, llm_client=llm, background_learning=False)
        agent.chat('owner', 'first', '沉淀', skills_enabled=True)
        self.assertEqual(agent_skills.list_skills(), [SAMPLE])
        agent.chat('owner', 'second', '排障', skills_enabled=True)
        self.assertIn('- example | Test title | Test trigger', seen[2][0]['content'])
        self.assertEqual(json.loads(seen[3][-1]['content'])['skill'], SAMPLE)

    def test_http_auth_csrf_and_gateway_compatibility(self):
        with RunningApp() as server:
            def delete(slug, *, token=True):
                headers = {'Origin': server.base}
                if token:
                    headers['X-ATA-Token'] = app.CSRF_TOKEN
                req = Request(server.base + '/api/skills/' + slug, method='DELETE', headers=headers)
                try:
                    with server.opener.open(req, timeout=5) as response:
                        return response.status
                except HTTPError as error:
                    return error.code

            agent_skills.write_skill(**SAMPLE)
            legacy_path = '/api/conversations/default/memory'
            self.assertEqual(server.call('/api/skills')[0], 401)
            self.assertEqual(server.call(legacy_path, {'action': 'list_skills'})[0], 401)
            self.assertEqual(delete('example'), 401)
            code, _ = server.call('/api/account/register', {
                'username': 'skill-user', 'password': 'test-only-password', 'allow_storage': True,
            })
            self.assertEqual(code, 200)
            code, library = server.call('/api/skills')
            self.assertEqual(code, 200)
            self.assertEqual(library['skills'], [SAMPLE])
            self.assertEqual(server.call(legacy_path, {'action': 'list_skills'}), (200, library))
            self.assertEqual(delete('example', token=False), 403)
            self.assertEqual(delete('%2e%2e'), 400)
            self.assertEqual(delete('example'), 200)
            self.assertEqual(delete('example'), 404)
            agent_skills.write_skill(**SAMPLE)
            self.assertEqual(server.call(legacy_path, {'action': 'delete_skill', 'slug': 'example'})[0], 200)
            self.assertEqual(server.call('/api/skills')[1], {'skills': [], 'summary': ''})
