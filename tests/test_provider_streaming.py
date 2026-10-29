"""Streaming must be used on the actual provider path, never partial verdicts."""
import asyncio
import types
import unittest
from unittest.mock import patch

from test_timeout_boundaries import moderation, utilities

ANSWER = '{"violation": true, "reason": "advertisement"}'


def frame(text, chunk=False):
    return types.SimpleNamespace(completion_text=text, is_chunk=chunk, role="assistant")


class StreamProvider:
    provider_name = "vision"

    def __init__(self, mode="ok", answer=ANSWER):
        self.mode = mode
        self.answer = answer
        self.calls = []
        self.nonstream_calls = 0
        self.closed = 0
        self.started = asyncio.Event()

    async def text_chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        try:
            if self.mode == "unsupported":
                raise NotImplementedError()
            if self.mode == "failure":
                raise ConnectionError("upstream unavailable")
            yield frame(self.answer[:15], True)
            self.started.set()
            if self.mode == "hang":
                await asyncio.Event().wait()
            yield frame(self.answer[15:], True)
            if self.mode == "partial":
                return
            if self.mode == "broken":
                raise ConnectionError("disconnected")
            yield frame(self.answer)
            if self.mode == "broken-after-final":
                raise ConnectionError("disconnected after final")
        finally:
            self.closed += 1

    async def text_chat(self, **kwargs):
        self.nonstream_calls += 1
        return frame("legacy complete answer")

    def meta(self):
        return types.SimpleNamespace(id="vision")


class Context:
    def __init__(self, provider):
        self.provider = provider
        self.nonstream_calls = 0

    def get_provider_by_id(self, pid):
        return self.provider

    def get_all_providers(self):
        return [self.provider]

    async def get_using_provider_async(self):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.nonstream_calls += 1
        return frame("legacy complete answer")


class Host(moderation.ModerationMixin, utilities.UtilitiesMixin):
    def __init__(self, provider):
        self.context = Context(provider)
        self.config = {"moderation_llm_provider_id": "vision", "ocr_provider_id": "vision"}
        self._config_schema = {}
        self._llm_semaphore = asyncio.Semaphore(1)
        self._ocr_semaphore = asyncio.Semaphore(1)


class ProviderStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_uses_stream_and_only_returns_final_once(self):
        provider = StreamProvider()
        host = Host(provider)
        result = await host._call_llm_safe("audit", "message")
        self.assertEqual(result, ANSWER)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["prompt"], "message")
        self.assertEqual(provider.nonstream_calls, 0)
        self.assertEqual(host.context.nonstream_calls, 0)
        self.assertEqual(provider.closed, 1)

    async def test_default_current_provider_also_uses_stream(self):
        provider = StreamProvider()
        host = Host(provider)
        host.config.clear()
        host.context.get_all_providers = lambda: []
        self.assertEqual(await host._call_llm_safe("audit", "message"), ANSWER)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.nonstream_calls, 0)

    async def test_ocr_preserves_image_payload_and_uses_stream(self):
        provider = StreamProvider(answer="Blue circle, code X7Q2")
        host = Host(provider)
        image = "data:image/png;base64,synthetic-image-bytes"
        result = await host._call_llm_ocr(image, group_id="123")
        self.assertEqual(result, "Blue circle, code X7Q2")
        self.assertEqual(provider.calls[0]["image_urls"], [image])
        self.assertEqual(host.context.nonstream_calls, 0)
        self.assertFalse(host._llm_semaphore.locked())
        self.assertFalse(host._ocr_semaphore.locked())

    async def test_ocr_without_selected_id_uses_current_streaming_provider(self):
        provider = StreamProvider(answer="image evidence")
        host = Host(provider)
        host.config.clear()
        self.assertEqual(await host._call_llm_ocr_impl("image.png"), "image evidence")
        self.assertEqual(provider.calls[0]["image_urls"], ["image.png"])
        self.assertEqual(host.context.nonstream_calls, 0)

    async def test_stream_failure_is_not_retried_via_nonstream_or_other_provider(self):
        for mode in ("failure", "broken", "partial", "broken-after-final"):
            with self.subTest(mode=mode):
                provider = StreamProvider(mode=mode)
                host = Host(provider)
                with self.assertRaises(RuntimeError):
                    await host._call_llm_safe("audit", "message")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(host.context.nonstream_calls, 0)
                self.assertEqual(provider.nonstream_calls, 0)
                self.assertEqual(provider.closed, 1)

    async def test_only_explicitly_unsupported_stream_uses_existing_compatibility(self):
        provider = StreamProvider(mode="unsupported")
        host = Host(provider)
        result = await host._call_llm_safe("audit", "message")
        self.assertEqual(result, "legacy complete answer")
        self.assertEqual(host.context.nonstream_calls, 1)
        self.assertEqual(provider.closed, 1)

    async def test_ocr_interrupted_stream_follows_three_delayed_retries(self):
        provider = StreamProvider(mode="broken")
        host = Host(provider)
        waits = []

        async def no_sleep(delay):
            waits.append(delay)
            self.assertFalse(host._llm_semaphore.locked())

        with patch.object(moderation.asyncio, "sleep", no_sleep):
            result = await host._call_llm_ocr("image.png")
        self.assertEqual(result, "")
        self.assertEqual(waits, [10, 20, 30])
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(provider.closed, 4)
        self.assertEqual(host.context.nonstream_calls, 0)
        self.assertEqual(provider.nonstream_calls, 0)

    async def test_cancellation_closes_stream_and_releases_global_permit(self):
        provider = StreamProvider(mode="hang")
        host = Host(provider)
        task = asyncio.create_task(host._run_llm_with_limits(
            lambda: host._call_llm_safe("audit", "message")))
        try:
            await asyncio.wait_for(provider.started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(provider.closed, 1)
            self.assertFalse(host._llm_semaphore.locked())
            self.assertEqual(len(provider.calls), 1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
