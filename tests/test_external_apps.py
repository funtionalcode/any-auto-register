import tempfile
import unittest
from pathlib import Path

from services import external_apps


class ExternalAppsConfigTests(unittest.TestCase):
    def test_cliproxyapi_remote_management_comments_do_not_create_duplicate_section(self):
        config_text = """# Server port
port: 8317

remote-management:
  # Whether to allow remote (non-localhost) management access.
  # When false, only localhost can access management endpoints (a key is still required).
  allow-remote: false

  # Management key. If a plaintext value is provided here, it will be hashed on startup.
  secret-key: ""

  disable-control-panel: false
"""

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            (repo / "config.local.yaml").write_text(config_text, encoding="utf-8")

            old_get_setting = external_apps._get_setting
            external_apps._get_setting = lambda key, default="": "new-secret"
            try:
                external_apps._ensure_cliproxyapi_runtime_config(repo)
            finally:
                external_apps._get_setting = old_get_setting

            updated = (repo / "config.local.yaml").read_text(encoding="utf-8")

        self.assertEqual(updated.count("remote-management:"), 1)
        self.assertIn("  allow-remote: true", updated)
        self.assertNotIn("  allow-remote: false", updated)
        self.assertIn('  secret-key: "new-secret"', updated)
        self.assertIn("  disable-control-panel: false", updated)

    def test_cliproxyapi_remote_management_deduplicates_broken_config(self):
        config_text = """port: 8317

remote-management:
  # Whether to allow remote (non-localhost) management access.
  allow-remote: false
  secret-key: ""
  disable-control-panel: false

remote-management:
  allow-remote: true
"""

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            (repo / "config.local.yaml").write_text(config_text, encoding="utf-8")

            old_get_setting = external_apps._get_setting
            external_apps._get_setting = lambda key, default="": "new-secret"
            try:
                external_apps._ensure_cliproxyapi_runtime_config(repo)
            finally:
                external_apps._get_setting = old_get_setting

            updated = (repo / "config.local.yaml").read_text(encoding="utf-8")

        self.assertEqual(updated.count("remote-management:"), 1)
        self.assertEqual(updated.count("allow-remote: true"), 1)
        self.assertIn('  secret-key: "new-secret"', updated)
        self.assertIn("  disable-control-panel: false", updated)


if __name__ == "__main__":
    unittest.main()
