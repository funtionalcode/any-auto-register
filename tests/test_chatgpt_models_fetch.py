import importlib
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace


class _PlaceholderCurlSession:
    def __init__(self, *args, **kwargs):
        self.headers = {}
        self.cookies = SimpleNamespace(set=lambda *a, **k: None, jar=[])
        self.proxies = {}

    def close(self):
        pass


def _load_message_tester():
    try:
        needs_fake = importlib.util.find_spec("curl_cffi") is None
    except ValueError:
        needs_fake = True

    if needs_fake:
        fake_requests = types.SimpleNamespace(Session=_PlaceholderCurlSession)
        fake_curl_cffi = types.ModuleType("curl_cffi")
        fake_curl_cffi.requests = fake_requests

        for module_name in (
            "platforms.chatgpt.message_tester",
            "platforms.chatgpt.chatgpt_client",
            "platforms.chatgpt.token_refresh",
        ):
            sys.modules.pop(module_name, None)

        sys.modules["curl_cffi"] = fake_curl_cffi
    return importlib.import_module("platforms.chatgpt.message_tester")


message_tester = _load_message_tester()


class _FakeModelsResponse:
    def __init__(self):
        self.status_code = 200
        self.closed = False
        self.headers = {}
        self.text = '{"models":[{"slug":"gpt-4o"},{"slug":"gpt-4.1"}]}'

    def json(self):
        return {
            "models": [
                {"slug": "gpt-4o", "title": "GPT-4o", "description": "omni"},
                {"slug": "gpt-4.1", "title": "GPT-4.1"},
            ]
        }

    def close(self):
        self.closed = True


class _FakeModelsSession:
    def __init__(self):
        self.response = _FakeModelsResponse()
        self.closed = False

    def get(self, url, headers=None, timeout=None):
        return self.response

    def close(self):
        self.closed = True


class _FakeClient:
    last_instance = None

    def __init__(self, proxy=None, verbose=True):
        self.proxy = proxy
        self.verbose = verbose
        self.session = _FakeModelsSession()
        _FakeClient.last_instance = self


class ChatGPTModelsFetchTests(unittest.TestCase):
    def test_normalize_models_url_reuses_conversation_origin(self):
        client = SimpleNamespace(BASE="https://chatgpt.com")

        self.assertEqual(
            message_tester._normalize_official_models_url(
                client,
                "https://chatgpt.com/backend-api/conversation",
            ),
            "https://chatgpt.com/backend-api/models",
        )
        self.assertEqual(
            message_tester._normalize_official_models_url(
                client,
                "https://example.com/custom/conversation",
            ),
            "https://example.com/custom/models",
        )
        self.assertEqual(
            message_tester._normalize_official_models_url(client, ""),
            "https://chatgpt.com/backend-api/models",
        )

    def test_extract_model_entries_reads_top_level_and_category_models(self):
        payload = {
            "models": [
                {"slug": "gpt-4o", "title": "GPT-4o"},
            ],
            "categories": [
                {
                    "models": [
                        {"slug": "o3", "title": "OpenAI o3"},
                        {"slug": "gpt-4o", "title": "GPT-4o duplicate"},
                    ]
                }
            ],
        }

        self.assertEqual(
            message_tester._extract_model_entries(payload),
            [
                {"id": "gpt-4o", "title": "GPT-4o", "description": ""},
                {"id": "o3", "title": "OpenAI o3", "description": ""},
            ],
        )

    def test_fetch_available_models_handles_non_context_manager_response(self):
        account = SimpleNamespace(
            access_token="access_token",
            refresh_token="refresh_token",
            session_token="",
            cookies="",
        )

        old_client = message_tester.ChatGPTClient
        old_prepare = message_tester._prepare_auth_context
        old_ensure = message_tester._ensure_access_token
        old_headers = message_tester._build_models_headers
        try:
            message_tester.ChatGPTClient = _FakeClient
            message_tester._prepare_auth_context = lambda client, account: None
            message_tester._ensure_access_token = lambda account, proxy: ("access_token", "", "")
            message_tester._build_models_headers = lambda client, access_token, target_url="": (
                "https://chatgpt.com/backend-api/models",
                {"authorization": f"Bearer {access_token}"},
            )

            result = message_tester.fetch_available_models(
                account,
                proxy="http://127.0.0.1:7890",
                target_url="https://chatgpt.com/backend-api/conversation",
            )
        finally:
            message_tester.ChatGPTClient = old_client
            message_tester._prepare_auth_context = old_prepare
            message_tester._ensure_access_token = old_ensure
            message_tester._build_models_headers = old_headers

        self.assertTrue(result.ok)
        self.assertEqual(result.models_url, "https://chatgpt.com/backend-api/models")
        self.assertEqual(
            result.models,
            [
                {"id": "gpt-4o", "title": "GPT-4o", "description": "omni"},
                {"id": "gpt-4.1", "title": "GPT-4.1", "description": ""},
            ],
        )
        self.assertEqual(result.message, "已获取 2 个模型")
        self.assertTrue(_FakeClient.last_instance.session.response.closed)
        self.assertTrue(_FakeClient.last_instance.session.closed)


if __name__ == "__main__":
    unittest.main()
