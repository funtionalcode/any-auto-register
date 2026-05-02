import unittest
from unittest import mock

from core.luckmail.client import LuckMailClient
from core.luckmail.http_client import LuckMailHttpClient


class LuckMailProxyChainTests(unittest.TestCase):
    @mock.patch("core.luckmail.client.LuckMailHttpClient")
    def test_luckmail_client_forwards_proxy_to_http_client(self, mock_http_cls):
        LuckMailClient(
            base_url="https://example.com",
            api_key="k",
            proxy_url="socks5://127.0.0.1:7890",
        )
        mock_http_cls.assert_called_once()
        self.assertEqual(
            mock_http_cls.call_args.kwargs.get("proxy_url"),
            "socks5://127.0.0.1:7890",
        )

    @mock.patch("core.luckmail.http_client.curl_requests.Session")
    def test_http_client_sync_session_uses_normalized_proxy(self, mock_session_cls):
        session_obj = mock.Mock()
        mock_session_cls.return_value = session_obj

        client = LuckMailHttpClient(
            base_url="https://example.com",
            api_key="k",
            proxy_url="socks5://127.0.0.1:7890",
        )
        _ = client._get_sync_session()

        mock_session_cls.assert_called_once()
        self.assertEqual(
            mock_session_cls.call_args.kwargs.get("proxy"),
            "socks5h://127.0.0.1:7890",
        )
        self.assertEqual(
            session_obj.proxies,
            {
                "http": "socks5h://127.0.0.1:7890",
                "https": "socks5h://127.0.0.1:7890",
            },
        )


if __name__ == "__main__":
    unittest.main()
