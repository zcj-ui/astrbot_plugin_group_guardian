"""Exercise the real Main prompt hook and canonical core-admin adapter."""
import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "group_guardian_runtime_context_tests"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
api.logger = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None, exception=lambda *a, **k: None)
event_module = types.ModuleType("astrbot.api.event")
event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
decorator = lambda *a, **k: (lambda fn: fn)
event_module.filter = types.SimpleNamespace(
    on_llm_request=decorator, command=decorator, event_message_type=decorator,
    llm_tool=decorator, permission_type=decorator, platform_adapter_type=decorator,
    EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1, ALL=2, PRIVATE_MESSAGE=3),
    PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP=1),
)
api.event = event_module
api.__path__ = []
sys.modules[event_module.__name__] = event_module
astrbot.api = api
provider = types.ModuleType("astrbot.api.provider")
provider.ProviderRequest = type("ProviderRequest", (), {})
sys.modules[provider.__name__] = provider
star_mod = types.ModuleType("astrbot.api.star")
star_mod.Context = type("Context", (), {})
star_mod.Star = type("Star", (), {})
star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda: Path("."))
star_mod.register = decorator
sys.modules[star_mod.__name__] = star_mod

def _stub_module(modname, clsname, cls):
    mod = types.ModuleType(f"{PKG}.{modname}")
    setattr(mod, clsname, cls)
    sys.modules[mod.__name__] = mod

package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
automaton = types.ModuleType(f"{PKG}.automaton")
automaton.KeywordAutomaton = object
automaton.HybridMatcher = object
sys.modules[automaton.__name__] = automaton

core = types.ModuleType("astrbot.core")
agent = types.ModuleType("astrbot.core.agent")
message = types.ModuleType("astrbot.core.agent.message")
class TextPart:
    def __init__(self, text): self.text, self.temporary = text, False
    def mark_as_temp(self): self.temporary = True; return self
message.TextPart = TextPart
config_pkg = types.ModuleType("astrbot.core.config")
config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
config_mod.AstrBotConfig = dict
for name, mod in (("astrbot.core", core), ("astrbot.core.agent", agent),
                  ("astrbot.core.agent.message", message), ("astrbot.core.config", config_pkg),
                  ("astrbot.core.config.astrbot_config", config_mod)):
    sys.modules[name] = mod
core.agent, core.config = agent, config_pkg
agent.message = message
config_pkg.astrbot_config = config_mod
platform = types.ModuleType("astrbot.core.platform")
sources = types.ModuleType("astrbot.core.platform.sources")
aio = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
aio_event = types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
aio_event.AiocqhttpMessageEvent = type("AiocqhttpMessageEvent", (), {})
for name, mod in (("astrbot.core.platform", platform), ("astrbot.core.platform.sources", sources),
                  ("astrbot.core.platform.sources.aiocqhttp", aio),
                  ("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event", aio_event)):
    sys.modules[name] = mod
platform.sources, sources.aiocqhttp = sources, aio
aio.aiocqhttp_message_event = aio_event

_load(f"{PKG}.single_flight", "single_flight.py")
utils = _load(f"{PKG}.utils", "utils.py")
onebot = _load(f"{PKG}.onebot", "onebot.py")
class EmptyMixin: pass
for modname, clsname in (("anti_flood", "AntiFloodMixin"), ("appeal", "AppealMixin"),
                         ("card_monitor", "CardMonitorMixin"), ("commands", "CommandsMixin"),
                         ("lexicon_learn", "LexiconLearnMixin"), ("llm_tools", "LlmToolsMixin"),
                         ("membership", "MembershipMixin"), ("moderation", "ModerationMixin"),
                         ("moderation_review", "ModerationReviewMixin"), ("remote", "RemoteMixin"),
                         ("scheduler", "SchedulerMixin"), ("web", "WebMixin")):
    _stub_module(modname, clsname, type(clsname, (), {}))
constants = types.ModuleType(f"{PKG}.constants")
constants.PLUGIN_NAME, constants.PLUGIN_VERSION = "test", "1"
sys.modules[constants.__name__] = constants
storage = types.ModuleType(f"{PKG}.storage")
storage.SQLiteStorage = object
sys.modules[storage.__name__] = storage
_stub_module("utils_proxy", "Unused", EmptyMixin)
sys.modules[f"{PKG}.utils"] = utils
sys.modules[f"{PKG}.onebot"] = onebot
main = _load(f"{PKG}.main", "main.py")


class ActionClient:
    def __init__(self, role="member", delay=0, fail=False):
        self.role, self.delay, self.fail = role, delay, fail
        self.calls = []
    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.delay: await asyncio.sleep(self.delay)
        if self.fail: raise RuntimeError("protocol down")
        if action == "get_login_info": return {"user_id": 200}
        if action == "get_group_member_info": return {"role": self.role}

class BadActionClient:
    call_action = None


class Event:
    def __init__(self, role="member", platform="aiocqhttp", identity=200, bot=None):
        self.role, self.platform, self.identity, self.bot = role, platform, identity, bot
        self.group_id = "100"
    def get_platform_name(self): return self.platform
    def get_self_id(self): return self.identity
    def get_sender_id(self): return "sender"
    def get_group_id(self): return self.group_id


class RuntimePromptTests(unittest.IsolatedAsyncioTestCase):
    def host(self, role="member", identity=200, platform="aiocqhttp", bot=None):
        h = object.__new__(main.Main)
        h._admin_role_cache, h._bot_role_cache = {}, {"100": (role, time.time(), 300)}
        h._bot_uin_cache, h.config = 0, {"disclaimer_agreed": True}
        h._group_white_set, h._group_black_set = set(), set()
        h._storage = types.SimpleNamespace(is_group_admin_blocked=lambda *a: False)
        h.context = types.SimpleNamespace()
        h._cfg = lambda key, default=True: default if key != "enabled" else True
        h._check_group_access = lambda event: (True, "")
        h._try_get_sender_id = lambda event: event.get_sender_id()
        h._get_group_id = lambda event: event.group_id
        h._is_admin = lambda event: asyncio.sleep(0, result=False)
        h._is_plugin_admin = lambda event: asyncio.sleep(0, result=False)
        return h, Event(role, platform, identity, bot)

    async def test_real_main_hook_separates_sender_and_bot_facts(self):
        h, ev = self.host("member", 200)
        h._is_admin = lambda event: asyncio.sleep(0, result=True)
        h._is_plugin_admin = lambda event: asyncio.sleep(0, result=False)
        req = types.SimpleNamespace(extra_user_content_parts=["existing"])
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        text = req.extra_user_content_parts[-1].text
        self.assertIn("当前发送者在本群是否具备有效群管权限: 是", text)
        self.assertIn("当前发送者是否为插件/ AstrBot 全局管理员（本群黑名单规则已计入）: 否", text)
        self.assertIn("机器人自身 ID: 200", text)
        self.assertIn("机器人在本群最近确认的 QQ 角色: member", text)
        self.assertTrue(req.extra_user_content_parts[-1].temporary)

    async def test_cached_role_is_fresh_only_and_unknown_platform_stays_unknown(self):
        for role in ("owner", "admin", "member"):
            h, ev = self.host(role)
            req = types.SimpleNamespace(extra_user_content_parts=[])
            await main.Main.inject_group_guardian_prompt(h, ev, req)
            self.assertIn(f"机器人在本群最近确认的 QQ 角色: {role}", req.extra_user_content_parts[0].text)
        h, ev = self.host("owner", platform="qq_official")
        req = types.SimpleNamespace(extra_user_content_parts=[])
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertIn("接口状态: 不支持 OneBot 管理接口", req.extra_user_content_parts[0].text)
        self.assertIn("机器人在本群最近确认的 QQ 角色: 未知", req.extra_user_content_parts[0].text)
        h, ev = self.host("owner", platform="custom", bot=BadActionClient())
        req = types.SimpleNamespace(extra_user_content_parts=[])
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertIn("接口状态: 不支持 OneBot 管理接口（custom）", req.extra_user_content_parts[0].text)
        h, ev = self.host("admin", platform="custom", bot=ActionClient("member"))
        req = types.SimpleNamespace(extra_user_content_parts=[])
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertIn("接口状态: OneBot/兼容管理接口可用", req.extra_user_content_parts[0].text)
        h, ev = self.host("owner", platform="")
        req = types.SimpleNamespace(extra_user_content_parts=[])
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertIn("接口状态: 平台/接口未知", req.extra_user_content_parts[0].text)

    async def test_gates_and_system_prompt_fallback_preserve_existing_content(self):
        h, ev = self.host()
        h._cfg = lambda key, default=True: False if key == "enabled" else default
        req = types.SimpleNamespace(extra_user_content_parts=None, system_prompt="existing")
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertEqual(req.system_prompt, "existing")
        h._cfg = lambda key, default=True: default
        req = types.SimpleNamespace(extra_user_content_parts=None, system_prompt="existing")
        await main.Main.inject_group_guardian_prompt(h, ev, req)
        self.assertTrue(req.system_prompt.startswith("existing\n\n<group_guardian_runtime>"))
        self.assertEqual(h._bot_role_cache["100"][0], "member")  # fresh cache satisfies lookup

    async def test_expired_role_timeout_and_protocol_error_are_unknown(self):
        old_wait = onebot.OneBotMixin.BOT_ROLE_HOT_WAIT
        onebot.OneBotMixin.BOT_ROLE_HOT_WAIT = 0.005
        try:
            client = ActionClient("admin", delay=0.05)
            h, ev = self.host("owner", 200, bot=client)
            h._bot_role_cache["100"] = ("owner", time.time() - 400, 300)
            h._bot_uin_cache = 200
            req = types.SimpleNamespace(extra_user_content_parts=[])
            await main.Main.inject_group_guardian_prompt(h, ev, req)
            self.assertIn("机器人在本群最近确认的 QQ 角色: 未知", req.extra_user_content_parts[0].text)
            self.assertEqual([a for a, _ in client.calls], ["get_group_member_info"])
            await asyncio.sleep(0.06)  # let the bounded shared refresh settle

            failed = ActionClient("owner", fail=True)
            h, ev = self.host("member", 200, bot=failed)
            h._bot_uin_cache = 200
            h._bot_role_cache["100"] = ("member", time.time() - 400, 300)
            req = types.SimpleNamespace(extra_user_content_parts=[])
            await main.Main.inject_group_guardian_prompt(h, ev, req)
            self.assertIn("机器人在本群最近确认的 QQ 角色: 未知", req.extra_user_content_parts[0].text)
        finally:
            onebot.OneBotMixin.BOT_ROLE_HOT_WAIT = old_wait

    async def test_all_prompt_gates_skip_append_and_role_lookup(self):
        for gate in ("enabled", "disclaimer", "prompt", "group", "access"):
            h, ev = self.host()
            client = ActionClient("admin")
            ev.bot = client
            req = types.SimpleNamespace(extra_user_content_parts=[])
            if gate == "enabled": h._cfg = lambda key, default=True: False if key == "enabled" else default
            elif gate == "disclaimer": h.config["disclaimer_agreed"] = False
            elif gate == "prompt": h._cfg = lambda key, default=True: False if key == "prompt_injection_enabled" else default
            elif gate == "group": h._get_group_id = lambda event: ""
            elif gate == "access": h._check_group_access = lambda event: (False, "blocked")
            await main.Main.inject_group_guardian_prompt(h, ev, req)
            self.assertEqual(req.extra_user_content_parts, [], gate)
            self.assertEqual(client.calls, [], gate)

    async def test_official_core_admin_union_and_group_block_remain_authoritative(self):
        h, ev = self.host()
        class OfficialContext:
            astrbot_config = {"admin_id": ["stale"]}
            def get_config(self): return {"admin_id": ["core"]}
        h.context = OfficialContext()
        h._get_admin_list = lambda: ["plugin"]
        h._storage.is_group_admin_blocked = lambda group, uid: uid == "core"
        self.assertEqual(h._get_all_admin_ids(), {"plugin", "core"})
        self.assertFalse(await onebot.OneBotMixin._is_plugin_admin(h, ev))
        ev.get_sender_id = lambda: "core"
        self.assertFalse(await onebot.OneBotMixin._is_plugin_admin(h, ev))
        h._storage.is_group_admin_blocked = lambda group, uid: False
        h._admin_role_cache.clear()
        self.assertTrue(await onebot.OneBotMixin._is_plugin_admin(h, ev))


if __name__ == "__main__":
    unittest.main()
