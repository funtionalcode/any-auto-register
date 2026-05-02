import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.request_logging import RequestLoggingMiddleware


class RequestLoggingMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.configure_patch = mock.patch(
            "core.request_logging.configure_request_logging",
            return_value=Path("requests.log"),
        )
        self.configure_patch.start()

        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.post("/api/echo")
        async def echo(request: Request):
            payload = await request.json()
            return {
                "ok": True,
                "payload": payload,
                "token": "server-secret-token",
                "nested": {
                    "api_key": "server-api-key",
                    "value": "visible-response",
                },
            }

        @app.get("/health")
        async def health():
            return {"ok": True}

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.configure_patch.stop()

    def test_logs_request_and_response_with_masking(self):
        with mock.patch("core.request_logging.is_request_logging_enabled", return_value=True):
            with self.assertLogs("http.request", level="INFO") as captured:
                response = self.client.post(
                    "/api/echo?api_key=query-secret&mode=debug",
                    headers={
                        "Authorization": "Bearer user-secret",
                        "X-Test": "demo",
                    },
                    json={
                        "password": "client-secret-password",
                        "nested": {
                            "token": "client-token",
                            "value": "visible-request",
                        },
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["nested"]["value"], "visible-response")

        content = "\n".join(captured.output)
        self.assertIn("[HTTP] POST /api/echo -> 200", content)
        self.assertIn('"authorization": "***"', content)
        self.assertIn('"api_key": "***"', content)
        self.assertIn('"password": "***"', content)
        self.assertIn('"token": "***"', content)
        self.assertIn('"value": "visible-request"', content)
        self.assertIn('"value": "visible-response"', content)
        self.assertNotIn("client-secret-password", content)
        self.assertNotIn("client-token", content)
        self.assertNotIn("server-secret-token", content)
        self.assertNotIn("Bearer user-secret", content)
        self.assertNotIn("query-secret", content)

    def test_disabled_logging_skips_writer(self):
        with mock.patch("core.request_logging.is_request_logging_enabled", return_value=False):
            with mock.patch("core.request_logging.REQUEST_LOGGER.info") as info_mock:
                response = self.client.post("/api/echo", json={"ok": True})

        self.assertEqual(response.status_code, 200, response.text)
        info_mock.assert_not_called()

    def test_non_api_routes_are_not_logged(self):
        with mock.patch("core.request_logging.is_request_logging_enabled", return_value=True):
            with mock.patch("core.request_logging.REQUEST_LOGGER.info") as info_mock:
                response = self.client.get("/health")

        self.assertEqual(response.status_code, 200, response.text)
        info_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
