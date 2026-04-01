import csv
import io
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from api import accounts as accounts_api
from core.db import AccountModel


class AccountsExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp_dir.name}/test.db")
        SQLModel.metadata.create_all(self.engine)
        self.original_engine = accounts_api.engine
        accounts_api.engine = self.engine

        with Session(self.engine) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="alpha@example.com",
                    password="pw-alpha",
                    user_id="user-alpha",
                    region="US",
                    status="registered",
                    cashier_url="https://cashier/alpha",
                )
            )
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="beta@example.com",
                    password="pw-beta",
                    user_id="user-beta",
                    region="JP",
                    status="registered",
                    cashier_url="https://cashier/beta",
                )
            )
            session.add(
                AccountModel(
                    platform="claude",
                    email="gamma@example.com",
                    password="pw-gamma",
                    user_id="user-gamma",
                    region="SG",
                    status="invalid",
                    cashier_url="https://cashier/gamma",
                )
            )
            session.commit()

        app = FastAPI()
        app.include_router(accounts_api.router, prefix="/api")
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        accounts_api.engine = self.original_engine
        self.temp_dir.cleanup()

    def test_export_generator_streams_in_chunks(self):
        chunks = list(accounts_api._iter_accounts_export_csv(platform="chatgpt", status="registered", batch_size=1))
        self.assertGreater(len(chunks), 2)

        content = "".join(chunks)
        rows = list(csv.DictReader(io.StringIO(content)))

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["email"] for row in rows], ["alpha@example.com", "beta@example.com"])
        self.assertEqual(rows[0]["cashier_url"], "https://cashier/alpha")
        self.assertEqual(rows[1]["status"], "registered")

    def test_export_route_returns_filtered_csv_attachment(self):
        response = self.client.get("/api/accounts/export?platform=chatgpt&status=registered")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('attachment; filename="accounts_chatgpt_registered.csv"', response.headers["content-disposition"])
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("alpha@example.com", response.text)
        self.assertIn("beta@example.com", response.text)
        self.assertNotIn("gamma@example.com", response.text)


if __name__ == "__main__":
    unittest.main()
