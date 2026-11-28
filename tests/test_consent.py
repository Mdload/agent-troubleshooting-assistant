if not __package__:
    import _bootstrap

import unittest
from unittest.mock import patch
import qa

class ConsentTests(unittest.TestCase):
    def test_default_and_non_boolean_never_call_model(self):
        for value in (False, None, 'true', 1):
            with patch('qa.ask_model') as model:
                result = qa.answer('局端触发切换的规则是什么', allow_new_topology=value)
            self.assertEqual(result['kind'], 'consent_required')
            model.assert_not_called()

    def test_explicit_user_consent_allows_call(self):
        with patch('qa.ask_model', return_value=('测试回答', 10, [])) as model:
            result = qa.answer('局端触发切换的规则是什么', allow_new_topology=True)
        self.assertEqual(result['kind'], 'model')
        model.assert_called_once()

    def test_consent_cannot_change_destination(self):
        cfg = dict(LLM_API_KEY='test-only', LLM_MODEL_ID='test', LLM_BASE_URL='https://example.invalid/v1')
        with patch('qa.model_config', return_value=cfg), patch('qa.urllib.request.urlopen') as request:
            result = qa.answer('局端触发切换的规则是什么', allow_new_topology=True)
        self.assertEqual(result['kind'], 'extract')
        request.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
