import tempfile
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from api import accounts as accounts_api
from core.db import AccountModel


class _FakePlatform:
    def __init__(self, config=None):
        self.config = config

    def check_valid(self, account):
        if account.email == "error@example.com":
            raise RuntimeError("boom")
        return account.email != "invalid@example.com"


class AccountsBatchCheckTests(unittest.TestCase):
    def test_batch_check_returns_summary_and_marks_invalid_accounts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = create_engine(f"sqlite:///{tmp_dir}/test.db")
            SQLModel.metadata.create_all(engine)

            with Session(engine) as session:
                good = AccountModel(platform="trae", email="good@example.com", password="pw", status="trial", token="token-a")
                invalid = AccountModel(platform="trae", email="invalid@example.com", password="pw", status="registered", token="token-b")
                errored = AccountModel(platform="trae", email="error@example.com", password="pw", status="registered", token="token-c")
                session.add(good)
                session.add(invalid)
                session.add(errored)
                session.commit()
                session.refresh(good)
                session.refresh(invalid)
                session.refresh(errored)

                with mock.patch.object(accounts_api, "load_all", lambda: None), \
                        mock.patch.object(accounts_api, "get", lambda _name: _FakePlatform), \
                        mock.patch.object(accounts_api.config_store, "get_all", lambda: {}):
                    result = accounts_api.batch_check_accounts(
                        accounts_api.BatchCheckRequest(ids=[good.id, invalid.id, errored.id, 999]),
                        session,
                    )

            self.assertEqual(result["total_requested"], 4)
            self.assertEqual(result["tested"], 3)
            self.assertEqual(result["valid"], 1)
            self.assertEqual(result["invalid"], 1)
            self.assertEqual(result["error"], 1)
            self.assertEqual(result["invalid_ids"], [invalid.id])
            self.assertEqual(result["error_ids"], [errored.id])
            self.assertEqual(result["not_found"], [999])

            with Session(engine) as session:
                rows = {
                    row.email: row
                    for row in session.exec(select(AccountModel)).all()
                }

            self.assertEqual(rows["good@example.com"].status, "trial")
            self.assertEqual(rows["invalid@example.com"].status, "invalid")
            self.assertEqual(rows["error@example.com"].status, "registered")

    def test_batch_check_uses_chatgpt_proxy_message_test(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = create_engine(f"sqlite:///{tmp_dir}/test.db")
            SQLModel.metadata.create_all(engine)

            with Session(engine) as session:
                good = AccountModel(
                    platform="chatgpt",
                    email="good@example.com",
                    password="pw",
                    status="registered",
                    token="old-token",
                    extra_json='{"refresh_token":"rt"}',
                )
                bad = AccountModel(
                    platform="chatgpt",
                    email="bad@example.com",
                    password="pw",
                    status="registered",
                    token="old-bad-token",
                )
                session.add(good)
                session.add(bad)
                session.commit()
                session.refresh(good)
                session.refresh(bad)

                class _Result:
                    def __init__(self, ok, invalid, message, updated_access_token=""):
                        self.ok = ok
                        self.invalid = invalid
                        self.message = message
                        self.response_excerpt = ""
                        self.model = "auto"
                        self.conversation_id = ""
                        self.used_proxy = "http://proxy.local:7890"
                        self.updated_access_token = updated_access_token
                        self.updated_refresh_token = ""

                with mock.patch.object(accounts_api, "load_all", lambda: None), \
                        mock.patch("api.accounts.config_store.get", lambda *args, **kwargs: ""), \
                        mock.patch("core.proxy_pool.proxy_pool.get_next", side_effect=["http://proxy.local:7890", "http://proxy.local:7890"]), \
                        mock.patch("core.proxy_pool.proxy_pool.report_success"), \
                        mock.patch("core.proxy_pool.proxy_pool.report_fail"), \
                        mock.patch("platforms.chatgpt.message_tester.send_test_message") as mock_send:
                    mock_send.side_effect = [
                        _Result(True, False, "ok", updated_access_token="new-token"),
                        _Result(False, True, "bad account"),
                    ]
                    result = accounts_api.batch_check_accounts(
                        accounts_api.BatchCheckRequest(ids=[good.id, bad.id]),
                        session,
                    )

            self.assertEqual(result["valid"], 1)
            self.assertEqual(result["invalid"], 1)
            self.assertEqual(result["error"], 0)
            self.assertEqual(result["invalid_ids"], [bad.id])

            with Session(engine) as session:
                rows = {
                    row.email: row
                    for row in session.exec(select(AccountModel)).all()
                }

            self.assertEqual(rows["good@example.com"].token, "new-token")
            self.assertEqual(rows["bad@example.com"].status, "invalid")


if __name__ == "__main__":
    unittest.main()
