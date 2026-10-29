"""Behavioral regression coverage for delayed AI retries and cancellation."""
import asyncio
import types
import unittest
from unittest.mock import patch

from test_timeout_boundaries import moderation, _ModerationHarness


class LlmRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_retries_wait_10_20_30_and_stop_after_recovery(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        attempts, waits = [], []

        async def call():
            attempts.append(len(attempts) + 1)
            if len(attempts) <= 3:
                raise ConnectionError("provider unavailable")
            return "recovered"

        async def sleep(delay):
            waits.append(delay)
            self.assertFalse(host._llm_semaphore.locked())

        with patch.object(moderation.asyncio, "sleep", sleep):
            result = await host._run_llm_with_limits(call)
        self.assertEqual(result, "recovered")
        self.assertEqual(attempts, [1, 2, 3, 4])
        self.assertEqual(waits, [10, 20, 30])

    async def test_success_never_sleeps_or_calls_again(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        attempts, waits = [], []

        async def call():
            attempts.append(1)
            return "ok"

        async def sleep(delay):
            waits.append(delay)

        with patch.object(moderation.asyncio, "sleep", sleep):
            self.assertEqual(await host._run_llm_with_limits(call), "ok")
        self.assertEqual(attempts, [1])
        self.assertEqual(waits, [])

    async def test_exhaustion_raises_last_failure_after_four_attempts(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        failures = [RuntimeError(str(i)) for i in range(4)]
        attempts, waits = [], []

        async def call():
            exc = failures[len(attempts)]
            attempts.append(1)
            raise exc

        async def sleep(delay):
            waits.append(delay)

        with patch.object(moderation.asyncio, "sleep", sleep):
            with self.assertRaises(RuntimeError) as caught:
                await host._run_llm_with_limits(call)
        self.assertIs(caught.exception, failures[3])
        self.assertEqual(len(attempts), 4)
        self.assertEqual(waits, [10, 20, 30])
        self.assertFalse(host._llm_semaphore.locked())

    async def test_empty_provider_results_retry_then_recover(self):
        host = _ModerationHarness()
        responses = iter([None, "", " \n", "valid answer"])
        waits = []

        async def call():
            return next(responses)

        async def sleep(delay):
            waits.append(delay)

        with patch.object(moderation.asyncio, "sleep", sleep):
            self.assertEqual(await host._run_llm_with_limits(call), "valid answer")
        self.assertEqual(waits, [10, 20, 30])

    async def test_each_timeout_cancels_attempt_before_retry(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        attempts, cancelled, waits = [], [], []

        async def call():
            attempts.append(1)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(1)
                raise

        async def sleep(delay):
            waits.append(delay)
            self.assertEqual(len(attempts), len(cancelled))
            self.assertFalse(host._llm_semaphore.locked())

        with patch.object(moderation.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.TimeoutError):
                await host._run_llm_with_limits(call, timeout=0.02)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(len(cancelled), 4)
        self.assertEqual(waits, [10, 20, 30])

    async def test_queue_timeout_does_not_retry_an_unstarted_call(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(0))
        attempts, waits = [], []

        async def call():
            attempts.append(1)
            return "unreachable"

        async def sleep(delay):
            waits.append(delay)

        with patch.object(moderation, "LLM_QUEUE_TIMEOUT", 0.02), \
                patch.object(moderation.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.TimeoutError):
                await host._run_llm_with_limits(call)
        self.assertEqual(attempts, [])
        self.assertEqual(waits, [])

    async def test_cancellation_during_call_is_not_retried(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        started = asyncio.Event()
        attempts, waits = [], []

        async def call():
            attempts.append(1)
            started.set()
            await asyncio.Event().wait()

        async def sleep(delay):
            waits.append(delay)

        with patch.object(moderation.asyncio, "sleep", sleep):
            task = asyncio.create_task(host._run_llm_with_limits(call))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(attempts, [1])
        self.assertEqual(waits, [])
        self.assertFalse(host._llm_semaphore.locked())

    async def test_cancelled_backoff_stops_retries_and_does_not_block_other_calls(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        sleeping = asyncio.Event()
        attempts, waits = [], []

        async def failing_call():
            attempts.append(1)
            raise ConnectionError("down")

        async def other_call():
            return "another message completed"

        async def sleep(delay):
            waits.append(delay)
            sleeping.set()
            await asyncio.Event().wait()

        with patch.object(moderation.asyncio, "sleep", sleep):
            task = asyncio.create_task(host._run_llm_with_limits(failing_call))
            await asyncio.wait_for(sleeping.wait(), 1)
            result = await asyncio.wait_for(host._run_llm_with_limits(other_call), 1)
            self.assertEqual(result, "another message completed")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(attempts, [1])
        self.assertEqual(waits, [10])
        self.assertFalse(host._llm_semaphore.locked())

    async def test_ocr_empty_failure_uses_same_retry_policy(self):
        host = _ModerationHarness(semaphore=asyncio.Semaphore(1))
        host._ocr_semaphore = asyncio.Semaphore(1)
        attempts, waits = [], []

        async def llm_generate(**_kwargs):
            attempts.append(1)
            if len(attempts) <= 3:
                raise ConnectionError("temporary image provider failure")
            return types.SimpleNamespace(completion_text="image text")

        async def sleep(delay):
            waits.append(delay)
            self.assertFalse(host._llm_semaphore.locked())

        host.context = types.SimpleNamespace(llm_generate=llm_generate)
        with patch.object(moderation.asyncio, "sleep", sleep):
            result = await host._call_llm_ocr("https://example.com/image.png")
        self.assertEqual(result, "image text")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(waits, [10, 20, 30])
        self.assertFalse(host._ocr_semaphore.locked())
