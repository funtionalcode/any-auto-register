import unittest

from core.base_mailbox import BaseMailbox, MailboxAccount


class _DummyMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        raise NotImplementedError

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()


class MailboxMessageNormalizationTests(unittest.TestCase):
    def test_decode_raw_content_produces_plain_text_from_quoted_printable_html(self):
        mailbox = _DummyMailbox()
        raw_html = (
            '<!doctype html>\n'
            '<html lang=3D"en">\n'
            "<body>\n"
            "<p>Validate your email</p>\n"
            '<div style=3D"font-weight:bold">KH1-7YU</div>\n'
            "</body>\n"
            "</html>\n"
        )

        decoded_text = mailbox._decode_raw_content(raw_html)

        self.assertIn("Validate your email", decoded_text)
        self.assertIn("KH1-7YU", decoded_text)
        self.assertNotIn("=3D", decoded_text)

    def test_decode_raw_payload_does_not_strip_html_before_blank_line(self):
        mailbox = _DummyMailbox()
        raw_html = "<!doctype html>\n<html>\n<body>\n\n<p>Hello</p>\n</body>\n</html>"

        decoded_html = mailbox._decode_raw_html(raw_html)

        self.assertIn("<!doctype html>", decoded_html.lower())
        self.assertIn("<p>Hello</p>", decoded_html)
        self.assertEqual(decoded_html, mailbox._extract_html_content(raw_html))


if __name__ == "__main__":
    unittest.main()
