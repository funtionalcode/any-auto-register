import json
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api import accounts as accounts_api
from core.db import AccountModel


class ChatGPTQuotaSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp_dir.name}/test.db")
        SQLModel.metadata.create_all(self.engine)
        self.original_engine = accounts_api.engine
        accounts_api.engine = self.engine

    def tearDown(self):
        accounts_api.engine = self.original_engine
        self.temp_dir.cleanup()

    def test_chatgpt_quota_persists_compact_snapshot(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="quota@example.com",
                password="pw",
                token="access-token",
                status="registered",
                extra_json='{"refresh_token":"refresh-token"}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)

            fake_result = SimpleNamespace(
                ok=True,
                invalid=False,
                message="已获取官方账户信息，当前套餐: chatgptfreeplan",
                used_proxy="socks5h://172.17.224.1:7890",
                query_url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                summary={
                    "account_id": "acc-1",
                    "account_structure": "personal",
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                signals=[
                    {"path": "accounts.default.balance.remaining", "value": 12.5},
                    {"path": "accounts.default.entitlement.expires_at", "value": None},
                ],
                data={"accounts": {"default": {}}},
                updated_access_token="",
                updated_refresh_token="",
                response_status_code=200,
            )
            fake_module = types.SimpleNamespace(fetch_official_quota=lambda *args, **kwargs: fake_result)

            with mock.patch.dict(sys.modules, {"platforms.chatgpt.message_tester": fake_module}), \
                    mock.patch.object(accounts_api, "_report_chatgpt_proxy_result"), \
                    mock.patch.object(accounts_api, "_persist_chatgpt_message_result"):
                payload = accounts_api.chatgpt_quota(
                    int(account.id or 0),
                    accounts_api.ChatGPTModelsRequest(proxy="socks5h://172.17.224.1:7890"),
                    session,
                )

        self.assertEqual(payload["message"], "已获取官方账户信息，当前套餐: chatgptfreeplan")
        self.assertEqual(payload["official_quota"]["subscription_plan"], "chatgptfreeplan")
        self.assertEqual(payload["official_quota"]["remaining_display"], "12.5")
        self.assertEqual(payload["official_quota"]["remaining_path"], "accounts.default.balance.remaining")
        self.assertEqual(payload["official_quota"]["response_status_code"], 200)

        with Session(self.engine) as session:
            saved = session.get(AccountModel, int(account.id or 0))
            self.assertIsNotNone(saved)
            extra = json.loads(saved.extra_json or "{}")
            snapshot = extra.get("official_quota")

        self.assertEqual(snapshot["subscription_plan"], "chatgptfreeplan")
        self.assertEqual(snapshot["remaining_display"], "12.5")
        self.assertEqual(snapshot["remaining_path"], "accounts.default.balance.remaining")


if __name__ == "__main__":
    unittest.main()
