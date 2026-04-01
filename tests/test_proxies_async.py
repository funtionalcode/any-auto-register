import tempfile
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from api import proxies as proxies_api
from core.db import ProxyModel, get_session


class ProxyAsyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{self.temp_dir.name}/test.db",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.original_engine = proxies_api.engine
        proxies_api.engine = self.engine
        with proxies_api._proxy_test_tasks_lock:
            proxies_api._proxy_test_tasks.clear()

        app = FastAPI()
        app.include_router(proxies_api.router, prefix="/api")

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        proxies_api.engine = self.original_engine
        with proxies_api._proxy_test_tasks_lock:
            proxies_api._proxy_test_tasks.clear()
        self.temp_dir.cleanup()

    def test_saved_proxy_async_returns_snapshot(self):
        with Session(self.engine) as session:
            proxy = ProxyModel(url="http://proxy.local:7890", region="US", is_active=True)
            session.add(proxy)
            session.commit()
            session.refresh(proxy)
            proxy_id = int(proxy.id or 0)

        fake_result = {
            "ok": True,
            "ip": "1.2.3.4",
            "region_label": "California",
            "country": "United States",
            "latency_ms": 120,
            "normalized_url": "http://proxy.local:7890",
        }

        with mock.patch.object(proxies_api.proxy_pool, "test_proxy", return_value=fake_result):
            create_resp = self.client.post(
                f"/api/proxies/{proxy_id}/test/async",
                json={"save_region": False},
            )

            self.assertEqual(create_resp.status_code, 200, create_resp.text)
            payload = create_resp.json()
            self.assertIn("id", payload)

            snapshot = payload
            for _ in range(40):
                detail_resp = self.client.get(f"/api/proxies/test/tasks/{payload['id']}")
                self.assertEqual(detail_resp.status_code, 200, detail_resp.text)
                snapshot = detail_resp.json()
                if snapshot["status"] in {"done", "failed"}:
                    break
                time.sleep(0.05)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["proxy_id"], proxy_id)
        self.assertEqual(snapshot["current_region"], "US")
        self.assertEqual(snapshot["result"]["region_label"], "California")
        self.assertEqual(snapshot["result"]["proxy"]["id"], proxy_id)


if __name__ == "__main__":
    unittest.main()
