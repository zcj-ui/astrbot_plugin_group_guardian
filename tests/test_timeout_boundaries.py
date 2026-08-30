"""Focused regression tests for external-call timeout boundaries."""

import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REAL_WAIT_FOR = asyncio.wait_for


def _install_astrbot_stubs():
    # AstrBot 运行时自带 aiohttp；CI 只安装插件的独立依赖。这里提供测试所需
    # 的最小接口，让 HTTP 会话测试不依赖完整 AstrBot 环境。
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
moderation_context = _load_module(
    f"{package.__name__}.moderation_context", "moderation_context.py"
)
image_audit = _load_module(f"{package.__name__}.image_audit", "image_audit.py")
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


class _ControlledHistoryClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.cancelled = False

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


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
    def __init__(self, client=None, semaphore=None):
        self.client = client
        self.config = {}
        self._config_schema = {}
        self._llm_semaphore = semaphore
        self.llm_calls = 0
        self.last_prompt = ""
        self.prompts = []

    async def _get_client(self, event=None):
        return self.client

    def _cfg(self, name, default=True, group_id=None):
        return self.config.get(name, default)

    def _cfg_str(self, name, default="", group_id=None):
        return str(self.config.get(name, default) or "")

    async def _call_llm_safe(self, system_prompt, prompt):
        self.llm_calls += 1
        self.last_prompt = prompt
        self.prompts.append(prompt)
        return '{"violation": false, "reason": "ok"}'


class _CurrentProvider:
    provider_name = "current-test-provider"

    async def text_chat(self, *args, **kwargs):
        return "current provider response"


class _CurrentProviderContext:
    def __init__(self):
        self.async_getter_calls = 0

    @staticmethod
    def get_all_providers():
        return []

    async def get_using_provider_async(self):
        self.async_getter_calls += 1
        return _CurrentProvider()

    @property
    def provider_manager(self):
        raise AssertionError("must use Context public provider API")


class _ProviderFallbackHarness(moderation.ModerationMixin):
    config = {}

    def __init__(self):
        self.context = _CurrentProviderContext()


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


class _ConcurrentOcrHarness(moderation.ModerationMixin):
    def __init__(self):
        self.release = asyncio.Event()
        self.all_started = asyncio.Event()
        self.started = []
        self.active = 0
        self.max_active = 0

    async def _call_llm_ocr(self, image_url, **kwargs):
        self.started.append(image_url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if len(self.started) == 4:
            self.all_started.set()
        try:
            await self.release.wait()
            delays = {
                "one.gif": 0.03, "bad.png": 0.01,
                "sticker-three.png": 0, "four.png": 0.02,
            }
            await asyncio.sleep(delays[image_url])
            if image_url == "bad.png":
                raise RuntimeError("single image failed")
            return {
                "one.gif": "one", "sticker-three.png": "three",
                "four.png": "four",
            }[image_url]
        finally:
            self.active -= 1


class _QueuedOcrHarness(moderation.ModerationMixin):
    def __init__(self):
        self.config = {}
        self._config_schema = {}
        self._ocr_semaphore = asyncio.Semaphore(4)
        self._llm_semaphore = asyncio.Semaphore(8)
        self.started = []
        self.first_wave_started = asyncio.Event()
        self.release_first_wave = asyncio.Event()

    async def _call_llm_ocr_impl(self, image_url, **_kwargs):
        self.started.append(image_url)
        if len(self.started) == 4:
            self.first_wave_started.set()
        if image_url != "five.png":
            await self.release_first_wave.wait()
        return image_url


class _ConcurrentQrHarness(moderation.ModerationMixin):
    def __init__(self):
        self.release = asyncio.Event()
        self.all_started = asyncio.Event()
        self.started = []
        self.active = 0
        self.max_active = 0

    async def _download_bytes(self, url, *args, **kwargs):
        self.started.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if len(self.started) == 4:
            self.all_started.set()
        try:
            await self.release.wait()
            delays = {
                "one.png": 0.03, "bad.png": 0.01,
                "three.png": 0, "four.png": 0.02,
            }
            await asyncio.sleep(delays[url])
            if url == "bad.png":
                raise RuntimeError("download failed")
            return url.encode()
        finally:
            self.active -= 1


class _FakeImageContent:
    def __init__(self, payload):
        self.chunks = list(payload) if isinstance(payload, list) else [payload]

    async def read(self, limit):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > limit:
            self.chunks.insert(0, chunk[limit:])
            return chunk[:limit]
        return chunk


class _FakeImageResponse:
    status = 200
    headers = {}

    def __init__(self, session, payload):
        self.session = session
        self.content = _FakeImageContent(payload)

    async def __aenter__(self):
        self.session.active += 1
        self.session.max_active = max(self.session.max_active, self.session.active)
        self.session.started.set()
        if self.session.release is not None:
            await self.session.release.wait()
        return self

    async def __aexit__(self, *_args):
        self.session.active -= 1


class _FakeImageSession:
    def __init__(self, release=None, payloads=None):
        self.release = release
        self.payloads = dict(payloads or {})
        self.started = asyncio.Event()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.close_calls = 0

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeImageResponse(
            self, self.payloads.get(url, url.encode())
        )

    async def close(self):
        self.close_calls += 1
        self.closed = True


class _ImageDownloadHarness(moderation.ModerationMixin):
    def __init__(self, concurrency=1):
        self._image_io_semaphore = asyncio.Semaphore(concurrency)
        self._image_http_session = None
        self._image_http_session_lock = asyncio.Lock()
        self._image_audit_closing = False

    async def _is_safe_image_url(self, _url):
        return True


class _QrDecodeSemaphoreHarness(moderation.ModerationMixin):
    def __init__(self, concurrency=1):
        self._qr_decode_semaphore = asyncio.Semaphore(concurrency)


class _ConcurrentApplyHarness(moderation.ModerationMixin):
    def __init__(self):
        self.release = asyncio.Event()
        self.both_started = asyncio.Event()
        self.started = set()

    def _cfg(self, name, default=True, group_id=None):
        return name in {"qrcode_decode_enabled", "ocr_enabled", "scan_sticker_enabled"}

    async def _wait_for_release(self, branch, result):
        self.started.add(branch)
        if len(self.started) == 2:
            self.both_started.set()
        await self.release.wait()
        return result

    async def _decode_qrcodes(self, image_urls):
        return await self._wait_for_release("qr", "qr-result")

    async def _ocr_images(self, event, image_urls, group_id=""):
        return await self._wait_for_release("ocr", "ocr-result")


class _FullImageApplyHarness(_ConcurrentApplyHarness):
    def _cfg(self, name, default=True, group_id=None):
        if name in {"llm_moderation_enabled", "llm_moderation_always"}:
            return True
        if name in {
            "qrcode_decode_enabled", "ocr_enabled", "scan_sticker_enabled"
        }:
            return False
        return default


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
    async def test_llm_fallback_uses_context_public_provider_api(self):
        harness = _ProviderFallbackHarness()

        result = await harness._call_llm_safe("system", "prompt")

        self.assertEqual(result, "current provider response")
        self.assertEqual(harness.context.async_getter_calls, 1)

    async def test_moderation_llm_queue_waits_instead_of_auto_passing(self):
        semaphore = asyncio.Semaphore(0)
        harness = _ModerationHarness(semaphore=semaphore)

        task = asyncio.create_task(harness._call_llm_for_moderation(
            _ModerationEvent(), "cs", {"swear": True}, group_id="1"
        ))
        await asyncio.sleep(0.02)

        self.assertFalse(task.done())
        self.assertEqual(harness.llm_calls, 0)
        semaphore.release()
        result = await task

        self.assertFalse(result["violation"])
        self.assertEqual(harness.llm_calls, 1)

    async def test_moderation_llm_queue_has_a_long_overload_boundary(self):
        semaphore = asyncio.Semaphore(0)
        harness = _ModerationHarness(semaphore=semaphore)

        with patch.object(moderation, "LLM_QUEUE_TIMEOUT", 0.01):
            result = await harness._call_llm_for_moderation(
                _ModerationEvent(), "cs", {"swear": True}, group_id="1"
            )

        self.assertFalse(result["violation"])
        self.assertTrue(result["fallback"])
        self.assertEqual(harness.llm_calls, 0)

    async def test_full_scan_reviews_every_oversized_message_chunk(self):
        harness = _ModerationHarness(semaphore=asyncio.Semaphore(2))
        middle_evidence = "MIDDLE_IMAGE_EVIDENCE"
        text = ("a" * 45_000) + middle_evidence + ("z" * 45_000)
        text = harness._append_stream_rule_evidence(text, [])
        expected_chunks = len(harness._llm_message_chunks(
            text, {"full_scan": True}
        ))

        result = await harness._call_llm_for_moderation(
            _ModerationEvent(), text, {"full_scan": True}, group_id="1"
        )

        self.assertFalse(result["violation"])
        self.assertEqual(harness.llm_calls, expected_chunks)
        self.assertIn(middle_evidence, "\n".join(harness.prompts))
        self.assertTrue(
            all("[审核分片 " in prompt for prompt in harness.prompts)
        )

    async def test_review_guidance_is_appended_to_default_moderation_prompt(self):
        harness = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        harness.config["llm_moderation_review_guidance"] = "需要结合推广意图，不按单个普通词处罚"

        result = await harness._call_llm_for_moderation(
            _ModerationEvent(), "日抛plus", {"full_scan": True}, group_id="1"
        )

        self.assertFalse(result["violation"])
        self.assertIn("管理员确认误判后的补充修正规则", harness.last_prompt)
        self.assertIn("需要结合推广意图，不按单个普通词处罚", harness.last_prompt)
        self.assertIn('{"violation": true/false, "reason": "判断原因"}', harness.last_prompt)

    async def test_review_guidance_is_appended_to_custom_moderation_prompt(self):
        harness = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        harness.config.update({
            "llm_moderation_custom_prompt": "只拦截存在明确招揽行为的广告",
            "llm_moderation_review_guidance": "正常商品名讨论需要放行",
        })

        result = await harness._call_llm_for_moderation(
            _ModerationEvent(), "日抛plus", {"full_scan": True}, group_id="1"
        )

        self.assertFalse(result["violation"])
        self.assertIn("只拦截存在明确招揽行为的广告", harness.last_prompt)
        self.assertIn("正常商品名讨论需要放行", harness.last_prompt)
        self.assertIn('{"violation": true/false, "reason": "判断原因"}', harness.last_prompt)

    async def test_group_history_hanging_api_is_cancelled_and_degrades_to_empty(self):
        client = _HangingClient()
        harness = _ModerationHarness(client=client)

        with patch.object(moderation_context, "ONEBOT_HISTORY_TIMEOUT", 0.01):
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

    async def test_group_history_is_sorted_and_future_messages_are_excluded(self):
        client = _StaticClient({
            "status": "ok",
            "retcode": 0,
            "data": {"messages": [
                {"message_id": 4, "message_seq": 4, "time": 104, "message": "future"},
                {"message_id": 2, "message_seq": 2, "time": 102, "message": "second"},
                {"message_id": 3, "message_seq": 3, "time": 103, "message": "current"},
                {"message_id": 1, "message_seq": 1, "time": 101, "message": "first"},
            ]},
        })
        harness = _ModerationHarness(client=client)

        result = await harness._fetch_context_messages(
            "123", "3", 30, current_seq=3, current_time=103
        )

        self.assertEqual([item["message_id"] for item in result], [1, 2])

    async def test_same_second_history_without_sequence_is_not_future_context(self):
        client = _StaticClient({
            "status": "ok", "retcode": 0,
            "data": {"messages": [
                {"message_id": 1, "time": 99, "message": "older"},
                {"message_id": 2, "time": 100, "message": "same-second"},
            ]},
        })
        harness = _ModerationHarness(client=client)

        result = await harness._fetch_context_messages(
            "123", "", current_time=100
        )

        self.assertEqual([item["message"] for item in result], ["older"])

    async def test_llm_uses_sequence_history_when_message_id_is_missing(self):
        client = _StaticClient({
            "status": "ok", "retcode": 0,
            "data": {"messages": [
                {
                    "message_id": 1, "message_seq": 1, "time": 101,
                    "sender": {"nickname": "before"},
                    "message": "earlier context",
                },
                {
                    "message_id": 4, "message_seq": 4, "time": 104,
                    "sender": {"nickname": "future"},
                    "message": "future context",
                },
            ]},
        })
        harness = _ModerationHarness(client=client)
        event = _ModerationEvent(message_id="", message_seq=3, timestamp=103)

        result = await harness._call_llm_for_moderation(
            event, "current", {"full_scan": True}, group_id="123"
        )

        self.assertFalse(result["violation"])
        self.assertIn("earlier context", harness.last_prompt)
        self.assertNotIn("future context", harness.last_prompt)
        self.assertEqual(len(client.calls), 1)

    async def test_llm_reuses_cached_ocr_for_history_images(self):
        image_url = "https://example.com/history.png"
        client = _StaticClient({
            "status": "ok", "retcode": 0,
            "data": {"messages": [{
                "message_id": 1, "message_seq": 1, "time": 101,
                "sender": {"nickname": "before", "user_id": 9},
                "message": [
                    {"type": "text", "data": {"text": "x" * 1000}},
                    {"type": "image", "data": {"url": image_url}},
                ],
            }]},
        })
        harness = _ModerationHarness(client=client)
        harness._cache_image_evidence(
            image_url, "ocr", "日抛plus /xxxxxx"
        )
        event = _ModerationEvent(message_id="2", message_seq=2, timestamp=102)

        result = await harness._call_llm_for_moderation(
            event, "current", {"full_scan": True}, group_id="123"
        )

        self.assertFalse(result["violation"])
        self.assertIn("[历史图片1OCR] 日抛plus /xxxxxx", harness.last_prompt)

    async def test_missing_ids_keep_earlier_history_by_sequence(self):
        client = _StaticClient({
            "status": "ok", "retcode": 0,
            "data": {"messages": [
                {"message_id": "", "message_seq": 1, "message": "first"},
                {"message_id": "", "message_seq": 2, "message": "second"},
                {"message_id": "", "message_seq": 3, "message": "current"},
            ]},
        })
        harness = _ModerationHarness(client=client)

        result = await harness._fetch_context_messages(
            "123", "", current_seq=3
        )

        self.assertEqual([item["message"] for item in result], ["first", "second"])

    async def test_same_group_history_requests_share_snapshot_and_cache(self):
        client = _ControlledHistoryClient({
            "status": "ok",
            "retcode": 0,
            "data": {"messages": [
                {"message_id": seq, "message_seq": seq, "time": 100 + seq}
                for seq in range(1, 6)
            ]},
        })
        harness = _ModerationHarness(client=client)

        first = asyncio.create_task(harness._fetch_context_messages(
            "123", "4", 30, current_seq=4, current_time=104
        ))
        second = asyncio.create_task(harness._fetch_context_messages(
            "123", "5", 30, current_seq=5, current_time=105
        ))
        await REAL_WAIT_FOR(client.started.wait(), timeout=1)
        await asyncio.sleep(0.02)

        self.assertEqual(len(client.calls), 1)
        client.release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(
            [item["message_id"] for item in first_result], [1, 2, 3]
        )
        self.assertEqual(
            [item["message_id"] for item in second_result], [1, 2, 3, 4]
        )
        cached = await harness._fetch_context_messages(
            "123", "3", 30, current_seq=3, current_time=103
        )
        self.assertEqual([item["message_id"] for item in cached], [1, 2])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0][1]["count"], moderation_context.HISTORY_FETCH_COUNT
        )

    async def test_cancelled_history_waiter_does_not_cancel_shared_request(self):
        client = _ControlledHistoryClient({
            "status": "ok", "retcode": 0,
            "data": {"messages": [{"message_id": 1, "message_seq": 1}]},
        })
        harness = _ModerationHarness(client=client)

        cancelled_waiter = asyncio.create_task(
            harness._fetch_context_messages("123", "2", current_seq=2)
        )
        surviving_waiter = asyncio.create_task(
            harness._fetch_context_messages("123", "3", current_seq=3)
        )
        await REAL_WAIT_FOR(client.started.wait(), timeout=1)
        cancelled_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_waiter
        self.assertFalse(client.cancelled)

        client.release.set()
        result = await surviving_waiter

        self.assertEqual([item["message_id"] for item in result], [1])
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(client.cancelled)

    async def test_history_requests_across_groups_respect_global_semaphore(self):
        client = _ControlledHistoryClient({
            "status": "ok", "retcode": 0, "data": {"messages": []},
        })
        harness = _ModerationHarness(client=client)
        harness._history_semaphore = asyncio.Semaphore(1)

        first = asyncio.create_task(
            harness._fetch_context_messages("101", "1", current_seq=1)
        )
        second = asyncio.create_task(
            harness._fetch_context_messages("202", "2", current_seq=2)
        )
        await REAL_WAIT_FOR(client.started.wait(), timeout=1)
        await asyncio.sleep(0.02)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.max_active, 1)
        client.release.set()
        await asyncio.gather(first, second)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.max_active, 1)

    async def test_history_queue_waits_instead_of_dropping_context(self):
        client = _StaticClient({
            "status": "ok", "retcode": 0, "data": {"messages": []},
        })
        harness = _ModerationHarness(client=client)
        harness._history_semaphore = asyncio.Semaphore(0)

        with patch.object(
            moderation_context, "ONEBOT_HISTORY_QUEUE_TIMEOUT", 1.0,
        ):
            task = asyncio.create_task(
                harness._fetch_context_messages("303", "1", current_seq=1)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertEqual(client.calls, [])

            harness._history_semaphore.release()
            result = await task

        self.assertEqual(result, [])
        self.assertEqual(len(client.calls), 1)

    async def test_history_queue_timeout_does_not_wait_forever(self):
        client = _StaticClient({
            "status": "ok", "retcode": 0, "data": {"messages": []},
        })
        harness = _ModerationHarness(client=client)
        harness._history_semaphore = asyncio.Semaphore(0)

        with patch.object(
            moderation_context, "ONEBOT_HISTORY_QUEUE_TIMEOUT", 0.01
        ):
            result = await harness._fetch_context_messages(
                "304", "1", current_seq=1
            )

        self.assertEqual(result, [])
        self.assertEqual(client.calls, [])

    async def test_ocr_hanging_llm_is_cancelled_and_degrades_to_empty(self):
        harness = _HangingOcrHarness()

        with patch.object(image_audit, "LLM_CALL_TIMEOUT", 0.01):
            result = await harness._call_llm_ocr("https://example.com/image.png")

        self.assertEqual(result, "")
        self.assertTrue(harness.ocr_started)
        self.assertTrue(harness.ocr_cancelled)
        self.assertFalse(harness._llm_semaphore.locked())

    async def test_multi_image_ocr_is_concurrent_ordered_and_failure_isolated(self):
        harness = _ConcurrentOcrHarness()
        task = asyncio.create_task(harness._ocr_images(
            None,
            ["one.gif", "one.gif", "bad.png", "sticker-three.png", "four.png"],
            group_id="1",
        ))

        await REAL_WAIT_FOR(harness.all_started.wait(), timeout=1)
        self.assertEqual(
            harness.started,
            ["one.gif", "bad.png", "sticker-three.png", "four.png"],
        )
        self.assertEqual(harness.max_active, 4)
        self.assertFalse(task.done())

        harness.release.set()
        result = await task

        self.assertEqual(
            result,
            "[图片1OCR] [GIF动图] one\n"
            "[图片3OCR] [表情包] three\n"
            "[图片4OCR] four",
        )

    async def test_fifth_slow_image_waits_for_ocr_slot_instead_of_dropping(self):
        harness = _QueuedOcrHarness()
        task = asyncio.create_task(harness._ocr_images(
            None,
            ["one.png", "two.png", "three.png", "four.png", "five.png"],
            group_id="1",
        ))
        await REAL_WAIT_FOR(harness.first_wave_started.wait(), timeout=1)
        await asyncio.sleep(0.02)
        self.assertEqual(
            harness.started,
            ["one.png", "two.png", "three.png", "four.png"],
        )
        self.assertFalse(task.done())

        harness.release_first_wave.set()
        result = await task

        self.assertEqual(harness.started[-1], "five.png")
        self.assertIn("five.png", result)

    async def test_multi_image_qr_is_concurrent_ordered_and_failure_isolated(self):
        harness = _ConcurrentQrHarness()

        def fake_decode(data, decoder):
            return {
                b"one.png": [" first "],
                b"three.png": ["third-a", " third-b "],
                b"four.png": ["fourth"],
            }[data]

        with patch.object(image_audit, "_probe_qr_decoder", return_value="fake"), \
                patch.object(image_audit, "_decode_qr_from_bytes", side_effect=fake_decode):
            task = asyncio.create_task(harness._decode_qrcodes(
                ["one.png", "one.png", "bad.png", "three.png", "four.png"]
            ))
            await REAL_WAIT_FOR(harness.all_started.wait(), timeout=1)
            self.assertEqual(
                harness.started,
                ["one.png", "bad.png", "three.png", "four.png"],
            )
            self.assertEqual(harness.max_active, 4)
            self.assertFalse(task.done())

            harness.release.set()
            result = await task

        self.assertEqual(
            result,
            "[图片1二维码] first\n"
            "[图片3二维码] third-a\nthird-b\n"
            "[图片4二维码] fourth",
        )

    async def test_cross_message_downloads_share_semaphore_and_http_session(self):
        release = asyncio.Event()
        session = _FakeImageSession(release=release)
        harness = _ImageDownloadHarness(concurrency=1)

        with patch("aiohttp.ClientSession", return_value=session) as session_factory:
            first = asyncio.create_task(
                harness._download_bytes("https://example.com/first.png")
            )
            second = asyncio.create_task(
                harness._download_bytes("https://example.com/second.png")
            )
            await REAL_WAIT_FOR(session.started.wait(), timeout=1)
            await asyncio.sleep(0.02)

            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.max_active, 1)
            self.assertFalse(second.done())
            release.set()
            results = await asyncio.gather(first, second)

        self.assertEqual(
            results,
            [
                b"https://example.com/first.png",
                b"https://example.com/second.png",
            ],
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.max_active, 1)
        session_factory.assert_called_once_with()

    async def test_concurrent_first_downloads_create_one_http_session(self):
        sessions = []

        def create_session():
            session = _FakeImageSession()
            sessions.append(session)
            return session

        harness = _ImageDownloadHarness(concurrency=2)
        with patch("aiohttp.ClientSession", side_effect=create_session) as factory:
            results = await asyncio.gather(
                harness._download_bytes("https://example.com/first.png"),
                harness._download_bytes("https://example.com/second.png"),
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(len(sessions), 1)
        factory.assert_called_once_with()

    async def test_chunked_image_download_reads_until_eof(self):
        url = "https://example.com/chunked.png"
        session = _FakeImageSession(payloads={url: [b"first-", b"second"]})
        harness = _ImageDownloadHarness(concurrency=1)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await harness._download_bytes(url)

        self.assertEqual(result, b"first-second")

    async def test_closed_image_resources_do_not_create_new_session(self):
        harness = _ImageDownloadHarness(concurrency=1)
        await harness._close_image_audit_resources()

        with patch("aiohttp.ClientSession") as factory:
            session = await harness._get_image_http_session()

        self.assertIsNone(session)
        factory.assert_not_called()

    async def test_image_http_session_close_is_idempotent(self):
        session = _FakeImageSession()
        harness = _ImageDownloadHarness()
        harness._image_http_session = session

        await harness._close_image_audit_resources()
        await harness._close_image_audit_resources()

        self.assertTrue(session.closed)
        self.assertEqual(session.close_calls, 1)
        self.assertIsNone(harness._image_http_session)

    async def test_cross_message_qr_threads_share_plugin_semaphore(self):
        harness = _QrDecodeSemaphoreHarness(concurrency=1)
        first_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        def fake_decode(data, _decoder):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            first_started.set()
            try:
                release.wait(timeout=1)
                return [data.decode()]
            finally:
                with lock:
                    state["active"] -= 1

        with patch.object(
            image_audit, "_decode_qr_from_bytes", side_effect=fake_decode
        ):
            first = asyncio.create_task(harness._run_qr_decoder(b"first", "fake"))
            second = asyncio.create_task(harness._run_qr_decoder(b"second", "fake"))
            try:
                for _ in range(100):
                    if first_started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(first_started.is_set())
                await asyncio.sleep(0.02)
                self.assertEqual(state["max_active"], 1)
                self.assertFalse(second.done())
            finally:
                release.set()
            results = await asyncio.gather(first, second)

        self.assertEqual(results, [["first"], ["second"]])
        self.assertEqual(state["max_active"], 1)

    async def test_qr_and_ocr_branches_run_concurrently_with_stable_output_order(self):
        harness = _ConcurrentApplyHarness()
        task = asyncio.create_task(harness._apply_ocr(
            "message", ["one.png"], None, group_id="1"
        ))

        await REAL_WAIT_FOR(harness.both_started.wait(), timeout=1)
        self.assertEqual(harness.started, {"qr", "ocr"})
        self.assertFalse(task.done())

        harness.release.set()
        result = await task

        self.assertEqual(
            result,
            "message\n[二维码内容]\nqr-result\n[OCR识图内容]\nocr-result",
        )

    async def test_full_scan_forces_image_and_qrcode_audit(self):
        harness = _FullImageApplyHarness()
        task = asyncio.create_task(harness._apply_ocr(
            "", ["sticker-one.png"], None, group_id="1"
        ))

        await REAL_WAIT_FOR(harness.both_started.wait(), timeout=1)
        self.assertEqual(harness.started, {"qr", "ocr"})

        harness.release.set()
        result = await task

        self.assertEqual(
            result,
            "[二维码内容]\nqr-result\n[OCR识图内容]\nocr-result",
        )

    async def test_image_audit_total_timeout_cancels_stuck_branches(self):
        harness = _FullImageApplyHarness()

        with patch.object(image_audit, "IMAGE_AUDIT_TOTAL_TIMEOUT", 0.01):
            result = await harness._apply_ocr(
                "message", ["one.png"], None, group_id="1"
            )

        self.assertIn("message", result)
        self.assertIn("视觉模型未返回可用识别结果", result)

    async def test_image_resource_close_cancels_tracked_branch_tasks(self):
        harness = _FullImageApplyHarness()
        harness._image_audit_tasks = set()
        harness._image_audit_closing = False
        harness._image_http_session = None
        harness._image_http_session_lock = asyncio.Lock()
        task = asyncio.create_task(harness._apply_ocr(
            "message", ["one.png"], None, group_id="1"
        ))
        await REAL_WAIT_FOR(harness.both_started.wait(), timeout=1)

        await harness._close_image_audit_resources()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(harness._image_audit_tasks, set())

    async def test_qr_decoder_timeout_does_not_block_the_event_chain(self):
        harness = _QrDecodeSemaphoreHarness(concurrency=1)
        started = threading.Event()
        release = threading.Event()

        def stuck_decode(_data, _decoder):
            started.set()
            release.wait(timeout=1)
            return []

        try:
            with patch.object(
                image_audit, "_decode_qr_from_bytes", side_effect=stuck_decode
            ), patch.object(image_audit, "IMAGE_QR_DECODE_TIMEOUT", 0.01):
                result = await harness._run_qr_decoder(b"data", "fake")
            self.assertTrue(started.is_set())
            self.assertEqual(result, [])
            self.assertTrue(harness._qr_decode_semaphore.locked())
        finally:
            release.set()
        for _ in range(100):
            if not harness._qr_decode_semaphore.locked():
                break
            await asyncio.sleep(0.01)
        self.assertFalse(harness._qr_decode_semaphore.locked())

    async def test_dns_timeout_rejects_image_without_blocking(self):
        harness = _QrDecodeSemaphoreHarness()
        started = threading.Event()
        release = threading.Event()

        def stuck_lookup(_host):
            started.set()
            release.wait(timeout=1)
            return False

        try:
            with patch.object(
                harness, "_is_private_host", side_effect=stuck_lookup
            ), patch.object(image_audit, "IMAGE_DNS_TIMEOUT", 0.01):
                result = await harness._is_safe_image_url(
                    "https://example.com/image.png"
                )
            self.assertTrue(started.is_set())
            self.assertFalse(result)
        finally:
            release.set()

    def test_connection_resolver_rejects_private_dns_result(self):
        addrinfo = [
            (
                image_audit.socket.AF_INET,
                image_audit.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ]

        with self.assertRaises(OSError):
            image_audit._validated_resolver_records("example.com", 443, addrinfo)

    def test_connection_resolver_rejects_mixed_public_private_results(self):
        addrinfo = [
            (
                image_audit.socket.AF_INET,
                image_audit.socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 443),
            ),
            (
                image_audit.socket.AF_INET,
                image_audit.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 443),
            ),
        ]

        with self.assertRaises(OSError):
            image_audit._validated_resolver_records("example.com", 443, addrinfo)

    def test_connection_resolver_returns_only_deduplicated_public_results(self):
        addrinfo = [
            (
                image_audit.socket.AF_INET,
                image_audit.socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 443),
            ),
            (
                image_audit.socket.AF_INET,
                image_audit.socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 443),
            ),
        ]

        records = image_audit._validated_resolver_records(
            "example.com", 443, addrinfo
        )

        self.assertEqual(1, len(records))
        self.assertEqual("8.8.8.8", records[0]["host"])

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

    async def test_mute_member_reports_protocol_failure(self):
        client = _StaticClient({
            "status": "failed", "retcode": 100, "message": "denied",
        })
        harness = _OneBotHarness(client)
        event = types.SimpleNamespace(
            group_id="123",
            get_sender_id=lambda: "456",
        )

        succeeded = await harness._mute_member(event, 300)

        self.assertFalse(succeeded)
        self.assertEqual(client.calls[0][0], "set_group_ban")

    async def test_mute_member_reports_success(self):
        client = _StaticClient({"status": "ok", "retcode": 0})
        harness = _OneBotHarness(client)
        event = types.SimpleNamespace(
            group_id="123",
            get_sender_id=lambda: "456",
        )

        succeeded = await harness._mute_member(event, 300)

        self.assertTrue(succeeded)

    async def test_unban_member_reports_protocol_failure(self):
        client = _StaticClient({
            "status": "failed", "retcode": 100, "message": "denied",
        })
        harness = _OneBotHarness(client)

        succeeded = await harness._unban_member("123", "456")

        self.assertFalse(succeeded)
        self.assertEqual(client.calls[0][0], "set_group_ban")
        self.assertEqual(0, client.calls[0][1]["duration"])

    async def test_unban_member_reports_success(self):
        client = _StaticClient({"status": "ok", "retcode": 0})
        harness = _OneBotHarness(client)

        succeeded = await harness._unban_member("123", "456")

        self.assertTrue(succeeded)

    async def test_kick_member_reports_protocol_failure(self):
        client = _StaticClient({
            "status": "failed", "retcode": 100, "message": "denied",
        })
        harness = _OneBotHarness(client)
        event = types.SimpleNamespace(
            group_id="123",
            get_sender_id=lambda: "456",
        )

        succeeded = await harness._kick_member(event)

        self.assertFalse(succeeded)
        self.assertEqual(client.calls[0][0], "set_group_kick")

    async def test_kick_member_reports_success(self):
        client = _StaticClient({"status": "ok", "retcode": 0})
        harness = _OneBotHarness(client)
        event = types.SimpleNamespace(
            group_id="123",
            get_sender_id=lambda: "456",
        )

        succeeded = await harness._kick_member(event)

        self.assertTrue(succeeded)


if __name__ == "__main__":
    unittest.main()
