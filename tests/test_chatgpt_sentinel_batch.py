import tempfile
import unittest
from pathlib import Path

from platforms.chatgpt.sentinel_batch import (
    ConfigResolver,
    DEFAULT_FLOW_SPECS,
    DEFAULT_FRAME_URL,
    DEFAULT_OUT,
    DEFAULT_SDK_URL,
    DEFAULT_USER_AGENT,
    ConfigBackedProxySelector,
    FlowSpec,
    SentinelBatchConfig,
    SentinelBatchService,
)


class _FakeConfigStore:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=""):
        return self.values.get(key, default)


class _FakeProxyPool:
    def __init__(self, proxy=None):
        self.proxy = proxy
        self.calls = 0

    def get_next(self):
        self.calls += 1
        return self.proxy


class _FakeProxySelector:
    def __init__(self, proxy):
        self.proxy = proxy

    def select_proxy(self):
        return self.proxy


class _FakeProvider:
    def __init__(self):
        self.token_calls = []
        self.so_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def get_flow_token(self, flow):
        self.token_calls.append(flow.internal_name)
        return f'{{"flow":"{flow.internal_name}","kind":"token"}}'

    def get_session_observer_token(self, flow):
        self.so_calls.append(flow.internal_name)
        return f'{{"flow":"{flow.internal_name}","kind":"so"}}'

    def resolved_sdk_url(self):
        return "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"


class _FakeProviderFactory:
    def __init__(self, provider):
        self.provider = provider
        self.last_config = None
        self.last_device_id = None

    def create(self, config, *, device_id):
        self.last_config = config
        self.last_device_id = device_id
        return self.provider


class ConfigBackedProxySelectorTests(unittest.TestCase):
    def test_prefers_explicit_proxy_server_over_proxy_pool(self):
        selector = ConfigBackedProxySelector(
            config=_FakeConfigStore({"PROXY_SERVER": "http://configured:8080"}),
            pool=_FakeProxyPool("http://pool:8080"),
        )

        self.assertEqual(selector.select_proxy(), "http://configured:8080")

    def test_builds_proxy_from_global_proxy_settings(self):
        selector = ConfigBackedProxySelector(
            config=_FakeConfigStore(
                {
                    "proxy.enabled": "true",
                    "proxy.type": "http",
                    "proxy.host": "127.0.0.1",
                    "proxy.port": "7890",
                }
            ),
            pool=_FakeProxyPool("http://pool:8080"),
        )

        self.assertEqual(selector.select_proxy(), "http://127.0.0.1:7890")

    def test_falls_back_to_proxy_pool_when_no_global_proxy(self):
        pool = _FakeProxyPool("socks5://pool:1080")
        selector = ConfigBackedProxySelector(
            config=_FakeConfigStore({}),
            pool=pool,
        )

        self.assertEqual(selector.select_proxy(), "socks5h://pool:1080")
        self.assertEqual(pool.calls, 1)

    def test_returns_none_when_proxy_pool_empty(self):
        selector = ConfigBackedProxySelector(
            config=_FakeConfigStore({}),
            pool=_FakeProxyPool(None),
        )

        self.assertIsNone(selector.select_proxy())


class ConfigResolverTests(unittest.TestCase):
    def test_uses_env_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolver = ConfigResolver(
                config=_FakeConfigStore({}),
                proxy_selector=_FakeProxySelector("http://pool:8888"),
                environ={
                    "OUT": str(Path(tmp_dir) / "out.json"),
                    "FRAME_URL": "https://example.com/frame.html",
                },
            )

            config = resolver.resolve()

        self.assertEqual(config.frame_url, "https://example.com/frame.html")
        self.assertEqual(config.sdk_url, DEFAULT_SDK_URL)
        self.assertEqual(config.user_agent, DEFAULT_USER_AGENT)
        self.assertEqual(config.output_path, Path(tmp_dir) / "out.json")
        self.assertEqual(config.proxy, "http://pool:8888")
        self.assertEqual(config.flows, DEFAULT_FLOW_SPECS)

    def test_flows_can_be_selected_by_alias_or_internal_name(self):
        resolver = ConfigResolver(
            config=_FakeConfigStore({}),
            proxy_selector=_FakeProxySelector(None),
            environ={
                "FLOWS": "authorize_continue,oauth-create-account",
            },
        )

        config = resolver.resolve()

        self.assertEqual(
            [item.internal_name for item in config.flows],
            ["authorize_continue", "oauth_create_account"],
        )

    def test_invalid_flow_raises_error(self):
        resolver = ConfigResolver(
            config=_FakeConfigStore({}),
            proxy_selector=_FakeProxySelector(None),
            environ={"FLOWS": "bad-flow"},
        )

        with self.assertRaises(ValueError):
            resolver.resolve()

    def test_default_out_path_uses_windows_safe_temp_location(self):
        resolver = ConfigResolver(
            config=_FakeConfigStore({}),
            proxy_selector=_FakeProxySelector(None),
            environ={},
        )

        config = resolver.resolve()

        self.assertEqual(config.output_path, DEFAULT_OUT)


class SentinelBatchServiceTests(unittest.TestCase):
    def test_requests_session_observer_only_for_oauth_create_account(self):
        provider = _FakeProvider()
        factory = _FakeProviderFactory(provider)
        service = SentinelBatchService(
            provider_factory=factory,
            device_id_factory=lambda: "device-fixed",
        )
        config = SentinelBatchConfig(
            frame_url=DEFAULT_FRAME_URL,
            sdk_url=DEFAULT_SDK_URL,
            user_agent=DEFAULT_USER_AGENT,
            output_path=Path(tempfile.gettempdir()) / "out.json",
            proxy=None,
            flows=DEFAULT_FLOW_SPECS,
            headless=True,
            headless_reason="default:true",
        )

        result = service.generate(config)

        self.assertEqual(factory.last_device_id, "device-fixed")
        self.assertEqual(
            provider.token_calls,
            [flow.internal_name for flow in DEFAULT_FLOW_SPECS],
        )
        self.assertEqual(provider.so_calls, ["oauth_create_account"])
        self.assertFalse(result.has_errors)
        self.assertEqual(
            result.flows["oauth-create-account"].sentinel_so_token,
            '{"flow":"oauth_create_account","kind":"so"}',
        )

    def test_result_serializes_stable_output_shape(self):
        provider = _FakeProvider()
        service = SentinelBatchService(
            provider_factory=_FakeProviderFactory(provider),
            device_id_factory=lambda: "device-fixed",
        )
        config = SentinelBatchConfig(
            frame_url=DEFAULT_FRAME_URL,
            sdk_url=DEFAULT_SDK_URL,
            user_agent=DEFAULT_USER_AGENT,
            output_path=Path(tempfile.gettempdir()) / "out.json",
            proxy="http://pool:8080",
            flows=(
                FlowSpec(
                    internal_name="authorize_continue",
                    alias="authorize-continue",
                    page_url="https://auth.openai.com/create-account",
                ),
            ),
            headless=True,
            headless_reason="default:true",
        )

        payload = service.generate(config).to_dict()

        self.assertEqual(payload["deviceId"], "device-fixed")
        self.assertEqual(payload["proxy"], "http://pool:8080")
        self.assertIn("authorize-continue", payload["flows"])
        self.assertIn("sentinel-token", payload["flows"]["authorize-continue"])


if __name__ == "__main__":
    unittest.main()
