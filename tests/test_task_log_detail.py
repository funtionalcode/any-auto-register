import unittest

from api.tasks import _build_task_log_detail, _build_task_log_summary


class TaskLogDetailTests(unittest.TestCase):
    def test_build_task_log_detail_preserves_attempt_context_and_logs(self):
        detail = _build_task_log_detail(
            task_id="task_123",
            attempt_no=2,
            total_count=5,
            logs=["[12:00:00] 开始注册", "[12:00:10] ✓ 注册成功"],
            source="cpa_replenish",
            meta={"missing": 3},
            proxy="http://127.0.0.1:7890",
            started_at=100.0,
            finished_at=101.25,
            request={"executor_type": "protocol"},
        )

        self.assertEqual(detail["task_id"], "task_123")
        self.assertEqual(detail["attempt_no"], 2)
        self.assertEqual(detail["total_count"], 5)
        self.assertEqual(detail["source"], "cpa_replenish")
        self.assertEqual(detail["meta"], {"missing": 3})
        self.assertEqual(detail["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(detail["log_count"], 2)
        self.assertEqual(detail["duration_ms"], 1250)
        self.assertEqual(detail["request"], {"executor_type": "protocol"})
        self.assertEqual(
            detail["logs"],
            ["[12:00:00] 开始注册", "[12:00:10] ✓ 注册成功"],
        )

    def test_build_task_log_summary_extracts_compact_metadata(self):
        summary = _build_task_log_summary(
            {
                "task_id": "task_123",
                "attempt_no": 2,
                "total_count": 5,
                "source": "manual",
                "proxy": "http://127.0.0.1:7890",
                "logs": ["[12:00:00] 开始注册", "[12:00:10] ✓ 注册成功"],
                "started_at": 100.0,
                "finished_at": 101.25,
                "duration_ms": 1250,
            }
        )

        self.assertEqual(summary["task_id"], "task_123")
        self.assertEqual(summary["attempt_no"], 2)
        self.assertEqual(summary["total_count"], 5)
        self.assertEqual(summary["source"], "manual")
        self.assertEqual(summary["proxy"], "http://127.0.0.1:7890")
        self.assertTrue(summary["has_logs"])
        self.assertEqual(summary["log_count"], 2)
        self.assertEqual(summary["latest_log"], "[12:00:10] ✓ 注册成功")
        self.assertEqual(summary["duration_ms"], 1250)


if __name__ == "__main__":
    unittest.main()
