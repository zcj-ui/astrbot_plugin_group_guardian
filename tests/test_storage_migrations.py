"""SQLite schema migration tests for scheduled unban retries."""

import importlib.util
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_storage():
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
        "group_guardian_storage_migration_tests", ROOT / "storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in reversed(created):
        sys.modules.pop(name, None)
    return module


storage_module = _load_storage()


class StorageMigrationTests(unittest.TestCase):
    @staticmethod
    def _new_store(temp_dir):
        store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
        with store._connect() as conn:
            storage_module.SQLiteStorage._create_tables(conn)
        return store

    def test_appeal_creation_is_atomic_and_deduplicates_active_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._new_store(temp_dir)

            def create(index):
                return store.open_appeal(
                    "123", "456", f"reason-{index}", "mute", 60,
                    100, 1000,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                ids = list(executor.map(create, (1, 2)))

            self.assertEqual(1, sum(bool(item) for item in ids))
            self.assertEqual(1, len(store.list_appeals("waiting")))
            self.assertEqual(
                0,
                store.open_appeal("123", "456", "again", "mute", 60, 101, 1001),
            )
            self.assertEqual(
                0,
                store.open_appeal("789", "456", "other group", "mute", 60, 101, 1001),
            )

            # A stale row is closed inside the same transaction and does not
            # block a new punishment from opening its own appeal window, even
            # when the later punishment came from another group.
            new_id = store.open_appeal(
                "789", "456", "after expiry", "mute", 60, 1001, 2000
            )
            self.assertGreater(new_id, 0)
            rows = store.list_appeals()
            self.assertEqual("waiting", rows[0]["status"])
            self.assertEqual("789", rows[0]["group_id"])
            self.assertEqual("expired", rows[1]["status"])

    def test_appeal_attempt_claim_rejects_deadline_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._new_store(temp_dir)
            appeal_id = store.open_appeal(
                "123", "456", "reason", "mute", 60, 100, 200
            )
            self.assertEqual(0, store.claim_appeal_attempt(appeal_id, 2, 200))
            self.assertEqual(1, store.claim_appeal_attempt(appeal_id, 2, 199))

    def test_old_scheduled_unbans_table_is_migrated_and_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
            with sqlite3.connect(store.db_path) as conn:
                conn.execute(
                    "CREATE TABLE scheduled_unbans ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "group_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                    "unban_at INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                    "UNIQUE(group_id, user_id))"
                )
                conn.execute(
                    "INSERT INTO scheduled_unbans(group_id, user_id, unban_at, created_at) "
                    "VALUES('123', '456', 900, 800)"
                )

            with store._connect() as conn:
                storage_module.SQLiteStorage._create_tables(conn)
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(scheduled_unbans)"
                    ).fetchall()
                }

            self.assertTrue(
                {"retry_count", "next_retry_at", "last_error"}.issubset(columns)
            )
            due = store.list_due_unbans(1000)
            self.assertEqual(1, len(due))
            self.assertEqual(0, due[0]["retry_count"])
            self.assertEqual(0, due[0]["next_retry_at"])

            self.assertTrue(
                store.mark_scheduled_unban_retry(due[0]["id"], 1050, "temporary")
            )
            self.assertEqual([], store.list_due_unbans(1049))
            retried = store.list_due_unbans(1050)
            self.assertEqual(1, retried[0]["retry_count"])
            self.assertEqual("temporary", retried[0]["last_error"])

    def test_feedback_snapshot_survives_log_deletion_and_transitions_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
            with store._connect() as conn:
                storage_module.SQLiteStorage._create_tables(conn)
            store.add_log({
                "id": 7,
                "time": "2026-08-14 12:00:00",
                "ts": 1000,
                "group_id": "123",
                "user_id": "456",
                "user_name": "tester",
                "msg_text": "正常聊天被误判",
                "msg_preview": "正常聊天被误判",
                "action": "撤回+禁言",
                "reason": "疑似广告",
                "image_urls": [],
            })

            feedback_id = store.mark_moderation_feedback(
                7, "false_positive", "管理员确认是正常讨论"
            )

            self.assertGreater(feedback_id, 0)
            self.assertEqual(1, store.delete_logs([7]))
            feedback = store.list_moderation_feedback("false_positive")
            self.assertEqual("正常聊天被误判", feedback[0]["msg_text"])

            suggestion_id = store.create_prompt_suggestion(
                [feedback_id], "误把正常讨论当广告", "需要推广意图才判广告", ""
            )
            self.assertGreater(suggestion_id, 0)
            self.assertEqual(
                "reviewed",
                store.list_moderation_feedback("false_positive")[0]["review_status"],
            )
            self.assertTrue(store.transition_prompt_suggestion(
                suggestion_id, ["pending"], "applied", "dashboard"
            ))
            self.assertFalse(store.transition_prompt_suggestion(
                suggestion_id, ["pending"], "applied", "dashboard"
            ))
            self.assertEqual(
                ["applied", "generated"],
                [item["action"] for item in store.list_review_audit(suggestion_id)],
            )

    def test_stale_false_positive_cannot_create_prompt_suggestion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
            with store._connect() as conn:
                storage_module.SQLiteStorage._create_tables(conn)
            store.add_log({
                "id": 8,
                "time": "2026-08-14 12:00:00",
                "ts": 1000,
                "group_id": "123",
                "user_id": "456",
                "user_name": "tester",
                "msg_text": "待复核消息",
                "msg_preview": "待复核消息",
                "action": "撤回+禁言",
                "reason": "疑似广告",
                "image_urls": [],
            })
            feedback_id = store.mark_moderation_feedback(
                8, "false_positive", "最初认为误判"
            )
            store.mark_moderation_feedback(
                8, "confirmed_violation", "复查后确认处罚正确"
            )

            suggestion_id = store.create_prompt_suggestion(
                [feedback_id], "旧结论", "不应保存", ""
            )

            self.assertEqual(0, suggestion_id)
            self.assertEqual([], store.list_prompt_suggestions())


if __name__ == "__main__":
    unittest.main()
