import unittest
from devices.diagnostic_model import make_planner

class ModelPermissionTests(unittest.TestCase):
    def test_explicit_consent_required_before_config_use(self):
        for consent in (False,None,1,'true'):
            with self.assertRaises(PermissionError): make_planner({},consent)

    def test_destination_is_fixed(self):
        with self.assertRaises(ValueError):
            make_planner({'LLM_BASE_URL':'https://elsewhere.invalid/v1'},True)
