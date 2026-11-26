"""Issue #93/#94/#95 回归测试：处罚冷却与解禁、入群精确匹配与去重、规则跨分页批量操作。"""

import asyncio
import importlib.util
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_membership_api_result as mtest  # noqa: E402  复用入群审核测试桩

membership = mtest.membership


# ---------------------------------------------------------------- #94


class _JoinHarness(mtest._Harness):
    def __init__(self, accept=(), reject=(), overrides=True):
        super().__init__()
        self.config["join_accept_keywords"] = list(accept)
        self.config["join_reject_keywords"] = list(reject)
        self.cfg_values["join_accept_overrides_lexicon"] = overrides
        self.cfg_values["join_audit_notify"] = True


def _join_event(answer, flag="flag-1"):
    client = mtest._Client({"status": "ok", "retcode": 0, "data": None})
    event = mtest._Event(client)
    event.raw_event["comment"] = f"问题：说出任意想下载的游戏\n答案：{answer}"
    event.raw_event["flag"] = flag
    return event, client


def _approve_flags(client):
    return [kw.get("approve") for action, kw in client.calls if action == "set_group_add_request"]


class JoinKeywordMatchTests(unittest.TestCase):
    def test_contains_and_exact(self):
        hit = membership.MembershipMixin._join_keyword_hit
        self.assertTrue(hit("1", "纪元1800"))           # 包含匹配保持原行为
        self.assertFalse(hit("=1", "纪元1800"))         # 精确匹配不误伤
        self.assertTrue(hit("=1", "1"))
        self.assertTrue(hit("=1", "  1 "))
        self.assertTrue(hit("=abc", "abc"))
        self.assertFalse(hit("=", "anything"))          # 只有 = 视为空词
        self.assertFalse(hit("", "x"))
        self.assertFalse(hit(None, "x"))


class JoinRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_reject_does_not_hit_longer_answer(self):
        harness = _JoinHarness(accept=["纪元"], reject=["=1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_reject_hits_bare_answer(self):
        harness = _JoinHarness(reject=["=1"])
        event, client = _join_event("1")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])
        self.assertIn("=1", harness.logs[0][-2])

    async def test_exact_accept_wins_over_reject_when_priority_on(self):
        harness = _JoinHarness(accept=["=纪元1800"], reject=["1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_accept_wins_regardless_of_keyword_order(self):
        harness = _JoinHarness(accept=["纪元", "=纪元1800"], reject=["1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_accept_respects_priority_switch(self):
        harness = _JoinHarness(accept=["=纪元1800"], reject=["1"], overrides=False)
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])

    async def test_contains_accept_cannot_bypass_reject(self):
        # 在广告后附带一个通过词不能绕过拒绝词
        harness = _JoinHarness(accept=["原神"], reject=["加v"])
        event, client = _join_event("原神 加v领福利")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])

    async def test_duplicate_request_processed_once(self):
        harness = _JoinHarness(reject=["=1"])
        event, client = _join_event("1", flag="dup")
        self.assertTrue(await harness._handle_group_request(event))
        event2, client2 = _join_event("1", flag="dup")
        self.assertTrue(await harness._handle_group_request(event2))
        self.assertEqual(len(harness.notifications), 1)
        self.assertEqual(_approve_flags(client2), [])

    async def test_unhandled_request_can_be_retried(self):
        harness = _JoinHarness()  # 不命中任何词，默认转人工
        event, _ = _join_event("随便", flag="manual")
        self.assertFalse(await harness._handle_group_request(event))
        self.assertNotIn("manual", harness._join_request_seen_flags)


# ---------------------------------------------------------------- #93


def _install_moderation():
    package = types.ModuleType("group_guardian_issue93_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.moderation", ROOT / "moderation.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.anti_flood", ROOT / "anti_flood.py")
    flood = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = flood
    spec.loader.exec_module(flood)
    return module, flood


moderation, anti_flood = _install_moderation()


class _PenaltyHost(moderation.ModerationMixin, anti_flood.AntiFloodMixin):
    def __init__(self):
        self._anti_flood_penalty_until = {}
        self._anti_flood_data = {}


class PenaltyCooldownTests(unittest.TestCase):
    def test_cooldown_still_covers_mute_duration(self):
        # LLM 结论可能迟到数分钟：冷却仍覆盖整段禁言，防止重复禁言/较短禁言覆盖较长禁言
        host = _PenaltyHost()
        host._mark_moderation_penalty("1", "2", 1800)
        until = host._moderation_penalty_until["1"]["2"]
        self.assertGreater(until - time.time(), 1700)
        self.assertTrue(host._moderation_in_penalty_cooldown("1", "2"))

    def test_unban_clears_both_cooldowns(self):
        host = _PenaltyHost()
        host._mark_moderation_penalty("1", "2", 1800)
        host._mark_anti_flood_penalty("1", "2", 30)
        host._mark_moderation_penalty("1", "3", 1800)
        host._on_member_unbanned(1, 2)  # 通知里的群号/QQ 号是 int
        self.assertFalse(host._moderation_in_penalty_cooldown("1", "2"))
        self.assertFalse(host._anti_flood_in_cooldown("1", "2"))
        self.assertTrue(host._moderation_in_penalty_cooldown("1", "3"))

    def test_unban_with_missing_ids_is_noop(self):
        host = _PenaltyHost()
        host._on_member_unbanned(None, "2")
        host._on_member_unbanned("1", "")

    def test_plugin_unban_paths_clear_cooldown(self):
        for rel in ("onebot.py", "commands.py", "llm_tools.py"):
            self.assertIn("_on_member_unbanned", (ROOT / rel).read_text(encoding="utf-8"), rel)

    def test_lift_ban_notice_handler_registered(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('raw.get("notice_type") == "group_ban"', source)
        self.assertIn('raw.get("sub_type") == "lift_ban"', source)
        self.assertIn("runtime._on_member_unbanned(", source)


# ---------------------------------------------------------------- #95


def _load_storage():
    package = types.ModuleType("group_guardian_issue95_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.storage", ROOT / "storage.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RuleBatchStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage_mod = _load_storage()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = self.storage_mod.SQLiteStorage(Path(self.tmp.name), str(Path(self.tmp.name) / "plugin"))
        if hasattr(self.st, "initialize"):
            self.st.initialize()
        with self.st._connect() as conn:
            conn.execute("DELETE FROM moderation_rules")
            conn.commit()
        self.swear_ids = [self.st.save_moderation_rule("swear", f"bad{i}", "auto:x", True) for i in range(5)]
        self.ad_id = self.st.save_moderation_rule("ad", "spam", "", True)

    def tearDown(self):
        self.tmp.cleanup()

    def _enabled(self, category):
        return self.st.count_moderation_rules_filtered(category, 1, "")

    def test_filtered_toggle_spans_all_pages(self):
        changed = self.st.toggle_moderation_rules_filtered("swear", None, "", False)
        self.assertEqual(changed, 5)
        self.assertEqual(self._enabled("swear"), 0)
        self.assertEqual(self._enabled("ad"), 1)

    def test_filtered_toggle_respects_search(self):
        self.st.toggle_moderation_rules_filtered("swear", None, "bad1", False)
        self.assertEqual(self._enabled("swear"), 4)

    def test_filtered_delete_respects_status(self):
        self.st.toggle_moderation_rules([self.swear_ids[0]], False, "swear")
        deleted = self.st.delete_moderation_rules_filtered("swear", 0, "")
        self.assertEqual(deleted, 1)
        self.assertEqual(self.st.count_moderation_rules_filtered("swear", None, ""), 4)

    def test_filtered_ops_require_category(self):
        self.assertEqual(self.st.toggle_moderation_rules_filtered("", None, "", False), 0)
        self.assertEqual(self.st.delete_moderation_rules_filtered("", None, ""), 0)
        self.assertEqual(self._enabled("ad"), 1)

    def test_id_batch_ignores_other_category(self):
        changed = self.st.toggle_moderation_rules([self.swear_ids[0], self.ad_id], False, "swear")
        self.assertEqual(changed, 1)
        self.assertEqual(self._enabled("ad"), 1)
        deleted = self.st.delete_moderation_rules([self.ad_id], "swear")
        self.assertEqual(deleted, 0)


if __name__ == "__main__":
    unittest.main()
