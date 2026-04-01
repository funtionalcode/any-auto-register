import tempfile
import unittest
from pathlib import Path

from services import external_apps


class ExternalAppsConfigTests(unittest.TestCase):
    def test_clean_log_text_strips_ansi_escape_sequences(self):
        raw = "\x1b[32m2026-03-31 19:01:22\x1b[0m | \x1b[1mINFO\x1b[0m | [36mscheduler.py:50[0m - ok"
        cleaned = external_apps._clean_log_text(raw)

        self.assertEqual(cleaned, "2026-03-31 19:01:22 | INFO | scheduler.py:50 - ok")

    def test_service_meta_uses_external_host_but_keeps_local_health_probe(self):
        old_get_setting = external_apps._get_setting
        external_apps._get_setting = lambda key, default="": {
            "external_apps_host": "172.16.0.180",
            "external_apps_scheme": "http",
        }.get(key, default)
        try:
            meta = external_apps._service_meta("cliproxyapi")
        finally:
            external_apps._get_setting = old_get_setting

        self.assertEqual(meta["url"], "http://172.16.0.180:8317")
        self.assertEqual(meta["management_url"], "http://172.16.0.180:8317/management.html")
        self.assertEqual(meta["health"], "http://127.0.0.1:8317/")

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
        self.assertRegex(updated, r'  secret-key: "?new-secret"?')
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
        self.assertRegex(updated, r'  secret-key: "?new-secret"?')
        self.assertIn("  disable-control-panel: false", updated)

    def test_managed_service_defaults_to_installed_and_started(self):
        store = {}
        old_get_setting = external_apps._get_setting
        old_set_setting = external_apps._set_setting

        external_apps._get_setting = lambda key, default="": store.get(key, default)
        external_apps._set_setting = lambda key, value: store.__setitem__(key, str(value))
        try:
            state = external_apps._load_service_state("cliproxyapi")
        finally:
            external_apps._get_setting = old_get_setting
            external_apps._set_setting = old_set_setting

        self.assertTrue(state["installed"])
        self.assertTrue(state["started"])
        self.assertTrue(state["managed"])
        self.assertEqual(store["external_apps_cliproxyapi_installed"], "true")
        self.assertEqual(store["external_apps_cliproxyapi_started"], "true")

    def test_ensure_managed_services_ready_installs_and_starts_missing_service(self):
        store = {}
        running = set()
        calls = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)

            old_get_setting = external_apps._get_setting
            old_set_setting = external_apps._set_setting
            old_repo_path = external_apps._repo_path
            old_status_one = external_apps._status_one
            old_install = external_apps.install
            old_start = external_apps.start

            external_apps._get_setting = lambda key, default="": store.get(key, default)
            external_apps._set_setting = lambda key, value: store.__setitem__(key, str(value))
            external_apps._repo_path = lambda name: base_dir / name

            def fake_status_one(name: str):
                repo = base_dir / name
                desired = external_apps._load_service_state(name)
                return {
                    "name": name,
                    "label": name,
                    "repo_path": str(repo),
                    "repo_exists": repo.exists(),
                    "url": "",
                    "management_url": "",
                    "management_key": "",
                    "desired_installed": desired["installed"],
                    "desired_running": desired["started"],
                    "managed": desired["managed"],
                    "running": name in running,
                    "starting": False,
                    "process_alive": name in running,
                    "pid": None,
                    "log_path": "",
                    "last_error": external_apps._LAST_ERROR.get(name, ""),
                    "kind": "web",
                }

            def fake_install(name: str, *, persist_state: bool = True):
                calls.append(("install", name, persist_state))
                repo = base_dir / name
                repo.mkdir(parents=True, exist_ok=True)
                if persist_state:
                    external_apps._persist_service_state(name, installed=True)
                return fake_status_one(name)

            def fake_start(name: str, *, persist_state: bool = True):
                calls.append(("start", name, persist_state))
                running.add(name)
                if persist_state:
                    external_apps._persist_service_state(name, installed=True, started=True)
                return fake_status_one(name)

            external_apps._status_one = fake_status_one
            external_apps.install = fake_install
            external_apps.start = fake_start

            try:
                results = external_apps.ensure_managed_services_ready(["cliproxyapi"])
            finally:
                external_apps._get_setting = old_get_setting
                external_apps._set_setting = old_set_setting
                external_apps._repo_path = old_repo_path
                external_apps._status_one = old_status_one
                external_apps.install = old_install
                external_apps.start = old_start

        self.assertEqual(
            calls,
            [("install", "cliproxyapi", False), ("start", "cliproxyapi", False)],
        )
        self.assertTrue(results[0]["repo_exists"])
        self.assertTrue(results[0]["running"])
        self.assertEqual(store["external_apps_cliproxyapi_installed"], "true")
        self.assertEqual(store["external_apps_cliproxyapi_started"], "true")


if __name__ == "__main__":
    unittest.main()
