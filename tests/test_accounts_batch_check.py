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
                good = AccountModel(platform="chatgpt", email="good@example.com", password="pw", status="trial")
                invalid = AccountModel(platform="chatgpt", email="invalid@example.com", password="pw", status="registered")
                errored = AccountModel(platform="chatgpt", email="error@example.com", password="pw", status="registered")
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


if __name__ == "__main__":
    unittest.main()
