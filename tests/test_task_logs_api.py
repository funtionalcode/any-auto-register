import json
import tempfile
import unittest
from unittest import mock

from sqlmodel import SQLModel, Session, create_engine

from api import tasks as tasks_api
from core.db import TaskLog


class TaskLogsApiTests(unittest.TestCase):
    def test_get_logs_supports_filters_and_returns_detail_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = create_engine(f"sqlite:///{tmp_dir}/test.db")
            SQLModel.metadata.create_all(engine)

            with Session(engine) as session:
                session.add(
                    TaskLog(
                        platform="chatgpt",
                        email="success@example.com",
                        status="success",
                        detail_json=json.dumps(
                            {
                                "task_id": "task_success",
                                "attempt_no": 1,
                                "total_count": 2,
                                "source": "manual",
                                "logs": ["[12:00:00] 开始注册", "[12:00:10] ✓ 注册成功"],
                                "duration_ms": 1500,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                session.add(
                    TaskLog(
                        platform="chatgpt",
                        email="failed@example.com",
                        status="failed",
                        error="网络错误",
                        detail_json=json.dumps(
                            {
                                "task_id": "task_failed",
                                "attempt_no": 2,
                                "total_count": 2,
                                "source": "cpa_replenish",
                                "logs": ["[12:10:00] 开始注册", "[12:10:10] ✗ 注册失败: 网络错误"],
                                "duration_ms": 2500,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                session.commit()

            with mock.patch.object(tasks_api, "engine", engine):
                result = tasks_api.get_logs(
                    platform="chatgpt",
                    status="success",
                    source="manual",
                    keyword="开始注册",
                    page=1,
                    page_size=20,
                )

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["page"], 1)
            self.assertEqual(result["page_size"], 20)
            self.assertEqual(len(result["items"]), 1)
            item = result["items"][0]
            self.assertEqual(item["email"], "success@example.com")
            self.assertEqual(item["detail_summary"]["source"], "manual")
            self.assertEqual(item["detail_summary"]["log_count"], 2)
            self.assertEqual(item["detail_summary"]["latest_log"], "[12:00:10] ✓ 注册成功")
            self.assertEqual(item["detail_summary"]["duration_ms"], 1500)

    def test_get_log_detail_returns_parsed_detail_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = create_engine(f"sqlite:///{tmp_dir}/test.db")
            SQLModel.metadata.create_all(engine)

            with Session(engine) as session:
                record = TaskLog(
                    platform="cursor",
                    email="detail@example.com",
                    status="failed",
                    error="boom",
                    detail_json=json.dumps(
                        {
                            "task_id": "task_detail",
                            "attempt_no": 3,
                            "total_count": 5,
                            "source": "manual",
                            "proxy": "http://127.0.0.1:7890",
                            "logs": ["[12:30:00] 开始注册", "[12:30:10] ✗ 注册失败: boom"],
                            "request": {"executor_type": "headed"},
                        },
                        ensure_ascii=False,
                    ),
                )
                session.add(record)
                session.commit()
                session.refresh(record)

            with mock.patch.object(tasks_api, "engine", engine):
                result = tasks_api.get_log_detail(int(record.id or 0))

            self.assertEqual(result["platform"], "cursor")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["detail"]["task_id"], "task_detail")
            self.assertEqual(result["detail"]["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(result["detail"]["request"], {"executor_type": "headed"})
            self.assertEqual(
                result["detail"]["logs"],
                ["[12:30:00] 开始注册", "[12:30:10] ✗ 注册失败: boom"],
            )


if __name__ == "__main__":
    unittest.main()
