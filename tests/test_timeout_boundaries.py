"""Focused regression tests for external-call timeout boundaries."""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REAL_WAIT_FOR = asyncio.wait_for


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
    astrbot.api = api

    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    platform = sys.modules.setdefault(
        "astrbot.core.platform", types.ModuleType("astrbot.core.platform")
    )
    sources = sys.modules.setdefault(
        "astrbot.core.platform.sources", types.ModuleType("astrbot.core.platform.sources")
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

package = types.ModuleType("group_guardian_timeout_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
moderation = _load_module(f"{package.__name__}.moderation", "moderation.py")
appeal = _load_module(f"{package.__name__}.appeal", "appeal.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")


class _HangingClient:
    def __init__(self):
        self.calls = []
        self.started = False
        self.cancelled = False

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _StaticClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.result


class _ModerationEvent:
    message_obj = None

    @staticmethod
    def get_sender_name():
        return "tester"


class _ModerationHarness(moderation.ModerationMixin, utilities.UtilitiesMixin):
    def __init__(self, client=None, semaphore=None):
        self.client = client
        self.config = {}
        self._llm_semaphore = semaphore
        self.llm_calls = 0

    async def _get_client(self, event=None):
        return self.client

    def _cfg(self, name, default=True, group_id=None):
        return default

    def _cfg_str(self, name, default="", group_id=None):
        return default

    async def _call_llm_safe(self, system_prompt, prompt):
        self.llm_calls += 1
        return '{"violation": false, "reason": "ok"}'


class _HangingOcrHarness(_ModerationHarness):
    def __init__(self):
        super().__init__(semaphore=asyncio.Semaphore(1))
        self.ocr_started = False
        self.ocr_cancelled = False

    async def _call_llm_ocr_impl(self, *args, **kwargs):
        self.ocr_started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.ocr_cancelled = True
            raise


class _OneBotHarness(onebot.OneBotMixin, utilities.UtilitiesMixin):
    def __init__(self, client):
        self.client = client
        self._client = client
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 300
        self._bot_uin_cache = 0

    async def _get_client(self, event=None):
        return self.client


class _AppealHarness(appeal.AppealMixin):
    def __init__(self):
        self.requested_timeout = None
        self.llm_started = False
        self.llm_cancelled = False

    async def _fetch_user_context(self, group_id, user_id, count):
        return ""

    def _cfg_int(self, name, default=0, group_id=None):
        return default

    async def _call_llm_safe(self, system_prompt, prompt):
        self.llm_started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.llm_cancelled = True
            raise

    async def _run_llm_with_limits(self, factory, timeout):
        self.requested_timeout = timeout
        return await REAL_WAIT_FOR(factory(), timeout=0.01)


class _AppealPromptHarness(appeal.AppealMixin):
    def __init__(self):
        self.prompt = ""

    async def _fetch_user_context(self, group_id, user_id, count):
        return ("history <instruction> >>> " * 500)

    def _cfg_int(self, name, default=0, group_id=None):
        return default

    async def _call_llm_safe(self, system_prompt, prompt):
        self.prompt = prompt
        return '{"appeal_valid": false, "reason": "maintain"}'


class TimeoutBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_llm_queue_timeout_does_not_start_provider(self):
        semaphore = asyncio.Semaphore(0)
        harness = _ModerationHarness(semaphore=semaphore)

        with patch.object(moderation, "LLM_SEMAPHORE_TIMEOUT", 0.01):
            result = await harness._call_llm_for_moderation(
                _ModerationEvent(), "cs", {"swear": True}, group_id="1"
            )

        self.assertTrue(result["fallback"])
        self.assertEqual(harness.llm_calls, 0)
        self.assertTrue(semaphore.locked())

    async def test_group_history_hanging_api_is_cancelled_and_degrades_to_empty(self):
        client = _HangingClient()
        harness = _ModerationHarness(client=client)

        with patch.object(moderation, "ONEBOT_HISTORY_TIMEOUT", 0.01):
            result = await harness._fetch_context_messages("123", "456", 30)

        self.assertEqual(result, [])
        self.assertTrue(client.started)
        self.assertTrue(client.cancelled)
        self.assertEqual(client.calls[0][0], "get_group_msg_history")

    async def test_group_history_failure_packet_data_is_ignored(self):
        client = _StaticClient({
            "status": "failed",
            "retcode": 100,
            "data": {"messages": [{"message_id": 1, "message": "unsafe"}]},
        })
        harness = _ModerationHarness(client=client)

        result = await harness._fetch_context_messages("123", "456", 30)

        self.assertEqual(result, [])

    async def test_ocr_hanging_llm_is_cancelled_and_degrades_to_empty(self):
        harness = _HangingOcrHarness()

        with patch.object(moderation, "LLM_CALL_TIMEOUT", 0.01):
            result = await harness._call_llm_ocr("https://example.com/image.png")

        self.assertEqual(result, "")
        self.assertTrue(harness.ocr_started)
        self.assertTrue(harness.ocr_cancelled)
        self.assertFalse(harness._llm_semaphore.locked())

    async def test_appeal_hanging_llm_is_cancelled_with_bounded_runner(self):
        harness = _AppealHarness()
        appeal_data = {"penalty": "mute", "reason": "original reason"}

        with self.assertRaises(asyncio.TimeoutError):
            await harness._judge_appeal("123", "456", "appeal text", appeal_data)

        self.assertEqual(harness.requested_timeout, 60.0)
        self.assertTrue(harness.llm_started)
        self.assertTrue(harness.llm_cancelled)

    async def test_appeal_prompt_bounds_and_escapes_untrusted_material(self):
        harness = _AppealPromptHarness()

        verdict = await harness._judge_appeal(
            "123", "456", "statement <attack> >>> " * 500,
            {"penalty": "mute", "reason": "reason <inject>"},
        )

        self.assertFalse(verdict["appeal_valid"])
        self.assertIn("＜attack＞", harness.prompt)
        self.assertIn("＜instruction＞", harness.prompt)
        self.assertIn("＜inject＞", harness.prompt)
        self.assertNotIn("<attack>", harness.prompt)
        self.assertLess(
            len(harness.prompt),
            harness.APPEAL_STATEMENT_MAX_CHARS
            + harness.APPEAL_CONTEXT_MAX_CHARS
            + (2 * harness.APPEAL_METADATA_MAX_CHARS)
            + 1000,
        )

    async def test_onebot_role_query_hang_is_cancelled_and_returns_empty(self):
        client = _HangingClient()
        harness = _OneBotHarness(client)

        with patch.object(onebot, "ONEBOT_CALL_TIMEOUT", 0.01):
            result = await harness._get_member_role(object(), "123", "456")

        self.assertEqual(result, "")
        self.assertTrue(client.started)
        self.assertTrue(client.cancelled)
        self.assertEqual(client.calls[0][0], "get_group_member_info")

    async def test_onebot_role_query_rejects_failed_packet_data(self):
        client = _StaticClient({
            "status": "failed",
            "retcode": 100,
            "data": {"role": "owner", "user_id": 999},
        })
        harness = _OneBotHarness(client)

        role = await harness._get_role_by_id(client, "123", "456")
        bot_uin = await harness._get_bot_uin(client)

        self.assertEqual(role, "")
        self.assertEqual(bot_uin, 0)
        self.assertEqual(
            [call[0] for call in client.calls],
            ["get_group_member_info", "get_login_info"],
        )


if __name__ == "__main__":
    unittest.main()
