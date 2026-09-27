"""自适应上下文学习（lexicon_learn）回归测试。

覆盖：候选解析、关键词安全过滤、按群匹配器命中、多维度自动审批门，
以及 learned_keywords 的真实 SQL CRUD（UPSERT 累计 / 状态流转 / 已生效列表）。
"""

import asyncio
import contextlib
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_api = sys.modules.setdefault(
        "astrbot.api.event", types.ModuleType("astrbot.api.event"))
    event_api.AstrMessageEvent = object
    astrbot.api = api


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
lexicon_learn = _load_module("group_guardian_lexicon_learn", "lexicon_learn.py")
storage_group = _load_module("group_guardian_storage_group", "storage_group.py")

_LEARNED_DDL = (
    "CREATE TABLE learned_keywords ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, keyword TEXT NOT NULL, "
    "category TEXT NOT NULL DEFAULT 'ad', status TEXT NOT NULL DEFAULT 'pending', "
    "reason TEXT DEFAULT '', sample TEXT DEFAULT '', confidence REAL DEFAULT 0, "
    "occurrences INTEGER DEFAULT 1, source TEXT DEFAULT 'llm', "
    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(group_id, keyword))"
)


class _MiniStore(storage_group.GroupStorageMixin):
    """只建 learned_keywords 表的最小 storage，用于测真实 SQL 而不必 seed 大词库。"""

    def __init__(self, path):
        self._path = path
        with self._connect() as conn:
            conn.execute(_LEARNED_DDL)
            conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


class _Learn(lexicon_learn.LexiconLearnMixin):
    def __init__(self, config=None, storage=None):
        self.config = config or {}
        self._config_schema = {}
        self._storage = storage
        self._init_lexicon_learn()

    def _cfg(self, key, default=True, group_id=None):
        return bool(self.config.get(key, default))

    def _cfg_int(self, key, default=0, group_id=None):
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.config.get(key, default))

    def _get_group_override(self, group_id, key):
        return None


class ParseCandidatesTests(unittest.TestCase):
    def test_parses_array_with_surrounding_text(self):
        raw = ('好的，结果如下：[{"keyword":"AI中转","category":"ad","confidence":0.9,'
               '"reason":"多次售卖","sample":"低价AI中转"}] 以上。')
        out = lexicon_learn.LexiconLearnMixin._learn_parse_candidates(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["keyword"], "AI中转")
        self.assertEqual(out[0]["category"], "ad")
        self.assertAlmostEqual(out[0]["confidence"], 0.9)

    def test_clamps_confidence_and_skips_empty(self):
        raw = '[{"keyword":"x","confidence":5},{"keyword":"","confidence":0.5},{"nope":1}]'
        out = lexicon_learn.LexiconLearnMixin._learn_parse_candidates(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["confidence"], 1.0)

    def test_garbage_returns_empty(self):
        self.assertEqual(lexicon_learn.LexiconLearnMixin._learn_parse_candidates("not json"), [])
        self.assertEqual(lexicon_learn.LexiconLearnMixin._learn_parse_candidates(""), [])


class KeywordFilterTests(unittest.TestCase):
    def setUp(self):
        self.learn = _Learn()
        self.allow = self.learn._learn_allowlist("g1")

    def test_rejects_short_and_digits_and_allowlist(self):
        ok = lexicon_learn.LexiconLearnMixin._learn_keyword_ok
        self.assertFalse(ok("a", 2, self.allow))          # 太短
        self.assertFalse(ok("123456", 2, self.allow))     # 纯数字
        self.assertFalse(ok("qq", 2, self.allow))         # 内置白名单
        self.assertFalse(ok("x" * 50, 2, self.allow))     # 超长
        self.assertFalse(ok("!!", 2, self.allow))         # 无有效字符

    def test_accepts_distinctive_terms(self):
        ok = lexicon_learn.LexiconLearnMixin._learn_keyword_ok
        self.assertTrue(ok("AI中转", 2, self.allow))
        self.assertTrue(ok("plus日抛", 2, self.allow))

    def test_user_allowlist_extends_builtin(self):
        learn = _Learn(config={"lexicon_learn_allowlist": ["正常商品名"]})
        allow = learn._learn_allowlist("g1")
        ok = lexicon_learn.LexiconLearnMixin._learn_keyword_ok
        self.assertFalse(ok("正常商品名", 2, allow))


class MatcherTests(unittest.TestCase):
    def test_per_group_hit_and_category(self):
        learn = _Learn()
        learn._build_learn_matcher_from_rows("g1", [
            {"keyword": "AI中转", "category": "ad"},
            {"keyword": "傻大个骂人词", "category": "swear"},
        ])
        self.assertEqual(learn._learned_hit("g1", "谁要AI中转账号"), "ad")
        self.assertEqual(learn._learned_hit("g1", "你个傻大个骂人词"), "swear")
        self.assertIsNone(learn._learned_hit("g1", "正常聊天内容"))
        # 其它群不受影响
        self.assertIsNone(learn._learned_hit("g2", "谁要AI中转账号"))

    def test_swear_takes_priority(self):
        learn = _Learn()
        learn._build_learn_matcher_from_rows("g1", [
            {"keyword": "重叠词", "category": "ad"},
            {"keyword": "重叠", "category": "swear"},
        ])
        # 文本同时含两者时，swear 优先返回
        self.assertEqual(learn._learned_hit("g1", "这是重叠词"), "swear")


class ObserveBufferTests(unittest.TestCase):
    def test_observe_only_when_enabled(self):
        learn = _Learn(config={"lexicon_learn_enabled": False})
        learn._learn_observe_message("g1", "hi")
        self.assertNotIn("g1", learn._learn_buffers)
        learn.config["lexicon_learn_enabled"] = True
        learn._learn_observe_message("g1", "  卖AI中转  ")
        self.assertEqual(list(learn._learn_buffers["g1"]), ["卖AI中转"])
        learn._learn_observe_message("g1", "")  # 空文本忽略
        self.assertEqual(len(learn._learn_buffers["g1"]), 1)


class MultiDimApprovalTests(unittest.TestCase):
    def _make(self, verify_result=True, config=None):
        cfg = {"lexicon_learn_verify_llm": True}
        cfg.update(config or {})
        learn = _Learn(config=cfg)

        async def _fake_verify(group_id, keyword, category, sample):
            return verify_result
        learn._learn_verify_safe_to_ban = _fake_verify
        return learn

    def test_all_dims_pass(self):
        learn = self._make(verify_result=True)
        v = asyncio.run(learn._learn_multidim_approval(
            "g1", "AI中转", "ad", 0.9, "低价AI中转", occurrences=5,
            min_conf=0.75, min_occ=3, min_len=2))
        self.assertTrue(v["approved"])

    def test_low_confidence_blocks(self):
        learn = self._make(verify_result=True)
        v = asyncio.run(learn._learn_multidim_approval(
            "g1", "AI中转", "ad", 0.5, "x", occurrences=5,
            min_conf=0.75, min_occ=3, min_len=2))
        self.assertFalse(v["approved"])

    def test_low_occurrence_blocks(self):
        learn = self._make(verify_result=True)
        v = asyncio.run(learn._learn_multidim_approval(
            "g1", "AI中转", "ad", 0.9, "x", occurrences=1,
            min_conf=0.75, min_occ=3, min_len=2))
        self.assertFalse(v["approved"])

    def test_adversarial_verify_can_veto(self):
        learn = self._make(verify_result=False)
        v = asyncio.run(learn._learn_multidim_approval(
            "g1", "AI中转", "ad", 0.95, "x", occurrences=9,
            min_conf=0.75, min_occ=3, min_len=2))
        self.assertFalse(v["approved"])

    def test_verify_skipped_when_disabled(self):
        learn = self._make(verify_result=False, config={"lexicon_learn_verify_llm": False})
        v = asyncio.run(learn._learn_multidim_approval(
            "g1", "AI中转", "ad", 0.95, "x", occurrences=9,
            min_conf=0.75, min_occ=3, min_len=2))
        # 关闭对抗复核后，前四维通过即批准
        self.assertTrue(v["approved"])


class StorageCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = _MiniStore(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_upsert_increments_occurrences(self):
        r1 = self.store.upsert_learned_candidate("g1", "AI中转", "ad", "r", "s", 0.8, 100)
        self.assertEqual(r1["occurrences"], 1)
        self.assertEqual(r1["status"], "pending")
        r2 = self.store.upsert_learned_candidate("g1", "AI中转", "ad", "r2", "s2", 0.9, 200)
        self.assertEqual(r2["occurrences"], 2)

    def test_status_transition_and_approved_list(self):
        self.store.upsert_learned_candidate("g1", "AI中转", "ad", "r", "s", 0.8, 100)
        row = self.store.get_learned_keyword("g1", "AI中转")
        updated = self.store.set_learned_status(row["id"], "approved", 300)
        self.assertEqual(updated["status"], "approved")
        approved = self.store.list_approved_learned("g1")
        self.assertEqual([a["keyword"] for a in approved], ["AI中转"])

    def test_rejected_not_resurrected_by_upsert(self):
        self.store.upsert_learned_candidate("g1", "普通词", "ad", "r", "s", 0.8, 100)
        row = self.store.get_learned_keyword("g1", "普通词")
        self.store.set_learned_status(row["id"], "rejected", 300)
        # 再次挖到同词：occurrences 累计但 status 保持 rejected（不复活为 pending）
        info = self.store.upsert_learned_candidate("g1", "普通词", "ad", "r", "s", 0.99, 400)
        self.assertEqual(info["status"], "rejected")
        self.assertEqual(info["occurrences"], 2)

    def test_case_variants_merge_into_one_candidate(self):
        # 匹配器大小写不敏感，候选也必须按不区分大小写去重，否则出现次数被拆散
        self.store.upsert_learned_candidate("g1", "AI中转", "ad", "r", "s", 0.8, 100)
        info = self.store.upsert_learned_candidate("g1", "ai中转", "ad", "r", "s", 0.8, 200)
        self.assertEqual(info["occurrences"], 2)
        self.assertEqual(self.store.count_learned("g1", "pending"), 1)
        # 保留首次写入的原始写法，且任意大小写都能查到同一行
        rows = self.store.list_learned("g1")
        self.assertEqual(rows[0]["keyword"], "AI中转")
        self.assertIsNotNone(self.store.get_learned_keyword("g1", "Ai中转"))

    def test_delete_and_counts(self):
        self.store.upsert_learned_candidate("g1", "词A", "ad", "", "", 0.8, 100)
        self.store.upsert_learned_candidate("g1", "词B", "swear", "", "", 0.8, 100)
        self.assertEqual(self.store.count_learned("g1", "pending"), 2)
        row = self.store.get_learned_keyword("g1", "词A")
        deleted = self.store.delete_learned(row["id"])
        self.assertEqual(deleted["keyword"], "词A")
        self.assertEqual(self.store.count_learned("g1", "pending"), 1)


if __name__ == "__main__":
    unittest.main()
