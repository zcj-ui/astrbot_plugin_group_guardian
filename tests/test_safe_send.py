"""v2.33.0 regression tests: ``event.send`` 安全壳 + LLM 空返回降级。

覆盖两个修复点：
1. ``_harden_event_send``：把 ``event.send`` 换成安全版本，发送失败只记日志
   返回 None，不抛异常（防止 AstrBot RespondStage 重新抛出打崩整个进程）；
2. ``review_chunk``：LLM 返回 ``None`` 时显式走「LLM无返回」降级，不再误报
   ``'NoneType' object has no attribute 'strip'``。
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    # AstrBot 运行时自带 aiohttp；CI 只安装插件的独立依赖。这里提供测试所需
    # 的最小接口，让 moderation 模块导入不依赖完整 AstrBot 环境。
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
    astrbot.api = api

    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    platform = sys.modules.setdefault(
        "astrbot.core.platform", types.ModuleType("astrbot.core.platform")
    )
    sources = sys.modules.setdefault(
        "astrbot.core.platform.sources",
        types.ModuleType("astrbot.core.platform.sources"),
    )
    aiocqhttp = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp"),
    )
    aio_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
        ),
    )
    aio_event.AiocqhttpMessageEvent = object
    core.platform = platform
    platform.sources = sources
    sources.aiocqhttp = aiocqhttp
    aiocqhttp.aiocqhttp_message_event = aio_event
    astrbot.core = core


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()

package = types.ModuleType("group_guardian_safe_send_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
moderation_context = _load_module(
    f"{package.__name__}.moderation_context", "moderation_context.py"
)
image_audit = _load_module(f"{package.__name__}.image_audit", "image_audit.py")
moderation = _load_module(f"{package.__name__}.moderation", "moderation.py")


class _FailingSendEvent:
    """底层 send 抛异常（模拟 NapCat 发送动作超时的 ActionFailed）。"""

    def __init__(self):
        self.sent = []

    async def send(self, chain, **kwargs):
        self.sent.append((chain, kwargs))
        raise RuntimeError(
            "ActionFailed status='failed' retcode=1200 EventChecker Failed"
        )


class _OkSendEvent:
    """底层 send 正常返回（模拟发送成功）。"""

    def __init__(self):
        self.sent = []

    async def send(self, chain, **kwargs):
        self.sent.append((chain, kwargs))
        return "message_id_123"


class _Harness(moderation.ModerationMixin):
    def __init__(self, config=None):
        self.config = config or {}

    def _cfg(self, name, default=True, group_id=None):
        return self.config.get(name, default)


class SafeSendShellTests(unittest.IsolatedAsyncioTestCase):
    def test_send_failure_is_swallowed_and_returns_none(self):
        event = _FailingSendEvent()
        _Harness()._harden_event_send(event)

        result = asyncio.run(event.send("chain"))

        self.assertIsNone(result)
        self.assertEqual(event.sent, [("chain", {})])

    async def test_success_result_passes_through(self):
        event = _OkSendEvent()
        _Harness()._harden_event_send(event)

        result = await event.send("chain", type="text")

        self.assertEqual(result, "message_id_123")
        self.assertEqual(event.sent, [("chain", {"type": "text"})])

    async def test_injection_is_idempotent(self):
        event = _FailingSendEvent()
        harness = _Harness()
        harness._harden_event_send(event)
        wrapped_once = event.send

        harness._harden_event_send(event)

        self.assertIs(event.send, wrapped_once)
        # 包装后调用仍不抛异常
        self.assertIsNone(await event.send("chain"))

    async def test_disabled_when_safe_send_enabled_false(self):
        event = _FailingSendEvent()
        _Harness({"safe_send_enabled": False})._harden_event_send(event)

        with self.assertRaises(RuntimeError):
            await event.send("chain")

    async def test_missing_send_does_not_crash(self):
        event = object()
        _Harness()._harden_event_send(event)  # 不应抛异常


class _ModerationEvent:
    def __init__(self, message_id="", message_seq=0, timestamp=0):
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.raw_event = {
            "user_id": 456,
            "message_seq": message_seq,
            "time": timestamp,
        }

    @staticmethod
    def get_sender_name():
        return "tester"


class _ModerationHarness(moderation.ModerationMixin, utilities.UtilitiesMixin):
    def __init__(self, config=None):
        self.config = config or {}
        self._config_schema = {}
        self.llm_calls = 0
        self.llm_responses = []

    async def _get_client(self, event=None):
        return None

    def _cfg(self, name, default=True, group_id=None):
        return self.config.get(name, default)

    def _cfg_str(self, name, default="", group_id=None):
        return str(self.config.get(name, default) or "")

    async def _call_llm_safe(self, system_prompt, prompt):
        self.llm_calls += 1
        if self.llm_responses:
            return self.llm_responses.pop(0)
        return '{"violation": false, "reason": "ok"}'


class LlmEmptyResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_llm_response_degrades_without_strip_error(self):
        harness = _ModerationHarness()
        harness.llm_responses = [None]

        result = await harness._call_llm_for_moderation(
            _ModerationEvent(), "cs", {"full_scan": True}, group_id="1"
        )

        self.assertFalse(result["violation"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["reason"], "LLM无返回")
        self.assertEqual(harness.llm_calls, 1)

    async def test_normal_llm_response_still_parses(self):
        harness = _ModerationHarness()

        result = await harness._call_llm_for_moderation(
            _ModerationEvent(), "cs", {"full_scan": True}, group_id="1"
        )

        self.assertFalse(result["violation"])
        self.assertFalse(result["fallback"])
        self.assertEqual(harness.llm_calls, 1)


if __name__ == "__main__":
    unittest.main()
