"""Issue #92 回归测试：非 OneBot 平台（QQ 官方机器人）的群管指令提示与管理员 ID 提示。"""

import asyncio
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
package = types.ModuleType("group_guardian_issue92_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")
commands = _load_module(f"{package.__name__}.commands", "commands.py")

OPENID = "35DDDD0AEE8A5CF6F0B6EBF0C45BBC1E"


class _ActionClient:
    async def call_action(self, action, **kwargs):
        if action == "get_group_member_info":
            return {"role": "member"}
        return None


class _Event:
    def __init__(self, platform="aiocqhttp", sender=OPENID, group="123456", bot=None,
                 message_str=""):
        self._platform = platform
        self._sender = sender
        self.group_id = group
        self.bot = bot
        self.message_str = message_str

    def get_platform_name(self):
        if isinstance(self._platform, Exception):
            raise self._platform
        return self._platform

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self.group_id

    def plain_result(self, text):
        return text


class _Storage:
    def is_group_admin_blocked(self, group_id, user_id):
        return False

    def is_group_super_admin(self, group_id, user_id):
        return False


class _Host(onebot.OneBotMixin):
    def __init__(self, admins=()):
        self._admins = list(admins)
        self.context = types.SimpleNamespace(astrbot_config={"admin_id": []})
        self._storage = _Storage()
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 10
        self._group_white_set = set()
        self._group_black_set = set()
        self._client = None
        self.is_admin_calls = 0

    def _get_admin_list(self):
        return list(self._admins)

    def _cfg(self, key, default=None, group_id=None):
        return default

    def _cfg_check(self, cfg_key, feature_name, group_id=None):
        return True, ""

    def _check_group_access(self, event):
        return True, ""

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def _is_admin(self, event):
        self.is_admin_calls += 1
        return await onebot.OneBotMixin._is_admin(self, event)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class PlatformDetectionTests(unittest.TestCase):
    def test_platform_name_normalized(self):
        host = _Host()
        self.assertEqual(host._event_platform_name(_Event(platform="QQ_Official ")), "qq_official")

    def test_platform_name_missing_or_invalid(self):
        host = _Host()
        self.assertEqual(host._event_platform_name(types.SimpleNamespace()), "")
        self.assertEqual(host._event_platform_name(_Event(platform=RuntimeError("boom"))), "")
        self.assertEqual(host._event_platform_name(_Event(platform=None)), "")
        self.assertEqual(host._event_platform_name(_Event(platform=object())), "")

    def test_platform_meta_fallback(self):
        host = _Host()
        event = types.SimpleNamespace(platform_meta=types.SimpleNamespace(name="qq_official"))
        self.assertEqual(host._event_platform_name(event), "qq_official")

    def test_qq_official_reports_unsupported(self):
        host = _Host()
        msg = host._onebot_unsupported_message(_Event(platform="qq_official", bot=object()))
        self.assertIn("qq_official", msg)
        self.assertIn("aiocqhttp", msg)

    def test_onebot_and_unknown_platforms_not_blocked(self):
        host = _Host()
        self.assertEqual(host._onebot_unsupported_message(_Event(platform="aiocqhttp")), "")
        self.assertEqual(host._onebot_unsupported_message(types.SimpleNamespace()), "")

    def test_custom_adapter_with_call_action_not_blocked(self):
        host = _Host()
        event = _Event(platform="my_onebot_bridge", bot=_ActionClient())
        self.assertEqual(host._onebot_unsupported_message(event), "")


class AdminDeniedMessageTests(unittest.TestCase):
    def test_numeric_qq_keeps_original_message(self):
        host = _Host()
        self.assertEqual(host._admin_denied_message(_Event(sender="3868975329"), "仅管理员可以使用此功能"),
                         "仅管理员可以使用此功能")

    def test_openid_is_shown_in_message(self):
        host = _Host()
        msg = host._admin_denied_message(_Event(sender=OPENID), "仅管理员可以使用此功能")
        self.assertTrue(msg.startswith("仅管理员可以使用此功能"))
        self.assertIn(OPENID, msg)

    def test_unknown_sender_keeps_original_message(self):
        host = _Host()
        self.assertEqual(host._admin_denied_message(_Event(sender=""), "x"), "x")


class CheckAdminCfgAccessTests(unittest.TestCase):
    def test_qq_official_short_circuits_before_admin_check(self):
        host = _Host(admins=["3868975329"])
        ok, msg = _run(host._check_admin_cfg_access(
            _Event(platform="qq_official", bot=object()), "whole_ban_enabled", "全体禁言"))
        self.assertFalse(ok)
        self.assertIn("qq_official", msg)
        self.assertEqual(host.is_admin_calls, 0)

    def test_qq_official_blocks_query_actions_too(self):
        host = _Host()
        ok, msg = _run(host._check_admin_cfg_access(
            _Event(platform="qq_official", bot=object()), "member_list_enabled", "查看群成员列表",
            need_admin=False))
        self.assertFalse(ok)
        self.assertIn("不支持", msg)

    def test_non_admin_openid_gets_id_hint(self):
        host = _Host(admins=["3868975329"])
        ok, msg = _run(host._check_admin_cfg_access(
            _Event(platform="aiocqhttp", sender="abc_user", bot=_ActionClient()), "ban_enabled", "禁言"))
        self.assertFalse(ok)
        self.assertIn("abc_user", msg)

    def test_numeric_non_admin_message_unchanged(self):
        host = _Host(admins=["3868975329"])
        ok, msg = _run(host._check_admin_cfg_access(
            _Event(platform="aiocqhttp", sender="10001", bot=_ActionClient()), "ban_enabled", "禁言"))
        self.assertFalse(ok)
        self.assertEqual(msg, "仅管理员可以使用此功能")

    def test_admin_on_onebot_passes(self):
        host = _Host(admins=["10001"])
        ok, msg = _run(host._check_admin_cfg_access(
            _Event(platform="aiocqhttp", sender="10001", bot=_ActionClient()), "ban_enabled", "禁言"))
        self.assertTrue(ok, msg)

    def test_openid_in_admin_list_is_recognized(self):
        host = _Host(admins=[OPENID])
        self.assertTrue(_run(host._is_plugin_admin(_Event(platform="qq_official", sender=OPENID))))


class CommandDenialTests(unittest.TestCase):
    def _collect(self, agen):
        async def run():
            return [item async for item in agen]
        return _run(run())

    def test_lexicon_command_denial_shows_openid(self):
        host = _Host(admins=["3868975329"])
        event = _Event(platform="qq_official", sender=OPENID, message_str="/添加违禁词 广告 x")
        out = self._collect(commands.CommandsMixin.cmd_add_rule_keyword(host, event))
        self.assertEqual(len(out), 1)
        self.assertIn("仅插件管理员可以管理违禁词", out[0])
        self.assertIn(OPENID, out[0])

    def test_no_bare_plugin_admin_denials_left(self):
        source = (ROOT / "commands.py").read_text(encoding="utf-8")
        self.assertNotIn('plain_result("仅插件管理员', source)
        self.assertNotIn('plain_result("仅群主或插件管理员', source)


if __name__ == "__main__":
    unittest.main()
