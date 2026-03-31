import unittest

from services import cpa_target


class CpaTargetTests(unittest.TestCase):
    def test_resolve_cpa_api_key_prefers_explicit_value(self):
        old_get_config_value = cpa_target._get_config_value
        cpa_target._get_config_value = lambda key, default="": {
            "cpa_api_key": "configured-cpa-key",
            "cliproxyapi_management_key": "clip-key",
        }.get(key, default)
        try:
            resolved = cpa_target.resolve_cpa_api_key("explicit-key", api_url="http://127.0.0.1:8317")
        finally:
            cpa_target._get_config_value = old_get_config_value

        self.assertEqual(resolved, "explicit-key")

    def test_resolve_cpa_api_key_falls_back_to_cliproxyapi_management_key(self):
        old_get_config_value = cpa_target._get_config_value
        cpa_target._get_config_value = lambda key, default="": {
            "cpa_api_key": "",
            "cliproxyapi_management_key": "clip-key",
        }.get(key, default)
        try:
            resolved = cpa_target.resolve_cpa_api_key("", api_url="http://127.0.0.1:8317")
        finally:
            cpa_target._get_config_value = old_get_config_value

        self.assertEqual(resolved, "clip-key")

    def test_resolve_cpa_api_key_uses_default_local_cliproxyapi_key_only_for_local_target(self):
        old_get_config_value = cpa_target._get_config_value
        old_cliproxyapi_base_url = cpa_target._cliproxyapi_base_url
        cpa_target._get_config_value = lambda key, default="": ""
        cpa_target._cliproxyapi_base_url = lambda: "http://172.16.0.180:8317"
        try:
            local_resolved = cpa_target.resolve_cpa_api_key("", api_url="http://127.0.0.1:8317")
            remote_resolved = cpa_target.resolve_cpa_api_key("", api_url="https://example.com")
        finally:
            cpa_target._get_config_value = old_get_config_value
            cpa_target._cliproxyapi_base_url = old_cliproxyapi_base_url

        self.assertEqual(local_resolved, "cliproxyapi")
        self.assertEqual(remote_resolved, "")


if __name__ == "__main__":
    unittest.main()
