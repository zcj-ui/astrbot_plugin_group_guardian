"""Issue #89 回归测试：机器人非管理员检测、撤回结果校验、链接白名单。"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    if not hasattr(aiohttp, "ClientSession"):
        aiohttp.ClientSession = lambda: None
    if not hasattr(aiohttp, "ClientTimeout"):
        aiohttp.ClientTimeout = lambda **kwargs: types.SimpleNamespace(**kwargs)
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    platform = sys.modules.setdefault("astrbot.core.platform", types.ModuleType("astrbot.core.platform"))
    sources = sys.modules.setdefault(
        "astrbot.core.platform.sources", types.ModuleType("astrbot.core.platform.sources"))
    aiocqhttp = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp"))
    aio_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"))
    aio_event.AiocqhttpMessageEvent = object
    astrbot.core = core
    core.platform = platform
    platform.sources = sources
    sources.aiocqhttp = aiocqhttp
    aiocqhttp.aiocqhttp_message_event = aio_event


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
package = types.ModuleType("group_guardian_issue89_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")
link_whitelist = _load_module(f"{package.__name__}.link_whitelist", "link_whitelist.py")
moderation = _load_module(f"{package.__name__}.moderation", "moderation.py")
web = _load_module(f"{package.__name__}.web", "web.py")
card_monitor = _load_module(f"{package.__name__}.card_monitor", "card_monitor.py")


class _RoleClient:
    """按 action 分派的 OneBot 客户端桩。"""

    def __init__(self, role="member", role_fails=False, delete_result=None):
        self.role = role
        self.role_fails = role_fails
        self.delete_result = delete_result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append(action)
        if action == "get_login_info":
            return {"user_id": 999}
        if action == "get_group_member_info":
            if self.role_fails:
                return {"status": "failed", "retcode": 100, "message": "denied"}
            return {"role": self.role, "user_id": kwargs.get("user_id")}
        if action == "delete_msg":
            if isinstance(self.delete_result, BaseException):
                raise self.delete_result
            return self.delete_result
        return None


class _OneBotHarness(onebot.OneBotMixin, utilities.UtilitiesMixin):
    def __init__(self, client, require_admin=True):
        self.client = client
        self._client = client
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 10
        self._bot_uin_cache = 0
        self.require_admin = require_admin

    async def _get_client(self, event=None):
        return self.client

    def _cfg(self, key, default=True, group_id=None):
        if key == "moderation_require_bot_admin":
            return self.require_admin
        return default


class RecallResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_success_returns_true(self):
        harness = _OneBotHarness(_RoleClient(delete_result=None))
        self.assertIs(await harness._recall_msg(object(), "123"), True)

    async def test_recall_rejected_packet_returns_false(self):
        client = _RoleClient(delete_result={"status": "failed", "retcode": 100, "message": "no perm"})
        harness = _OneBotHarness(client)
        self.assertIs(await harness._recall_msg(object(), "123"), False)

    async def test_recall_exception_returns_false(self):
        harness = _OneBotHarness(_RoleClient(delete_result=RuntimeError("ActionFailed")))
        self.assertIs(await harness._recall_msg(object(), "123"), False)

    async def test_recall_without_message_id_is_not_attempted(self):
        client = _RoleClient()
        harness = _OneBotHarness(client)
        self.assertIsNone(await harness._recall_msg(object(), ""))
        self.assertNotIn("delete_msg", client.calls)


class BotRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_bot_cannot_moderate(self):
        harness = _OneBotHarness(_RoleClient(role="member"))
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))

    async def test_admin_and_owner_bot_can_moderate(self):
        for role in ("admin", "owner"):
            with self.subTest(role=role):
                harness = _OneBotHarness(_RoleClient(role=role))
                self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_role_query_failure_does_not_block_moderation(self):
        harness = _OneBotHarness(_RoleClient(role_fails=True))
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_switch_off_skips_query_entirely(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client, require_admin=False)
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))
        self.assertEqual(client.calls, [])

    async def test_role_is_cached_per_group(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client)
        for _ in range(5):
            await harness._bot_can_moderate(object(), "100")
        self.assertEqual(client.calls.count("get_group_member_info"), 1)

    async def test_invalidate_forces_requery_after_promotion(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client)
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))
        client.role = "admin"
        # 缓存未失效前仍是旧值
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))
        harness._invalidate_bot_role("100")
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_failed_lookup_uses_short_ttl(self):
        client = _RoleClient(role_fails=True)
        harness = _OneBotHarness(client)
        await harness._get_bot_group_role(object(), "100")
        role, _ts, ttl = harness._bot_role_cache["100"]
        self.assertEqual(role, "")
        self.assertEqual(ttl, harness.BOT_ROLE_FAIL_TTL)
        self.assertLess(harness.BOT_ROLE_FAIL_TTL, harness.BOT_ROLE_CACHE_TTL)


class _Event:
    def __init__(self, message_id="555"):
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.stopped = False

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class _PenaltyHarness(moderation.ModerationMixin):
    def __init__(self, recall_result, mute_ok=False):
        self.recall_result = recall_result
        self.mute_ok = mute_ok
        self.logs = []

    async def _recall_msg(self, event, msg_id):
        return self.recall_result

    async def _recall_extra_messages(self, event, ids):
        return None

    @staticmethod
    def _moderation_in_penalty_cooldown(_g, _u):
        return False

    @staticmethod
    def _anti_flood_in_cooldown(_g, _u):
        return False

    def _mark_moderation_penalty(self, *_a):
        return None

    def _clear_moderation_penalty(self, *_a):
        return None

    def _schedule_unban(self, *_a):
        return None

    async def _mute_member(self, event, duration):
        return self.mute_ok

    def _log_moderation(self, *args):
        self.logs.append(args)

    @staticmethod
    def _cfg(name, default=True, group_id=""):
        return default

    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default

    @staticmethod
    def _cfg_str(name, default="", group_id=""):
        return default


async def _drain(gen):
    return [item async for item in gen]


class PenaltyNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_penalty_does_not_claim_recall_when_recall_rejected(self):
        harness = _PenaltyHarness(recall_result=False)
        event = _Event()
        notices = await _drain(harness._execute_llm_penalty(
            event, "1", "2", "tester", "text", "广告", "ad", [], []))
        self.assertEqual(notices, [])
        action = harness.logs[0][4]
        self.assertNotIn("撤回", action)  # 统计把含「撤回」计为已拦截
        self.assertTrue(event.stopped)

    async def test_llm_penalty_announces_when_recall_succeeds(self):
        harness = _PenaltyHarness(recall_result=True)
        notices = await _drain(harness._execute_llm_penalty(
            _Event(), "1", "2", "tester", "text", "广告", "ad", [], []))
        self.assertEqual(len(notices), 1)
        self.assertEqual(harness.logs[0][4], "LLM撤回")

    async def test_llm_penalty_treats_unknown_recall_result_as_success(self):
        # 旧测试桩与未返回结果的调用方返回 None，保持原行为
        harness = _PenaltyHarness(recall_result=None)
        notices = await _drain(harness._execute_llm_penalty(
            _Event(), "1", "2", "tester", "text", "广告", "ad", [], []))
        self.assertEqual(len(notices), 1)

    async def test_rule_penalty_logs_failed_recall_without_recall_word(self):
        harness = _PenaltyHarness(recall_result=False, mute_ok=False)
        await _drain(harness._execute_rule_penalty(
            _Event(), "1", "2", "tester", "text", {"ad": True}, [], []))
        self.assertNotIn("撤回", harness.logs[0][4])


class LinkWhitelistTests(unittest.TestCase):
    def setUp(self):
        self.wl = link_whitelist.normalize_whitelist(
            ["github.com", "https://www.bilibili.com/video/", "*.qq.com", "no dot", "x"])

    def test_normalization(self):
        self.assertEqual(self.wl, ("github.com", "www.bilibili.com", "qq.com"))

    def test_whitelisted_links_are_stripped(self):
        strip = link_whitelist.strip_whitelisted_links
        for text in ("看 https://github.com/a/b", "docs.github.com/x", "im.qq.com",
                     "https://www.bilibili.com/video/BV1"):
            with self.subTest(text=text):
                out = strip(text, self.wl)
                self.assertNotIn("github", out)
                self.assertNotIn("bilibili", out)
                self.assertNotIn("qq.com", out)

    def test_bypass_attempts_are_not_whitelisted(self):
        strip = link_whitelist.strip_whitelisted_links
        for text in ("github.com.evil.com/x", "evilgithub.com",
                     "https://evil.com/?r=github.com", "m.bilibili.com"):
            with self.subTest(text=text):
                self.assertEqual(strip(text, self.wl), text)

    def test_userinfo_trick_keeps_real_host(self):
        out = link_whitelist.strip_whitelisted_links("https://github.com@evil.com/x", self.wl)
        self.assertIn("evil.com", out)

    def test_other_content_is_preserved(self):
        out = link_whitelist.strip_whitelisted_links("github.com 还有 taobao.com/abc", self.wl)
        self.assertIn("taobao.com/abc", out)


class _Matcher:
    def __init__(self, needle):
        self.needle = needle

    def is_match(self, text):
        return self.needle in text


class _ScreeningHarness(moderation.ModerationMixin):
    def __init__(self, whitelist):
        self.config = {"link_whitelist": whitelist}
        self._ad_matcher = _Matcher("github.com")
        self._swear_matcher = _Matcher("\x00never")

    @staticmethod
    def _cfg(name, default=True, group_id=None):
        return default

    @staticmethod
    def _lexicon_switch_map(group_id=None):
        return {}

    @staticmethod
    def _check_lexicon(text):
        return {}


class ScreeningIntegrationTests(unittest.TestCase):
    def test_whitelisted_link_no_longer_hits_ad_rule(self):
        self.assertTrue(_ScreeningHarness([])._initial_screening("来 github.com/x", "1")["ad"])
        self.assertFalse(
            _ScreeningHarness(["github.com"])._initial_screening("来 github.com/x", "1")["ad"])

    def test_whitelist_cache_follows_config_changes(self):
        harness = _ScreeningHarness(["github.com"])
        self.assertEqual(harness._link_whitelist(), ("github.com",))
        harness.config["link_whitelist"] = ["gitee.com"]
        self.assertEqual(harness._link_whitelist(), ("gitee.com",))


class _HangClient:
    async def call_action(self, action, **kwargs):
        await asyncio.Event().wait()


class CheckedCallTests(unittest.IsolatedAsyncioTestCase):
    """_call_action_checked：查询类指令不再把失败包当空列表、也不再无限挂起。"""

    async def test_success_returns_raw_result(self):
        client = _RoleClient()
        client.delete_result = {"messages": [1, 2]}
        harness = _OneBotHarness(client)
        self.assertEqual(
            await harness._call_action_checked(client, "delete_msg", message_id=1),
            {"messages": [1, 2]})

    async def test_failed_packet_raises(self):
        client = _RoleClient(delete_result={"status": "failed", "retcode": 100, "message": "denied"})
        harness = _OneBotHarness(client)
        with self.assertRaises(RuntimeError):
            await harness._call_action_checked(client, "delete_msg", message_id=1)

    async def test_hang_times_out(self):
        harness = _OneBotHarness(_HangClient())
        original = onebot.ONEBOT_CALL_TIMEOUT
        onebot.ONEBOT_CALL_TIMEOUT = 0.05
        try:
            with self.assertRaises(RuntimeError) as ctx:
                await harness._call_action_checked(_HangClient(), "get_group_member_list", "成员列表")
        finally:
            onebot.ONEBOT_CALL_TIMEOUT = original
        self.assertIn("超时", str(ctx.exception))


class RegexProbeTests(unittest.TestCase):
    """结构黑名单漏掉的分支重叠型回溯，由子进程限时探测拦截。"""

    def test_alternation_backtracking_is_rejected(self):
        self.assertFalse(web.WebMixin._is_redos_prone("(a|ab|b)+c"))  # 结构检查确实漏掉
        self.assertFalse(web.WebMixin._regex_runtime_safe("(a|ab|b)+c"))

    def test_legitimate_rules_pass(self):
        for pattern in ("(微信|vx)+", r"\d{5,11}", "免费领.{0,6}皮肤", "代[练刷]|接单"):
            with self.subTest(pattern=pattern):
                self.assertTrue(web.WebMixin._regex_runtime_safe(pattern))


class _ClampHarness(utilities.UtilitiesMixin):
    pass


class ClampTests(unittest.TestCase):
    def test_negative_limit_cannot_become_unlimited(self):
        # SQLite 把 LIMIT -1 视为不限制；此前只做了上界 min()
        clamp = _ClampHarness()._clamp_int
        self.assertEqual(clamp("-1", 200, 1, 1000), 1)
        self.assertEqual(clamp("abc", 30, 1, 365), 30)
        self.assertEqual(clamp("99999", 10, 1, 50), 50)


class CardPendingTests(unittest.IsolatedAsyncioTestCase):
    # 状态初始化会创建 asyncio.Lock，Python 3.8 要求在运行中的事件循环里创建
    async def test_drop_pending_for_group_only_touches_that_group(self):
        mixin = card_monitor.CardMonitorMixin()
        mixin._mark_card_pending("1", "a")
        mixin._mark_card_pending("1", "b")
        mixin._mark_card_pending("2", "c")
        mixin._drop_card_pending_for_group("1")
        self.assertEqual(mixin._card_pending_members, {("2", "c")})
        self.assertEqual(set(mixin._card_pending_misses), {("2", "c")})


if __name__ == "__main__":
    unittest.main()
