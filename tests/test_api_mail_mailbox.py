import unittest

from core.base_mailbox import ApiMailMailbox


class ApiMailMailboxTests(unittest.TestCase):
    def test_get_email_accepts_list_domain_payload(self):
        mailbox = ApiMailMailbox(mail_tm_password="MailTm123!")
        responses = iter(
            [
                {"status_code": 200, "data": [{"domain": "mail.tm"}]},
                {"status_code": 201, "data": {"id": "account-1"}},
                {"status_code": 200, "data": {"token": "token-123"}},
            ]
        )
        mailbox._request = lambda *args, **kwargs: next(responses)

        account = mailbox.get_email()

        self.assertTrue(account.email.endswith("@mail.tm"))
        self.assertEqual(account.account_id, "token-123")


if __name__ == "__main__":
    unittest.main()
