import unittest

from api.tasks import _build_task_log_detail


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
        )

        self.assertEqual(detail["task_id"], "task_123")
        self.assertEqual(detail["attempt_no"], 2)
        self.assertEqual(detail["total_count"], 5)
        self.assertEqual(detail["source"], "cpa_replenish")
        self.assertEqual(detail["meta"], {"missing": 3})
        self.assertEqual(detail["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(
            detail["logs"],
            ["[12:00:00] 开始注册", "[12:00:10] ✓ 注册成功"],
        )


if __name__ == "__main__":
    unittest.main()
