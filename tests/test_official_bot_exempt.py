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
        self.assertTrue(is_bot("3889xxxxxxxx") if False else is_bot("3882345678"))
        self.assertTrue(is_bot("38912345678"))    # 11 位
        self.assertTrue(is_bot(2880542731))       # int 输入也接受

    def test_real_users_not_matched(self):
        is_bot = self.moderation.ModerationMixin._is_qq_official_bot_uid
        self.assertFalse(is_bot("1234567890"))    # 普通真实 QQ 号
        self.assertFalse(is_bot("28812345"))      # 8 位短号，不在号段
        self.assertFalse(is_bot("10000"))
        self.assertFalse(is_bot(""))
        self.assertFalse(is_bot(None))

    def test_pre_check_skips_official_bot_when_enabled(self):
        moderation = self.moderation
        mixin = moderation.ModerationMixin

        class FakePlugin:
            _user_white_set = set()
            _group_black_set = set()
            _group_white_set = set()
            _pre_check_message = mixin._pre_check_message
            _is_qq_official_bot_uid = staticmethod(mixin._is_qq_official_bot_uid)
            _cfg = lambda self, key, default=True, group_id=None: (
                True if key == "official_bot_exempt_enabled" else default
            )
            _should_scan_message = lambda self, event: True
            config = {"disclaimer_agreed": True}
            _config_schema = {}

        plugin = FakePlugin()
        # 开关开启：官方机器人号段直接跳过（返回 True=不进入审核管线）
        self.assertTrue(plugin._pre_check_message(None, "123456", "2880542731"))
        # 普通用户不受影响
        self.assertFalse(plugin._pre_check_message(None, "123456", "1234567890"))

        class DisabledPlugin(FakePlugin):
            _cfg = lambda self, key, default=True, group_id=None: (
                False if key == "official_bot_exempt_enabled" else default
            )

        # 开关关闭：号段命中也不豁免
        self.assertFalse(
            DisabledPlugin()._pre_check_message(None, "123456", "2880542731")
        )


if __name__ == "__main__":
    unittest.main()
