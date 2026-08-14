"""v2.16.0 数据库性能优化回归测试。

覆盖 storage.py 的：
- 高频查询组合索引存在性（moderation_logs / group_activity / web_audit_logs）
- 统计查询带 TTL 的内存缓存（命中 / 不同 key / TTL 过期 / 前缀失效）
- add_log 写日志后违规积分计数缓存主动失效
- run_in_thread 线程池执行同步 DB 操作

无需安装 AstrBot：顶层 astrbot 包用 shim 代替。
"""

import importlib.util
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _stub_astrbot():
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})


def _load_module(filename, alias):
    path = _ROOT / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[alias] = module
    return module


def _load_storage():
    _stub_astrbot()
    if "lexicon_migration" not in sys.modules:
        _load_module("lexicon_migration.py", "lexicon_migration")
    if "storage_group" not in sys.modules:
        _load_module("storage_group.py", "storage_group")
    if "gg_storage" not in sys.modules:
        _load_module("storage.py", "gg_storage")
    return sys.modules["gg_storage"]


storage_mod = None


class DbPerfIndexTests(unittest.TestCase):
    """高频查询组合索引。"""

    @classmethod
    def setUpClass(cls):
        global storage_mod
        storage_mod = _load_storage()

    def make_storage(self, prefix="gg_dbperf_"):
        data_dir = Path(tempfile.mkdtemp(prefix=prefix))
        st = storage_mod.SQLiteStorage(data_dir, _ROOT)
        st.initialize()
        return st

    def _index_names(self, st, table):
        with st._connect() as conn:
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {r["name"] for r in rows}

    def test_composite_indexes_created(self):
        st = self.make_storage()
        self.assertIn("idx_logs_group_user_ts", self._index_names(st, "moderation_logs"))
        self.assertIn("idx_activity_group_ts_user", self._index_names(st, "group_activity"))
        self.assertIn("idx_web_audit_group_ts", self._index_names(st, "web_audit_logs"))

    def test_index_survives_reopen(self):
        # 索引随库持久化：重建 storage 实例后仍存在
        data_dir = Path(tempfile.mkdtemp(prefix="gg_dbperf_idx_"))
        st1 = storage_mod.SQLiteStorage(data_dir, _ROOT)
        st1.initialize()
        st2 = storage_mod.SQLiteStorage(data_dir, _ROOT)
        st2.initialize()
        self.assertIn("idx_logs_group_user_ts", self._index_names(st2, "moderation_logs"))


class DbPerfCacheTests(unittest.TestCase):
    """统计查询 TTL 缓存。"""

    @classmethod
    def setUpClass(cls):
        global storage_mod
        storage_mod = _load_storage()

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="gg_dbperf_cache_"))
        self.st = storage_mod.SQLiteStorage(data_dir, _ROOT)
        self.st.initialize()

    def test_same_key_hits_cache(self):
        calls = {"n": 0}

        def fn(x):
            calls["n"] += 1
            return x * 2

        self.assertEqual(self.st._query_cached("k", 30, fn, 21), 42)
        self.assertEqual(self.st._query_cached("k", 30, fn, 21), 42)
        self.assertEqual(calls["n"], 1)  # 第二次命中缓存，fn 不重复执行

    def test_distinct_keys_rerun(self):
        calls = {"n": 0}

        def fn(x):
            calls["n"] += 1
            return x

        self.st._query_cached("a", 30, fn, 1)
        self.st._query_cached("b", 30, fn, 2)
        self.assertEqual(calls["n"], 2)

    def test_ttl_expiry_reruns(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return calls["n"]

        self.st._query_cached("k", 0.001, fn)
        time.sleep(0.02)
        self.st._query_cached("k", 0.001, fn)
        self.assertEqual(calls["n"], 2)  # TTL 过期后重新执行

    def test_invalidate_prefix(self):
        calls = {"n": 0}

        def fn(x):
            calls["n"] += 1
            return x

        self.st._query_cached("violation:100:200:30", 30, fn, 1)
        self.st._query_cached("daily_trend:30", 30, fn, 2)
        self.st.invalidate_query_cache("violation:")
        self.st._query_cached("violation:100:200:30", 30, fn, 1)  # 前缀失效 → 重新执行
        self.st._query_cached("daily_trend:30", 30, fn, 2)          # 其它 key 仍命中缓存
        self.assertEqual(calls["n"], 3)


class DbPerfThreadTests(unittest.IsolatedAsyncioTestCase):
    """asyncio.to_thread 线程池执行 + 违规计数缓存与写日志联动。"""

    @classmethod
    def setUpClass(cls):
        global storage_mod
        storage_mod = _load_storage()

    async def test_run_in_thread_executes_sync_fn(self):
        data_dir = Path(tempfile.mkdtemp(prefix="gg_dbperf_thread_"))
        st = storage_mod.SQLiteStorage(data_dir, _ROOT)
        st.initialize()
        result = await st.run_in_thread(lambda: 40 + 2)
        self.assertEqual(result, 42)

    async def test_violation_count_cache_invalidated_by_add_log(self):
        data_dir = Path(tempfile.mkdtemp(prefix="gg_dbperf_viol_"))
        st = storage_mod.SQLiteStorage(data_dir, _ROOT)
        st.initialize()
        base = int(time.time())

        def make_log(i):
            return {
                "id": i, "time": "2026-08-14 10:00:00", "ts": base,
                "group_id": "100", "user_id": "200", "user_name": "u",
                "msg_text": "x", "msg_preview": "x", "action": "积分警告",
                "reason": "r", "image_urls": [],
            }

        st.add_log(make_log(1))
        # 首次查询未命中缓存 → 计数 1
        self.assertEqual(st.get_user_violation_count("100", "200", 30), 1)
        # 命中缓存仍为 1
        self.assertEqual(st.get_user_violation_count("100", "200", 30), 1)
        # add_log 主动失效缓存，新日志计入 → 2
        st.add_log(make_log(2))
        self.assertEqual(st.get_user_violation_count("100", "200", 30), 2)
        self.assertNotIn("violation:100:200:30", st._query_cache)


if __name__ == "__main__":
    unittest.main()

