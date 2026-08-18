"""v2.26.0 内存自动回收机制测试。

覆盖：
- memory_guard.py：内存读取、缓存清理、指纹裁剪、阈值触发/跳过
- _conf_schema.json：3 个内存回收配置项
- scheduler.py / main.py：后台循环集成
"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def _load_memory_guard():
    _stub_astrbot()
    path = ROOT / "memory_guard.py"
    spec = importlib.util.spec_from_file_location("group_guardian_memory_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memory_guard = _load_memory_guard()


class _Harness(memory_guard.MemoryGuardMixin):
    def __init__(self):
        self.cfg_values = {}
        self._recent_media_hashes = {"url1": "phash1"}
        self._recent_video_fingerprints = {"fp1_10": 1}
        self._query_cache = {"q1": "v1"}
        self._web_group_cache = {"ts": 1.0, "data": [1, 2]}
        self._admin_role_cache = {"g:u": ("admin", 1.0)}
        self._card_snapshots = {"g1": {"u1": "card"}}
        self._stats_cache = {
            "today_start": 1000,
            "group_stats": {"g1": 3},
            "user_stats": {"u1": 2},
        }
        self._video_fp_cache = {}

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)


class MemoryReadingTests(unittest.TestCase):
    def test_current_process_memory_positive(self):
        mb = memory_guard.MemoryGuardMixin._current_process_memory_mb()
        self.assertGreater(mb, 0)  # 本机应能读到 RSS


class CacheCleanTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_clean_cache_entries(self):
        self.h._memory_guard_clean_cache_entries()
        self.assertEqual({}, self.h._recent_media_hashes)
        self.assertEqual({}, self.h._recent_video_fingerprints)
        self.assertEqual({}, self.h._query_cache)
        self.assertEqual({}, self.h._web_group_cache)
        self.assertEqual({}, self.h._admin_role_cache)
        self.assertEqual({}, self.h._card_snapshots)
        self.assertEqual({}, self.h._stats_cache["group_stats"])
        self.assertEqual({}, self.h._stats_cache["user_stats"])
        # today_start 保留
        self.assertEqual(1000, self.h._stats_cache["today_start"])

    def test_trim_video_fp_cache(self):
        for i in range(150):
            self.h._video_fp_cache[f"fp{i}_{i}"] = i
        self.h._memory_guard_trim_video_fp_cache()
        self.assertLessEqual(len(self.h._video_fp_cache), 100)

    def test_trim_skips_small_cache(self):
        self.h._video_fp_cache = {"fp1_1": 1}
        self.h._memory_guard_trim_video_fp_cache()
        self.assertEqual(1, len(self.h._video_fp_cache))


class RunGuardTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_run_guard_skips_below_threshold(self):
        self.h.cfg_values["memory_guard_threshold_mb"] = 999999
        self.h._current_process_memory_mb = lambda: 100.0
        self.h._memory_guard_clean_cache_entries = lambda: (
            self.h._card_snapshots.clear()
        )
        self.h._run_memory_guard()
        # 未达阈值：不清缓存
        self.assertEqual({"g1": {"u1": "card"}}, self.h._card_snapshots)

    def test_run_guard_triggers_at_threshold(self):
        self.h.cfg_values["memory_guard_threshold_mb"] = 1
        self.h._current_process_memory_mb = lambda: 100.0
        self.h._run_memory_guard()
        self.assertEqual({}, self.h._card_snapshots)
        self.assertEqual({}, self.h._recent_media_hashes)
        self.assertEqual({}, self.h._recent_video_fingerprints)

    def test_run_guard_unlimited_threshold(self):
        # 阈值 0 = 每周期都回收
        self.h.cfg_values["memory_guard_threshold_mb"] = 0
        self.h._current_process_memory_mb = lambda: 100.0
        self.h._run_memory_guard()
        self.assertEqual({}, self.h._card_snapshots)


class MemoryGuardStaticChecks(unittest.TestCase):
    def test_schema_has_configs(self):
        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertTrue(schema["memory_guard_enabled"]["default"])
        self.assertEqual(0, schema["memory_guard_threshold_mb"]["default"])
        self.assertEqual(60, schema["memory_guard_interval_sec"]["default"])

    def test_scheduler_integrates_memory_guard(self):
        src = (ROOT / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn("_memory_guard_task", src)
        self.assertIn("self._memory_guard_loop()", src)

    def test_main_inherits_memory_guard(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("MemoryGuardMixin", src)

    def test_memory_guard_no_temp_dir_cleanup(self):
        src = (ROOT / "memory_guard.py").read_text(encoding="utf-8")
        # 不清理视频临时目录（避免中断在途审核）
        self.assertNotIn("_cleanup_video_temp_dir", src)


if __name__ == "__main__":
    unittest.main()
