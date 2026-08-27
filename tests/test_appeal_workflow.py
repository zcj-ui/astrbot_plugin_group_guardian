"""Regression coverage for the private appeal state machine."""

import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_appeal_module():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = getattr(api, "logger", types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    ))
    event_module = sys.modules.setdefault(
        "astrbot.api.event", types.ModuleType("astrbot.api.event")
    )
    event_module.AstrMessageEvent = object
    astrbot.api = api
    api.event = event_module

    package_name = "group_guardian_appeal_workflow_tests"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.appeal", ROOT / "appeal.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


appeal = _load_appeal_module()


class _Storage:
    def __init__(self):
        self.appeal = {
            "id": 1,
            "group_id": "group",
            "user_id": "user",
            "reason": "repeat messages",
            "penalty": "mute",
            "mute_duration": 60,
            "status": "waiting",
            "created_at": 1,
            "expire_at": 9_999_999_999,
            "attempts": 0,
            "prompt_sent": False,
        }

    def get_waiting_appeal(self, user_id):
        if (str(user_id) == self.appeal["user_id"]
                and self.appeal["status"] == "waiting"):
            return self.appeal
        return None

    def mark_appeal_prompted(self, appeal_id):
        if (int(appeal_id) == self.appeal["id"]
                and self.appeal["status"] == "waiting"
                and not self.appeal["prompt_sent"]):
            self.appeal["prompt_sent"] = True
            return True
        return False

    def claim_appeal_attempt(self, appeal_id, max_attempts=2, now_ts=None):
        if (int(appeal_id) != self.appeal["id"]
                or self.appeal["status"] != "waiting"
                or self.appeal["attempts"] >= int(max_attempts)):
            return 0
        if now_ts is not None and self.appeal["expire_at"] <= int(now_ts):
            return 0
        self.appeal["status"] = "judging"
        self.appeal["attempts"] += 1
        return self.appeal["attempts"]

    def reopen_appeal_waiting(self, appeal_id, decrement_attempt=False):
        if (int(appeal_id) != self.appeal["id"]
                or self.appeal["status"] != "judging"):
            return False
        self.appeal["status"] = "waiting"
        if decrement_attempt:
            self.appeal["attempts"] = max(0, self.appeal["attempts"] - 1)
        return True

    def reopen_active_appeal(self, appeal_id, now_ts, decrement_attempt=False):
        if (int(appeal_id) != self.appeal["id"]
                or self.appeal["status"] != "judging"
                or self.appeal["expire_at"] <= int(now_ts)):
            return False
        self.appeal["status"] = "waiting"
        if decrement_attempt:
            self.appeal["attempts"] = max(0, self.appeal["attempts"] - 1)
        return True

    def finalize_appeal_if_active(self, appeal_id, status, now_ts):
        if (int(appeal_id) != self.appeal["id"]
                or self.appeal["status"] != "judging"
                or self.appeal["expire_at"] <= int(now_ts)):
            return False
        self.appeal["status"] = status
        self.appeal["decided_at"] = int(now_ts)
        return True

    def expire_appeal_if_due(self, appeal_id, now_ts):
        if (int(appeal_id) != self.appeal["id"]
                or self.appeal["status"] not in {"waiting", "judging"}
                or self.appeal["expire_at"] > int(now_ts)):
            return False
        self.appeal["status"] = "expired"
        self.appeal["decided_at"] = int(now_ts)
        return True

    def set_appeal_status(self, appeal_id, status, decided_at):
        if int(appeal_id) != self.appeal["id"]:
            return False
        self.appeal["status"] = status
        self.appeal["decided_at"] = int(decided_at)
        return True


class _PrivateEvent:
    def __init__(self, segments, *, message_str="", raw_extra=None):
        self._segments = segments
        self.message_obj = types.SimpleNamespace(message=segments)
        self.message_str = message_str
        self.raw_event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": "user",
        }
        self.raw_event.update(raw_extra or {})

    def get_messages(self):
        return self._segments

    @staticmethod
    def plain_result(text):
        return text

    @staticmethod
    def get_sender_name():
        return "tester"


class _Harness(appeal.AppealMixin):
    def __init__(self):
        self._storage = _Storage()
        self.judgements = []
        self.logs = []

    @staticmethod
    def _try_get_sender_id(_event):
        return "user"

    @staticmethod
    def _cfg(name, default=None, group_id=""):
        return True if name == "appeal_enabled" else default

    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default

    async def _judge_appeal(self, group_id, user_id, statement, appeal_data):
        self.judgements.append((group_id, user_id, statement, dict(appeal_data)))
        return {"appeal_valid": False, "reason": "maintain"}

    def _log_moderation(self, *args):
        self.logs.append(args)


class _ExpiresDuringReviewHarness(_Harness):
    async def _judge_appeal(self, group_id, user_id, statement, appeal_data):
        self.judgements.append((group_id, user_id, statement, dict(appeal_data)))
        self._storage.appeal["expire_at"] = int(time.time())
        return {"appeal_valid": False, "reason": "maintain"}


class AppealWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _collect(harness, event):
        return [item async for item in harness._handle_private_appeal(event)]

    async def test_empty_private_event_does_not_start_an_appeal(self):
        harness = _Harness()

        self.assertFalse(harness._is_user_private_message_event(_PrivateEvent([])))
        self.assertFalse(harness._is_user_private_message_event(_PrivateEvent([
            {"type": "text", "data": {"text": ""}},
        ])))
        self.assertTrue(harness._is_user_private_message_event(_PrivateEvent([
            {"type": "image", "data": {"url": "x"}},
        ])))
        cq_image = _PrivateEvent([])
        cq_image.message_str = "[CQ:image,file=fixture]"
        self.assertTrue(harness._is_user_private_message_event(cq_image))
        for empty in ("None", "null", "[]", "MessageChain([])"):
            with self.subTest(empty=empty):
                self.assertFalse(
                    harness._is_user_private_message_event(
                        _PrivateEvent([], message_str=empty)
                    )
                )

        bot_echo = _PrivateEvent(
            [{"type": "text", "data": {"text": "bot prompt"}}],
            raw_extra={"user_id": "999", "self_id": "999"},
        )
        self.assertFalse(harness._is_user_private_message_event(bot_echo))
        getter_echo = _PrivateEvent(
            [{"type": "text", "data": {"text": "bot prompt"}}],
            raw_extra={"user_id": "999"},
        )
        getter_echo.get_self_id = lambda: "999"
        self.assertFalse(harness._is_user_private_message_event(getter_echo))

        message_object_echo = _PrivateEvent(
            [{"type": "text", "data": {"text": "bot reply"}}],
            raw_extra={"user_id": "other"},
        )
        message_object_echo.message_obj.self_id = "bot"
        message_object_echo.message_obj.sender = types.SimpleNamespace(user_id="bot")
        self.assertFalse(
            harness._is_user_private_message_event(message_object_echo)
        )

        flag_echo = _PrivateEvent(
            [{"type": "text", "data": {"text": "bot reply"}}],
            raw_extra={"user_id": "user", "from_me": "true"},
        )
        self.assertFalse(harness._is_user_private_message_event(flag_echo))

        false_flag_user = _PrivateEvent(
            [{"type": "text", "data": {"text": "user reason"}}],
            raw_extra={"user_id": "user", "from_me": "false", "self_id": "bot"},
        )
        self.assertTrue(harness._is_user_private_message_event(false_flag_user))
        self.assertTrue(harness._is_user_private_message_event(_PrivateEvent(
            [{"type": "text", "data": {"text": "user reason"}}],
            raw_extra={"user_id": "user", "self_id": "999"},
        )))

    async def test_text_extraction_accepts_plain_and_nested_adapter_shapes(self):
        harness = _Harness()
        cases = [
            ([{"type": "plain", "data": {"text": "plain reason"}}], "plain reason"),
            ([{"type": "PLAIN", "data": "string reason"}], "string reason"),
            ([{"type": "text", "text": "top-level reason"}], "top-level reason"),
            (({"message": [{"type": "plain", "data": {"text": "wrapped reason"}}]},), "wrapped reason"),
        ]
        for segments, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    harness._extract_private_statement(_PrivateEvent(segments)),
                    expected,
                )

        raw_only = _PrivateEvent([], raw_extra={
            "message": [{"type": "text", "data": {"text": "raw event reason"}}],
        })
        self.assertTrue(harness._is_user_private_message_event(raw_only))
        self.assertEqual(
            harness._extract_private_statement(raw_only), "raw event reason"
        )

    async def test_non_text_prompt_is_sent_once_and_two_text_attempts_are_allowed(self):
        harness = _Harness()
        non_text = _PrivateEvent([{"type": "image", "data": {"url": "x"}}])

        self.assertEqual(
            await self._collect(harness, non_text),
            [harness.APPEAL_TEXT_PROMPT],
        )
        self.assertEqual(await self._collect(harness, non_text), [])
        self.assertTrue(harness._storage.appeal["prompt_sent"])

        first = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "first reason"}},
        ]))
        self.assertEqual(len(first), 2)
        self.assertEqual(harness._storage.appeal["attempts"], 1)
        self.assertEqual(harness._storage.appeal["status"], "waiting")

        second = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "second reason"}},
        ]))
        self.assertEqual(len(second), 2)
        self.assertEqual(harness._storage.appeal["attempts"], 2)
        self.assertEqual(harness._storage.appeal["status"], "rejected")
        self.assertEqual(
            [judgement[2] for judgement in harness.judgements],
            ["first reason", "second reason"],
        )

    async def test_bot_rejection_echo_does_not_consume_second_appeal_attempt(self):
        harness = _Harness()
        first_event = _PrivateEvent([
            {"type": "text", "data": {"text": "first reason"}},
        ])
        first = await self._collect(harness, first_event)
        self.assertEqual(1, harness._storage.appeal["attempts"])
        self.assertEqual("waiting", harness._storage.appeal["status"])
        self.assertEqual(2, len(first))

        bot_echo = _PrivateEvent(
            [{"type": "text", "data": {"text": first[-1]}}],
            raw_extra={"user_id": "other"},
        )
        bot_echo.message_obj.self_id = "bot"
        bot_echo.message_obj.sender = types.SimpleNamespace(user_id="bot")
        self.assertFalse(harness._is_user_private_message_event(bot_echo))
        if harness._is_user_private_message_event(bot_echo):
            await self._collect(harness, bot_echo)
        self.assertEqual(1, harness._storage.appeal["attempts"])
        self.assertEqual("waiting", harness._storage.appeal["status"])

        second = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "second reason"}},
        ]))
        self.assertEqual(2, harness._storage.appeal["attempts"])
        self.assertEqual("rejected", harness._storage.appeal["status"])
        self.assertEqual(2, len(second))

    async def test_cached_bot_echo_without_adapter_identity_does_not_consume_attempt(self):
        harness = _Harness()
        first = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "first reason"}},
        ]))
        self.assertEqual(1, harness._storage.appeal["attempts"])
        self.assertEqual("waiting", harness._storage.appeal["status"])

        # Some private-message adapters report an outgoing echo with the
        # original user's ID but omit self_id, direction, and sender metadata.
        # It must not spend the remaining appeal opportunity.
        unmarked_echo = _PrivateEvent([
            {"type": "text", "data": {"text": first[-1]}},
        ])
        self.assertTrue(harness._is_private_bot_echo(unmarked_echo))
        self.assertFalse(harness._is_user_private_message_event(unmarked_echo))
        self.assertEqual(1, harness._storage.appeal["attempts"])
        self.assertEqual("waiting", harness._storage.appeal["status"])

        second = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "second reason"}},
        ]))
        self.assertEqual(2, len(second))
        self.assertEqual(2, harness._storage.appeal["attempts"])
        self.assertEqual("rejected", harness._storage.appeal["status"])

    async def test_review_finishing_after_expiry_does_not_reopen_the_appeal(self):
        harness = _ExpiresDuringReviewHarness()

        replies = await self._collect(harness, _PrivateEvent([
            {"type": "text", "data": {"text": "late reason"}},
        ]))

        self.assertEqual(2, len(replies))
        self.assertEqual("你的申诉已超时，处罚维持。", replies[-1])
        self.assertEqual("expired", harness._storage.appeal["status"])
        self.assertEqual([], harness.logs)


if __name__ == "__main__":
    unittest.main()
