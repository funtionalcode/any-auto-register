import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from api import accounts as accounts_api
except ModuleNotFoundError:
    accounts_api = None


@unittest.skipIf(accounts_api is None, "fastapi is not installed in the local test environment")
class ChatGPTConversationModeTests(unittest.TestCase):
    def test_iter_official_chat_chunks_uses_send_chat_message_when_stream_disabled(self):
        sent_calls: list[tuple[str, str, str]] = []

        def fake_send_chat_message(account, *, proxy, prompt, model, conversation_id, parent_message_id):
            sent_calls.append((proxy, prompt, model))
            return SimpleNamespace(
                ok=True,
                invalid=False,
                message="ok",
                response_excerpt="hello",
                response_text="hello",
                conversation_id="conv_sync",
                response_message_id="msg_sync",
                used_proxy=proxy,
                model=model,
                updated_access_token="",
                updated_refresh_token="",
            )

        def fake_stream_chat_message(*args, **kwargs):
            raise AssertionError("stream_chat_message should not be called when stream is disabled")

        fake_module = types.SimpleNamespace(
            send_chat_message=fake_send_chat_message,
            stream_chat_message=fake_stream_chat_message,
        )

        with mock.patch.dict(sys.modules, {"platforms.chatgpt.message_tester": fake_module}):
            chunks = list(
                accounts_api._iter_official_chat_chunks(
                    SimpleNamespace(email="demo@example.com"),
                    proxy="http://127.0.0.1:7890",
                    prompt="hello",
                    model="auto",
                    conversation_id="",
                    parent_message_id="",
                    stream=False,
                )
            )

        self.assertEqual([item["event"] for item in chunks], ["meta", "done"])
        self.assertEqual(chunks[0]["data"]["request_mode"], "sync")
        self.assertEqual(chunks[0]["data"]["chain"], "send_chat_message")
        self.assertTrue(chunks[0]["data"]["shared_test_flow"])
        self.assertEqual(chunks[1]["data"]["response_text"], "hello")
        self.assertEqual(chunks[1]["data"]["conversation_id"], "conv_sync")
        self.assertEqual(chunks[1]["data"]["response_message_id"], "msg_sync")
        self.assertEqual(chunks[1]["data"]["request_mode"], "sync")
        self.assertEqual(sent_calls, [("http://127.0.0.1:7890", "hello", "auto")])


if __name__ == "__main__":
    unittest.main()
