import unittest

try:
    from platforms.chatgpt.register_v2 import RegistrationEngineV2
except ModuleNotFoundError:
    RegistrationEngineV2 = None


class _FailingEmailService:
    def create_email(self, config=None):
        raise AttributeError("'list' object has no attribute 'get'")


class RegisterV2TracebackTests(unittest.TestCase):
    @unittest.skipIf(RegistrationEngineV2 is None, "curl_cffi is not installed in the local test environment")
    def test_run_logs_traceback_lines_to_callback_logger(self):
        lines: list[str] = []
        engine = RegistrationEngineV2(
            email_service=_FailingEmailService(),
            callback_logger=lines.append,
            max_retries=1,
        )

        result = engine.run()
        joined = "\n".join(lines)

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "'list' object has no attribute 'get'")
        self.assertIn("Traceback (most recent call last):", joined)
        self.assertIn("create_email", joined)
        self.assertIn("AttributeError: 'list' object has no attribute 'get'", joined)


if __name__ == "__main__":
    unittest.main()
