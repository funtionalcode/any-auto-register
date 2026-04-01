import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import api.sk_keys as sk_keys_module
from api.auth import router as auth_router
from api.sk_keys import openai_router, router as sk_router
from core.db import AccountModel, ProxyModel, get_session


class _MockHTTPResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def close(self):
        return None


class _MockStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code
        self.headers = {"Content-Type": "text/event-stream"}
        self.text = "\n".join(lines)

    def iter_lines(self, decode_unicode: bool = True):
        for line in self._lines:
            yield line if decode_unicode else line.encode("utf-8")

    def close(self):
        return None


class AuthAndSkFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.original_sk_engine = sk_keys_module.engine
        sk_keys_module.engine = self.engine

        app = FastAPI()
        app.include_router(auth_router, prefix="/api")
        app.include_router(sk_router, prefix="/api")
        app.include_router(openai_router)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        sk_keys_module.engine = self.original_sk_engine
        self.temp_dir.cleanup()

    def _bootstrap_admin(self) -> tuple[dict, dict]:
        response = self.client.post(
            "/api/auth/bootstrap",
            json={"username": "admin", "password": "secret123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        headers = {"Authorization": f"Bearer {payload['token']}"}
        return payload, headers

    def _create_proxy(self, url: str = "http://proxy.local:7890") -> int:
        with Session(self.engine) as session:
            proxy = ProxyModel(url=url, region="US", is_active=True)
            session.add(proxy)
            session.commit()
            session.refresh(proxy)
            return int(proxy.id or 0)

    def _create_chatgpt_account(self, email: str, token: str, status: str = "registered") -> int:
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token=token,
                status=status,
                extra_json="{}",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return int(account.id or 0)

    def test_bootstrap_login_and_role_guard(self):
        status_resp = self.client.get("/api/auth/bootstrap/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json(), {"bootstrapped": False})

        bootstrap_payload, admin_headers = self._bootstrap_admin()
        self.assertEqual(bootstrap_payload["user"]["role"], "admin")

        create_user_resp = self.client.post(
            "/api/auth/users",
            headers=admin_headers,
            json={"username": "alice", "password": "alice-pass", "role": "user"},
        )
        self.assertEqual(create_user_resp.status_code, 200, create_user_resp.text)

        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "alice-pass"},
        )
        self.assertEqual(login_resp.status_code, 200, login_resp.text)
        user_headers = {"Authorization": f"Bearer {login_resp.json()['token']}"}

        forbidden_resp = self.client.get("/api/auth/users", headers=user_headers)
        self.assertEqual(forbidden_resp.status_code, 403)

    def test_sk_key_binds_proxy_and_openai_v1_quota(self):
        _, admin_headers = self._bootstrap_admin()
        create_user_resp = self.client.post(
            "/api/auth/users",
            headers=admin_headers,
            json={"username": "alice", "password": "alice-pass", "role": "user"},
        )
        self.assertEqual(create_user_resp.status_code, 200, create_user_resp.text)
        alice_id = create_user_resp.json()["item"]["id"]
        proxy_id = self._create_proxy()

        create_key_resp = self.client.post(
            "/api/sk-keys",
            headers=admin_headers,
            json={
                "name": "alice-key",
                "owner_user_id": alice_id,
                "proxy_id": proxy_id,
                "target_url": "https://example.com/v1",
                "token_limit": 30,
            },
        )
        self.assertEqual(create_key_resp.status_code, 200, create_key_resp.text)
        create_key_payload = create_key_resp.json()
        secret_key = create_key_payload["secret_key"]
        sk_headers = {"Authorization": f"Bearer {secret_key}"}

        authorize_resp = self.client.post("/api/sk/authorize", headers=sk_headers)
        self.assertEqual(authorize_resp.status_code, 200, authorize_resp.text)
        self.assertEqual(
            authorize_resp.json()["api_key"]["resolved_proxy_url"],
            "http://proxy.local:7890",
        )

        models_payload = {"object": "list", "data": [{"id": "gpt-test", "object": "model"}]}
        with mock.patch("requests.get", return_value=_MockHTTPResponse(models_payload)) as mock_get:
            models_resp = self.client.get("/v1/models", headers=sk_headers)
        self.assertEqual(models_resp.status_code, 200, models_resp.text)
        self.assertEqual(models_resp.json()["data"][0]["id"], "gpt-test")
        self.assertTrue(mock_get.called)

        upstream_payload = {
            "id": "chatcmpl-demo",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }
        with mock.patch("requests.post", return_value=_MockHTTPResponse(upstream_payload)) as mock_post:
            first_chat_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-test",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 5,
                },
            )

        self.assertEqual(first_chat_resp.status_code, 200, first_chat_resp.text)
        self.assertEqual(first_chat_resp.json()["usage"]["total_tokens"], 12)
        self.assertTrue(mock_post.called)
        called_kwargs = mock_post.call_args.kwargs
        self.assertEqual(
            called_kwargs["proxies"],
            {"http": "http://proxy.local:7890", "https": "http://proxy.local:7890"},
        )
        self.assertEqual(
            called_kwargs["json"]["messages"],
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(
            called_kwargs["headers"]["Accept"],
            "application/json",
        )

        stream_response = _MockStreamResponse(
            [
                'data: {"id":"chatcmpl-stream","choices":[{"delta":{"content":"po"}}]}',
                "",
                'data: {"id":"chatcmpl-stream","choices":[{"delta":{"content":"ng"}}]}',
                "",
                'data: {"id":"chatcmpl-stream","usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        with mock.patch("requests.post", return_value=stream_response):
            streamed_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-test",
                    "messages": [{"role": "user", "content": "stream hello"}],
                    "stream": True,
                    "max_tokens": 3,
                },
            )
        self.assertEqual(streamed_resp.status_code, 200, streamed_resp.text)
        self.assertIn("data: [DONE]", streamed_resp.text)

        usage_resp = self.client.get(
            f"/api/sk-keys/{create_key_payload['item']['id']}/usage",
            headers=admin_headers,
        )
        self.assertEqual(usage_resp.status_code, 200, usage_resp.text)
        usage_payload = usage_resp.json()
        self.assertEqual(usage_payload["summary"]["total_tokens_used"], 18)
        self.assertEqual(usage_payload["summary"]["remaining_tokens"], 12)
        self.assertEqual(usage_payload["summary"]["request_count"], 3)
        self.assertEqual(len(usage_payload["items"]), 3)
        self.assertEqual(usage_payload["items"][0]["proxy_url"], "http://proxy.local:7890")

        over_limit_resp = self.client.post(
            "/v1/chat/completions",
            headers=sk_headers,
            json={
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 20,
            },
        )
        self.assertEqual(over_limit_resp.status_code, 429, over_limit_resp.text)
        self.assertIn("Token 配额不足", over_limit_resp.text)

    def test_sk_key_defaults_to_chatgpt_official_mode_for_models_and_completion(self):
        _, admin_headers = self._bootstrap_admin()

        create_key_resp = self.client.post(
            "/api/sk-keys",
            headers=admin_headers,
            json={
                "name": "official-key",
                "upstream_api_key": "chatgpt-access-token",
            },
        )
        self.assertEqual(create_key_resp.status_code, 200, create_key_resp.text)
        create_key_payload = create_key_resp.json()
        self.assertEqual(
            create_key_payload["item"]["target_url"],
            "https://chatgpt.com/backend-api/conversation",
        )
        sk_headers = {"Authorization": f"Bearer {create_key_payload['secret_key']}"}

        models_result = SimpleNamespace(
            ok=True,
            invalid=False,
            message="已获取 1 个模型",
            used_proxy="",
            models_url="https://chatgpt.com/backend-api/models",
            models=[{"id": "gpt-5", "title": "GPT-5", "description": "official"}],
            data=None,
            updated_access_token="",
            updated_refresh_token="",
            response_status_code=200,
        )
        completion_result = SimpleNamespace(
            ok=True,
            invalid=False,
            message="ok",
            response_excerpt="pong",
            response_text="pong",
            model="gpt-5",
            conversation_id="conv_123",
            response_message_id="msg_123",
            used_proxy="",
            updated_access_token="",
            updated_refresh_token="",
        )

        with mock.patch("platforms.chatgpt.message_tester.fetch_available_models", return_value=models_result) as mock_models:
            models_resp = self.client.get("/v1/models", headers=sk_headers)
        self.assertEqual(models_resp.status_code, 200, models_resp.text)
        self.assertEqual(models_resp.json()["data"][0]["id"], "gpt-5")
        self.assertTrue(mock_models.called)
        self.assertEqual(mock_models.call_args.kwargs["proxy"], "")
        self.assertEqual(
            mock_models.call_args.kwargs["target_url"],
            "https://chatgpt.com/backend-api/conversation",
        )

        with mock.patch("platforms.chatgpt.message_tester.send_chat_message", return_value=completion_result) as mock_send:
            chat_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        self.assertEqual(chat_resp.status_code, 200, chat_resp.text)
        chat_payload = chat_resp.json()
        self.assertEqual(chat_payload["choices"][0]["message"]["content"], "pong")
        self.assertEqual(chat_payload["conversation_id"], "conv_123")
        self.assertEqual(chat_payload["response_message_id"], "msg_123")
        self.assertGreater(chat_payload["usage"]["prompt_tokens"], 0)
        self.assertTrue(mock_send.called)
        self.assertEqual(mock_send.call_args.kwargs["proxy"], "")
        self.assertEqual(
            mock_send.call_args.kwargs["target_url"],
            "https://chatgpt.com/backend-api/conversation",
        )

        usage_resp = self.client.get(
            f"/api/sk-keys/{create_key_payload['item']['id']}/usage",
            headers=admin_headers,
        )
        self.assertEqual(usage_resp.status_code, 200, usage_resp.text)
        usage_payload = usage_resp.json()
        self.assertEqual(usage_payload["summary"]["request_count"], 2)
        self.assertGreater(usage_payload["summary"]["total_tokens_used"], 0)

    def test_sk_key_normalizes_chatgpt_models_path_back_to_official_conversation(self):
        _, admin_headers = self._bootstrap_admin()

        create_key_resp = self.client.post(
            "/api/sk-keys",
            headers=admin_headers,
            json={
                "name": "official-models-path-key",
                "target_url": "https://chatgpt.com/backend-api/conversation/models",
                "upstream_api_key": "chatgpt-access-token",
            },
        )
        self.assertEqual(create_key_resp.status_code, 200, create_key_resp.text)
        create_key_payload = create_key_resp.json()
        self.assertEqual(
            create_key_payload["item"]["target_url"],
            "https://chatgpt.com/backend-api/conversation",
        )

        sk_headers = {"Authorization": f"Bearer {create_key_payload['secret_key']}"}
        models_result = SimpleNamespace(
            ok=True,
            invalid=False,
            message="已获取 1 个模型",
            used_proxy="",
            models_url="https://chatgpt.com/backend-api/models",
            models=[{"id": "gpt-5", "title": "GPT-5", "description": "official"}],
            data=None,
            updated_access_token="",
            updated_refresh_token="",
            response_status_code=200,
        )

        with mock.patch("platforms.chatgpt.message_tester.fetch_available_models", return_value=models_result) as mock_models:
            models_resp = self.client.get("/v1/models", headers=sk_headers)

        self.assertEqual(models_resp.status_code, 200, models_resp.text)
        self.assertEqual(models_resp.json()["data"][0]["id"], "gpt-5")
        self.assertTrue(mock_models.called)
        self.assertEqual(
            mock_models.call_args.kwargs["target_url"],
            "https://chatgpt.com/backend-api/conversation",
        )

    def test_sk_key_wraps_chatgpt_official_stream_as_openai_sse(self):
        _, admin_headers = self._bootstrap_admin()

        create_key_resp = self.client.post(
            "/api/sk-keys",
            headers=admin_headers,
            json={
                "name": "official-stream-key",
                "upstream_api_key": "chatgpt-access-token",
            },
        )
        self.assertEqual(create_key_resp.status_code, 200, create_key_resp.text)
        create_key_payload = create_key_resp.json()
        sk_headers = {"Authorization": f"Bearer {create_key_payload['secret_key']}"}

        def fake_stream(*args, **kwargs):
            yield {
                "event": "meta",
                "data": {
                    "target_url": "https://chatgpt.com/backend-api/conversation",
                },
            }
            yield {"event": "delta", "data": {"delta": "po"}}
            yield {"event": "delta", "data": {"delta": "ng"}}
            yield {
                "event": "done",
                "data": {
                    "response_text": "pong",
                    "updated_access_token": "chatgpt-access-token-next",
                },
            }

        with mock.patch("platforms.chatgpt.message_tester.stream_chat_message", side_effect=fake_stream) as mock_stream:
            stream_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )
        self.assertEqual(stream_resp.status_code, 200, stream_resp.text)
        self.assertIn('"object": "chat.completion.chunk"', stream_resp.text)
        self.assertIn('"content": "po"', stream_resp.text)
        self.assertIn('"content": "ng"', stream_resp.text)
        self.assertIn("data: [DONE]", stream_resp.text)
        self.assertTrue(mock_stream.called)
        self.assertEqual(mock_stream.call_args.kwargs["proxy"], "")

        usage_resp = self.client.get(
            f"/api/sk-keys/{create_key_payload['item']['id']}/usage",
            headers=admin_headers,
        )
        self.assertEqual(usage_resp.status_code, 200, usage_resp.text)
        usage_payload = usage_resp.json()
        self.assertEqual(usage_payload["summary"]["request_count"], 1)
        self.assertGreater(usage_payload["summary"]["total_tokens_used"], 0)

    def test_sk_key_official_mode_round_robins_local_chatgpt_accounts_when_no_upstream_token(self):
        _, admin_headers = self._bootstrap_admin()
        self._create_chatgpt_account("a@example.com", "token-a")
        self._create_chatgpt_account("b@example.com", "token-b")

        create_key_resp = self.client.post(
            "/api/sk-keys",
            headers=admin_headers,
            json={
                "name": "official-pool-key",
            },
        )
        self.assertEqual(create_key_resp.status_code, 200, create_key_resp.text)
        sk_headers = {"Authorization": f"Bearer {create_key_resp.json()['secret_key']}"}

        completion_result = SimpleNamespace(
            ok=True,
            invalid=False,
            message="ok",
            response_excerpt="pong",
            response_text="pong",
            model="gpt-5",
            conversation_id="conv_pool",
            response_message_id="msg_pool",
            used_proxy="",
            updated_access_token="",
            updated_refresh_token="",
        )

        with mock.patch("platforms.chatgpt.message_tester.send_chat_message", return_value=completion_result) as mock_send:
            first_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hello a"}],
                },
            )
            second_resp = self.client.post(
                "/v1/chat/completions",
                headers=sk_headers,
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hello b"}],
                },
            )

        self.assertEqual(first_resp.status_code, 200, first_resp.text)
        self.assertEqual(second_resp.status_code, 200, second_resp.text)
        self.assertEqual(mock_send.call_count, 2)
        access_tokens = [call.args[0].access_token for call in mock_send.call_args_list]
        self.assertEqual(set(access_tokens), {"token-a", "token-b"})
        self.assertNotEqual(access_tokens[0], access_tokens[1])


if __name__ == "__main__":
    unittest.main()
