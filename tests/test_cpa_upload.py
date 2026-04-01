import unittest
from unittest import mock

from platforms.chatgpt import cpa_upload


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class CpaUploadTests(unittest.TestCase):
    def test_upload_to_cpa_includes_request_url_in_http_error(self):
        token_data = {"email": "demo@example.com"}

        with mock.patch.object(cpa_upload, "CurlMime", return_value=mock.Mock()) as curl_mime_mock:
            with mock.patch.object(
                cpa_upload.cffi_requests,
                "post",
                return_value=_FakeResponse(500, payload={"message": "服务异常"}),
            ):
                ok, msg = cpa_upload.upload_to_cpa(
                    token_data,
                    api_url="https://cpa.example.com",
                    api_key="demo-key",
                )

        self.assertFalse(ok)
        self.assertIn("服务异常", msg)
        self.assertIn("请求地址: https://cpa.example.com/v0/management/auth-files", msg)
        curl_mime_mock.return_value.close.assert_called_once()

    def test_upload_to_cpa_includes_request_url_in_exception(self):
        token_data = {"email": "demo@example.com"}

        with mock.patch.object(cpa_upload, "CurlMime", return_value=mock.Mock()) as curl_mime_mock:
            with mock.patch.object(
                cpa_upload.cffi_requests,
                "post",
                side_effect=RuntimeError("connect reset"),
            ):
                ok, msg = cpa_upload.upload_to_cpa(
                    token_data,
                    api_url="https://cpa.example.com",
                    api_key="demo-key",
                )

        self.assertFalse(ok)
        self.assertIn("上传异常: connect reset", msg)
        self.assertIn("请求地址: https://cpa.example.com/v0/management/auth-files", msg)
        curl_mime_mock.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
