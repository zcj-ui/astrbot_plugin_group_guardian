"""定时解禁成功删除、失败退避重试的回归测试。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_scheduler():
    created = []
    if "astrbot" not in sys.modules:
        sys.modules["astrbot"] = types.ModuleType("astrbot")
        created.append("astrbot")
    if "astrbot.api" not in sys.modules:
        sys.modules["astrbot.api"] = types.ModuleType("astrbot.api")
        created.append("astrbot.api")
    astrbot = sys.modules["astrbot"]
    api = sys.modules["astrbot.api"]
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
        )
    astrbot.api = api
    spec = importlib.util.spec_from_file_location(
        "group_guardian_scheduler_tests", ROOT / "scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in reversed(created):
        sys.modules.pop(name, None)
    return module


scheduler = _load_scheduler()


class _Storage:
    def __init__(self, due):
        self.due = due
        self.deleted = []
        self.retries = []

    def list_due_unbans(self, _now):
        return list(self.due)

    def delete_scheduled_unban(self, unban_id):
        self.deleted.append(unban_id)

    def mark_scheduled_unban_retry(self, unban_id, next_retry_at, last_error):
        self.retries.append((unban_id, next_retry_at, last_error))


class _Harness(scheduler.SchedulerMixin):
    def __init__(self, succeeds, retry_count=0):
        self.succeeds = succeeds
        self._storage = _Storage([{
            "id": 7,
            "group_id": "123",
            "user_id": "456",
            "unban_at": 900,
            "retry_count": retry_count,
        }])

    def _cfg(self, _key, default=False, group_id=None):
        return True

    async def _unban_member(self, group_id, user_id):
        return self.succeeds


class SchedulerRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_deletes_completed_plan(self):
        harness = _Harness(True)

        with patch.object(scheduler.time, "time", return_value=1000):
            await harness._run_due_unbans()

        self.assertEqual([7], harness._storage.deleted)
        self.assertEqual([], harness._storage.retries)

    async def test_failure_keeps_plan_and_schedules_retry(self):
        harness = _Harness(False)

        with patch.object(scheduler.time, "time", return_value=1000):
            await harness._run_due_unbans()

        self.assertEqual([], harness._storage.deleted)
        self.assertEqual(
            [(7, 1030, "OneBot 解禁失败")], harness._storage.retries
        )

    async def test_retry_delay_is_capped(self):
        harness = _Harness(False, retry_count=100)

        with patch.object(scheduler.time, "time", return_value=1000):
            await harness._run_due_unbans()

        self.assertEqual(4600, harness._storage.retries[0][1])


if __name__ == "__main__":
    unittest.main()
