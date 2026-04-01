import tempfile
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountModel
from services import cpa_manager


class CpaManagerTests(unittest.TestCase):
    def test_extract_chatgpt_emails_from_auth_names(self):
        emails = cpa_manager._extract_chatgpt_emails_from_auth_names(
            [
                "demo@example.com.json",
                "/tmp/second@example.com.json",
                "invalid-name",
                "DEMO@example.com.json",
            ]
        )

        self.assertEqual(emails, ["demo@example.com", "second@example.com"])

    def test_delete_local_chatgpt_accounts_by_auth_names_removes_matching_chatgpt_rows_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = create_engine(f"sqlite:///{tmp_dir}/test.db")
            SQLModel.metadata.create_all(engine)

            with Session(engine) as session:
                session.add(AccountModel(platform="chatgpt", email="demo@example.com", password="pw"))
                session.add(AccountModel(platform="chatgpt", email="keep@example.com", password="pw"))
                session.add(AccountModel(platform="kiro", email="demo@example.com", password="pw"))
                session.commit()

            with mock.patch.object(cpa_manager, "engine", engine):
                result = cpa_manager._delete_local_chatgpt_accounts_by_auth_names(
                    ["demo@example.com.json"]
                )

            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["emails"], ["demo@example.com"])

            with Session(engine) as session:
                rows = session.exec(select(AccountModel).order_by(AccountModel.platform, AccountModel.email)).all()

            self.assertEqual(
                [(row.platform, row.email) for row in rows],
                [("chatgpt", "keep@example.com"), ("kiro", "demo@example.com")],
            )


if __name__ == "__main__":
    unittest.main()
