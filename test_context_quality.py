"""Acceptance preparation correctness, not real-model answer quality."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import chat_model
from evaluation import context_quality as evaluation


class ContextQualityTests(unittest.TestCase):
    def test_all_six_use_actual_http_with_bounded_materials_and_source_bound_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'prepared'
            with patch.object(evaluation.qa, 'model_config', side_effect=AssertionError('no config')):
                result = evaluation.prepare(output)
            self.assertEqual(result['cases'], 6)
            self.assertEqual(result['model_api_calls'], 0)
            rows = json.loads((output/'offline-preparation.json').read_text(encoding='utf-8'))['cases']
            self.assertTrue(all(all(row['automatic_checks'].values()) for row in rows))
            self.assertTrue(all(row['status'] == 'answered' for row in rows))
            first = rows[0]['captured'][0]['view']
            self.assertIn('retry=4', str(first['sources']))
            third = rows[2]['captured'][0]['view']
            self.assertIn('retry=7', str(third['sources']))
            self.assertIn(':C', str(third['sources']))
            scope = (output/'scope.json').read_text(encoding='utf-8')
            self.assertNotIn('JUDGE_ONLY', scope)
            self.assertNotIn('offline-context-only-123', scope)
            self.assertNotIn('Authorization', scope)
            self.assertIsNone(json.loads((output/'offline-preparation.json').read_text(encoding='utf-8'))['semantic_accuracy'])
            with self.assertRaises(FileExistsError): evaluation.prepare(output)

    def test_wrong_approval_hash_never_reads_config_or_creates_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); prepared = root/'prepared'; prepared.mkdir()
            (prepared/'scope.json').write_text('{}')
            with patch.object(evaluation.qa, 'model_config', side_effect=AssertionError('no config')):
                with self.assertRaisesRegex(ValueError, 'scope_mismatch'):
                    evaluation.model_run(prepared, root/'model', 'incorrect', None)
            self.assertFalse((prepared/'CLAIMED.json').exists())
            self.assertFalse((root/'model').exists())

    def test_model_permission_is_enabled_only_while_adapter_executes(self):
        state = Mock(); state.model_call = False
        def adapter(context):
            self.assertTrue(state.model_call)
            return evaluation.offline_adapter(context)
        row = evaluation.run_case(evaluation.cases()[0], adapter, True, state)
        self.assertFalse(state.model_call)
        self.assertEqual(len(row['adapter_calls']), 1)
        self.assertEqual(row['adapter_calls'][0]['decoded_action']['action'], 'answer')
        self.assertNotIn('reasoning', row['adapter_calls'][0]['decoded_action'])
        self.assertTrue(all(row['automatic_checks'].values()))

    def test_failure_keeps_unknown_usage_and_drops_transport_permission(self):
        state = Mock(); state.model_call = False
        def adapter(context):
            adapter.last_metrics = {'transport_attempted': True, 'stage': 'awaiting_headers'}
            raise chat_model.ModelFailure('model_timeout')
        row = evaluation.run_case(evaluation.cases()[0], adapter, True, state)
        self.assertFalse(state.model_call)
        self.assertEqual(row['status'], 'incomplete')
        self.assertIsNone(row['adapter_calls'][0]['usage'])
        self.assertEqual(row['adapter_calls'][0]['outcome'], 'model_timeout')
        self.assertEqual(len(row['adapter_calls']), 1)


if __name__ == '__main__': unittest.main()
