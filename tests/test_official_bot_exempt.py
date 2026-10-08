# -*- coding: utf-8 -*-
"""Issue #90：QQ 开放平台（官方）机器人消息豁免开关的回归测试。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault(
        "astrbot.api.event", types.ModuleType("astrbot.api.event")
    )
    event_module.AstrMessageEvent = object
    api.event = event_module
    filter_module = sys.modules.setdefault(
        "astrbot.api.event.filter", types.ModuleType("astrbot.api.event.filter")
    )
    event_module.filter = filter_module
    filter_module.event_message_type = lambda *a, **k: (lambda f: f)
    filter_module.platform_adapter_type = lambda *a, **k: (lambda f: f)
    filter_module.EventMessageType = types.SimpleNamespace(ALL="ALL")
    filter_module.PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="AIOCQHTTP")
    star_module = sys.modules.setdefault(
        "astrbot.api.star", types.ModuleType("astrbot.api.star")
    )
    star_module.Context = object
    star_module.Star = object
    star_module.register = lambda *a, **k: (lambda cls: cls)
    message_module = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    )
    message_module.AiocqhttpMessageEvent = object


def _load_moderation():
    _install_astrbot_stubs()
    spec = importlib.util.spec_from_file_location(
        "moderation_under_test", ROOT / "moderation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfficialBotUidTest(unittest.TestCase):
    """_is_qq_official_bot_uid 虚拟号段识别。"""

    @classmethod
    def setUpClass(cls):
        cls.moderation = _load_moderation()

    def test_official_bot_prefixes(self):
        is_bot = self.moderation.ModerationMixin._is_qq_official_bot_uid
        self.assertTrue(is_bot("2880542731"))     # 群聊场景官方机器人
        self.assertTrue(is_bot("3882345678"))
        self.assertTrue(is_bot("38912345678"))    # 11 位
        self.assertTrue(is_bot(2880542731))       # int 输入也接受

    def test_real_users_not_matched(self):
        is_bot = self.moderation.ModerationMixin._is_qq_official_bot_uid
        self.assertFalse(is_bot("1234567890"))    # 普通真实 QQ 号
        self.assertFalse(is_bot("28812345"))      # 8 位短号，不在号段
        self.assertFalse(is_bot("10000"))
        self.assertFalse(is_bot(""))
        self.assertFalse(is_bot(None))

    def test_pre_check_no_longer_decides_official_bot(self):
        """号段判定已移到异步的 _is_exempt_official_bot，_pre_check_message 不再按号段放行。"""
        mixin = self.moderation.ModerationMixin

        class FakePlugin:
            _user_white_set = set()
            _group_black_set = set()
            _group_white_set = set()
            _pre_check_message = mixin._pre_check_message
            _cfg = lambda self, key, default=True, group_id=None: (
                True if key == "official_bot_exempt_enabled" else default
            )
            _should_scan_message = lambda self, event: True
            config = {"disclaimer_agreed": True}

        self.assertFalse(FakePlugin()._pre_check_message(None, "123456", "2880542731"))


class _MemberInfoClient:
    def __init__(self, info=None, fail=False):
        self.info = info
        self.fail = fail
        self.calls = 0


def _make_plugin(moderation, client, enabled=True):
    mixin = moderation.ModerationMixin

    class FakePlugin:
        _is_qq_official_bot_uid = staticmethod(mixin._is_qq_official_bot_uid)
        _query_member_is_robot = mixin._query_member_is_robot
        _is_exempt_official_bot = mixin._is_exempt_official_bot
        _resolve_official_bot = mixin._resolve_official_bot

        def _cfg(self, key, default=None, group_id=None):
            if key == "official_bot_exempt_enabled":
                return enabled
            return default

        @staticmethod
        def _safe_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        async def _get_client(self, event=None):
            return client

        async def _call_group_api_result(self, cli, action, result_name="", **kwargs):
            cli.calls += 1
            if cli.fail:
                return False, None, "timeout"
            return True, cli.info, ""

    return FakePlugin()


class OfficialBotExemptTest(unittest.IsolatedAsyncioTestCase):
    """_is_exempt_official_bot：协议端 is_robot 优先，号段仅作兜底。"""

    @classmethod
    def setUpClass(cls):
        cls.moderation = _load_moderation()

    async def test_is_robot_true_exempts_any_uid(self):
        client = _MemberInfoClient({"role": "member", "is_robot": True})
        plugin = _make_plugin(self.moderation, client)
        self.assertTrue(await plugin._is_exempt_official_bot(None, "123456", "1234567890"))

    async def test_real_user_in_bot_number_range_not_exempt(self):
        # 10 位 388/389 号段同样分配给真实账号：协议端明确 is_robot=false 时不得豁免
        client = _MemberInfoClient({"role": "member", "is_robot": False})
        plugin = _make_plugin(self.moderation, client)
        self.assertFalse(await plugin._is_exempt_official_bot(None, "123456", "3882345678"))

    async def test_missing_field_falls_back_to_number_range(self):
        client = _MemberInfoClient({"role": "member"})
        plugin = _make_plugin(self.moderation, client)
        self.assertTrue(await plugin._is_exempt_official_bot(None, "123456", "2880542731"))
        self.assertFalse(await plugin._is_exempt_official_bot(None, "123456", "1234567890"))
        self.assertEqual(
            plugin._official_bot_cache["123456:2880542731"][2],
            self.moderation.OFFICIAL_BOT_CACHE_TTL,
        )

    async def test_lookup_failure_uses_fallback_with_short_cache(self):
        client = _MemberInfoClient(fail=True)
        plugin = _make_plugin(self.moderation, client)
        self.assertTrue(await plugin._is_exempt_official_bot(None, "123456", "2880542731"))
        self.assertEqual(
            plugin._official_bot_cache["123456:2880542731"][2],
            self.moderation.OFFICIAL_BOT_RETRY_TTL,
        )

    async def test_disabled_switch_skips_lookup(self):
        client = _MemberInfoClient({"is_robot": True})
        plugin = _make_plugin(self.moderation, client, enabled=False)
        self.assertFalse(await plugin._is_exempt_official_bot(None, "123456", "2880542731"))
        self.assertEqual(client.calls, 0)

    async def test_result_is_cached(self):
        client = _MemberInfoClient({"is_robot": True})
        plugin = _make_plugin(self.moderation, client)
        for _ in range(3):
            self.assertTrue(await plugin._is_exempt_official_bot(None, "123456", "2880542731"))
        self.assertEqual(client.calls, 1)

    async def test_string_flag_values(self):
        plugin = _make_plugin(self.moderation, _MemberInfoClient({"is_robot": "true"}))
        self.assertTrue(await plugin._is_exempt_official_bot(None, "1", "10001"))
        plugin = _make_plugin(self.moderation, _MemberInfoClient({"is_robot": "0"}))
        self.assertFalse(await plugin._is_exempt_official_bot(None, "1", "2880542731"))

    async def test_non_numeric_ids_do_not_query(self):
        client = _MemberInfoClient({"is_robot": True})
        plugin = _make_plugin(self.moderation, client)
        self.assertFalse(await plugin._is_exempt_official_bot(None, "123456", "abc"))
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
