from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import retrieval.qa as qa
from retrieval.hybrid_retrieval import CharacterVectors, VectorCache, preferred_backend, reciprocal_rank_fusion


class CountingVectors(CharacterVectors):
    def __init__(self):
        self.encoded = []

    def encode(self, text):
        self.encoded.append(text)
        return super().encode(text)


class HybridRetrievalTests(unittest.TestCase):
    def test_cache_reuse_incremental_update_and_version_invalidation(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'vectors.sqlite3'
            encoder = CountingVectors()
            old_docs = {'a': '业务VIP', 'b': '回程'}
            old = VectorCache(path).vectors(old_docs, encoder)
            self.assertEqual(encoder.encoded, ['业务VIP', '回程'])
            encoder.encoded.clear()
            self.assertEqual(old, VectorCache(path).vectors(old_docs, encoder))
            self.assertEqual(encoder.encoded, [])
            new_docs = {'a': '业务VIP', 'b': '回包', 'c': '探活'}
            self.assertEqual(len(VectorCache(path).vectors(new_docs, encoder)), 3)
            self.assertEqual(encoder.encoded, ['回包', '探活'])
            encoder.encoded.clear()
            encoder.version = 'test-new-version'
            VectorCache(path).vectors(new_docs, encoder)
            self.assertEqual(encoder.encoded, list(new_docs.values()))

    def test_rrf_rewards_agreement_and_preserves_weighted_exact_match(self):
        both = reciprocal_rank_fusion(['lexical', 'shared'], ['semantic', 'shared'])
        self.assertEqual([key for key, _ in both], ['shared', 'lexical', 'semantic'])
        lexical = ['exact'] + [str(i) for i in range(14)] + ['shared']
        exact = reciprocal_rank_fusion(lexical, ['shared'], lexical_weight=4)
        self.assertEqual(exact[0][0], 'exact')

    def test_metadata_applies_before_vector_ranking_and_extras_are_not_cached(self):
        base = {'kind': 'reviewed_experience', 'operating_modes': [],
                'title': 'Zeta_probe-agent.service 虚拟地址漂移'}
        extra = dict(base, id='EXP-mounted', device_groups=['local'])
        blocked = dict(base, id='EXP-blocked', device_groups=['remote'])
        with TemporaryDirectory() as tmp:
            cache = VectorCache(Path(tmp) / 'vectors.sqlite3')
            with patch.object(qa, 'VECTOR_CACHE', cache), patch.object(cache, 'vectors', wraps=cache.vectors) as store:
                args = ('Zeta_probe-agent.service',)
                hits = qa.retrieve(*args, group='local', extra_records=[extra, blocked])
                ids = [hit['case']['id'] for hit in hits]
                self.assertIn(extra['id'], ids)
                self.assertNotIn(blocked['id'], ids)
                self.assertNotIn(extra['id'], store.call_args[0][0])

    def test_missing_onnx_directory_falls_back(self):
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ATA_RAG_ONNX_DIR': tmp + '/missing'}):
            self.assertIsInstance(preferred_backend(), CharacterVectors)
