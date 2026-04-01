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
    if importlib.util.find_spec("curl_cffi") is None:
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


class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"message":{"id":"msg_1","content":{"parts":["Hello from ChatGPT"]}},"conversation_id":"conv_1"}'
        yield "data: [DONE]"

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self):
        self.response = _FakeResponse()
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        return self.response

    def close(self):
        self.closed = True


class _FakeClient:
    last_instance = None

    def __init__(self, proxy=None, verbose=True):
        self.proxy = proxy
        self.verbose = verbose
        self.session = _FakeSession()
        _FakeClient.last_instance = self


class ChatGPTMessageStreamTests(unittest.TestCase):
    def test_stream_chat_message_handles_non_context_manager_response(self):
        account = SimpleNamespace(
            access_token="access_token",
            refresh_token="refresh_token",
            session_token="",
            cookies="",
        )

        old_client = message_tester.ChatGPTClient
        old_prepare = message_tester._prepare_auth_context
        old_ensure = message_tester._ensure_access_token
        old_requirements = message_tester._get_chat_requirements
        old_headers = message_tester._build_conversation_headers
        try:
            message_tester.ChatGPTClient = _FakeClient
            message_tester._prepare_auth_context = lambda client, account: None
            message_tester._ensure_access_token = lambda account, proxy: ("access_token", "", "")
            message_tester._get_chat_requirements = lambda client, access_token: ("requirements", "proof")
            message_tester._build_conversation_headers = lambda client, access_token, requirements_token, proof_token: (
                "https://chatgpt.com/backend-api/conversation",
                {"authorization": f"Bearer {access_token}"},
            )

            events = list(
                message_tester.stream_chat_message(
                    account,
                    proxy="http://127.0.0.1:7890",
                    prompt="hello",
                )
            )
        finally:
            message_tester.ChatGPTClient = old_client
            message_tester._prepare_auth_context = old_prepare
            message_tester._ensure_access_token = old_ensure
            message_tester._get_chat_requirements = old_requirements
            message_tester._build_conversation_headers = old_headers

        self.assertEqual([item["event"] for item in events], ["meta", "delta", "done"])
        self.assertEqual(events[0]["data"]["chain"], "stream_chat_message")
        self.assertTrue(events[0]["data"]["shared_test_flow"])
        self.assertEqual(events[0]["data"]["request_mode"], "stream")
        self.assertEqual(events[1]["data"]["delta"], "Hello from ChatGPT")
        self.assertEqual(events[2]["data"]["response_text"], "Hello from ChatGPT")
        self.assertEqual(events[2]["data"]["conversation_id"], "conv_1")
        self.assertEqual(events[2]["data"]["response_message_id"], "msg_1")
        self.assertEqual(events[2]["data"]["chain"], "stream_chat_message")
        self.assertTrue(events[2]["data"]["shared_test_flow"])
        self.assertTrue(_FakeClient.last_instance.session.response.closed)
        self.assertTrue(_FakeClient.last_instance.session.closed)


if __name__ == "__main__":
    unittest.main()
