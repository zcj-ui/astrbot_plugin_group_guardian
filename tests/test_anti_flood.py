"""Regression tests for anti-flood repeat and rate detection."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anti_flood


AntiFloodMixin = anti_flood.AntiFloodMixin


class _Harness(AntiFloodMixin):
    def __init__(self, **overrides):
        self.values = {
            "anti_flood_rate_per_second": 0,
            "anti_flood_rate_per_minute": 0,
            "anti_flood_rate_per_hour": 0,
            "anti_flood_night_enabled": False,
            "repeat_detect_enabled": True,
            "repeat_detect_window_seconds": 120,
            "repeat_detect_count": 3,
            "long_text_detect_enabled": False,
            "long_text_threshold": 0,
        }
        self.values.update(overrides)
        self._init_anti_flood()

    def _cfg(self, key, default=False, group_id=None):
        return self.values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.values.get(key, default))


class AntiFloodRepeatTests(unittest.TestCase):
    @staticmethod
    def _record(harness, *texts):
        with patch.object(anti_flood.time, "time", return_value=1000.0):
            for index, value in enumerate(texts, start=1):
                harness._record_message("100", "200", str(index), value)
            return harness._check_anti_flood("100", "200")

    def test_separate_images_do_not_trigger_repeat_detection(self):
        harness = _Harness(repeat_detect_count=5)

        detected, info = self._record(
            harness, "[图片]", "[图片]", "[图片]", "[图片]", "[图片]"
        )

        self.assertFalse(detected)
        self.assertIsNone(info)

    def test_multiple_media_placeholders_are_not_repeat_keys(self):
        placeholders = (
            "[图片][图片]",
            " [图片] \n [商城表情] [表情] ",
            "[语音][视频][文件]",
            "[合并转发消息] [空消息]",
        )
        for value in placeholders:
            with self.subTest(value=value):
                detected, info = self._record(_Harness(), value, value, value)
                self.assertFalse(detected)
                self.assertIsNone(info)

    def test_repeated_text_still_triggers_on_configured_count(self):
        detected, info = self._record(
            _Harness(), "same message", "same message", "same message"
        )

        self.assertTrue(detected)
        self.assertEqual(info["rate"], "重复消息")
        self.assertEqual(info["count"], 3)
        self.assertEqual(info["msg_ids"], ["3", "2", "1"])

    def test_text_with_image_remains_repeatable(self):
        detected, info = self._record(
            _Harness(), "same caption[图片]", "same caption[图片]", "same caption[图片]"
        )

        self.assertTrue(detected)
        self.assertEqual(info["rate"], "重复消息")

    def test_latest_media_does_not_fall_back_to_older_text(self):
        detected, info = self._record(
            _Harness(repeat_detect_count=2), "older text", "older text", "[图片]"
        )

        self.assertFalse(detected)
        self.assertIsNone(info)

    def test_images_still_count_toward_rate_limits(self):
        harness = _Harness(anti_flood_rate_per_second=2, repeat_detect_count=2)

        detected, info = self._record(harness, "[图片]", "[图片]", "[图片]")

        self.assertTrue(detected)
        self.assertEqual(info["rate"], "每秒")
        self.assertEqual(info["count"], 3)

    def test_night_window_supports_cross_midnight_boundaries(self):
        harness = _Harness(
            anti_flood_night_enabled=True,
            anti_flood_night_start_hour=23,
            anti_flood_night_end_hour=6,
        )

        def localtime(value):
            return SimpleNamespace(tm_hour=int(value))

        with patch.object(anti_flood.time, "localtime", side_effect=localtime):
            self.assertTrue(harness._is_anti_flood_night_time(now_ts=23))
            self.assertTrue(harness._is_anti_flood_night_time(now_ts=5))
            self.assertFalse(harness._is_anti_flood_night_time(now_ts=6))
            self.assertFalse(harness._is_anti_flood_night_time(now_ts=22))

    def test_night_window_same_start_and_end_is_all_day(self):
        harness = _Harness(
            anti_flood_night_enabled=True,
            anti_flood_night_start_hour=4,
            anti_flood_night_end_hour=4,
        )

        with patch.object(
            anti_flood.time,
            "localtime",
            return_value=SimpleNamespace(tm_hour=12),
        ):
            self.assertTrue(harness._is_anti_flood_night_time(now_ts=123))

    def test_night_limits_replace_all_three_rate_windows(self):
        harness = _Harness(
            anti_flood_rate_per_second=50,
            anti_flood_rate_per_minute=60,
            anti_flood_rate_per_hour=70,
            anti_flood_night_enabled=True,
            anti_flood_night_start_hour=0,
            anti_flood_night_end_hour=6,
            anti_flood_night_rate_per_second=1,
            anti_flood_night_rate_per_minute=2,
            anti_flood_night_rate_per_hour=3,
        )

        with patch.object(
            anti_flood.time,
            "localtime",
            return_value=SimpleNamespace(tm_hour=2),
        ):
            limits = harness._get_effective_rate_limits(now_ts=1000)

        self.assertEqual(
            {"per_second": 1, "per_minute": 2, "per_hour": 3, "night": True},
            limits,
        )

    def test_night_rate_limit_reports_night_label(self):
        harness = _Harness(
            anti_flood_rate_per_second=50,
            anti_flood_night_enabled=True,
            anti_flood_night_start_hour=0,
            anti_flood_night_end_hour=6,
            anti_flood_night_rate_per_second=1,
            repeat_detect_count=99,
        )
        with patch.object(anti_flood.time, "time", return_value=1000.0), patch.object(
            anti_flood.time,
            "localtime",
            return_value=SimpleNamespace(tm_hour=2),
        ):
            harness._record_message("100", "200", "1", "first")
            harness._record_message("100", "200", "2", "second")
            detected, info = harness._check_anti_flood("100", "200")

        self.assertTrue(detected)
        self.assertEqual("夜间每秒", info["rate"])
        self.assertEqual(1, info["limit"])

    def test_duplicate_delivery_does_not_create_an_extra_rate_event(self):
        harness = _Harness()

        with patch.object(anti_flood.time, "monotonic", return_value=1000.0):
            self.assertFalse(
                harness._anti_flood_event_is_duplicate("100", "200", "msg-1")
            )
            self.assertTrue(
                harness._anti_flood_event_is_duplicate("100", "200", "msg-1")
            )
            self.assertFalse(
                harness._anti_flood_event_is_duplicate("100", "200", "msg-2")
            )

    def test_event_identity_falls_back_to_raw_message_id(self):
        event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id=""),
            raw_event={"message_id": 456},
        )

        harness = _Harness()
        self.assertEqual(
            harness._anti_flood_event_message_identity(event),
            ("456", "456"),
        )

    def test_event_identity_reads_deep_nested_raw_message_id(self):
        event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id=""),
            raw_event={
                "data": {"event": {"raw_message": {"msg_id": 789}}},
            },
        )
        harness = _Harness()

        tracking_id, recall_id = harness._anti_flood_event_message_identity(event)

        self.assertEqual((tracking_id, recall_id), ("789", "789"))
        with patch.object(anti_flood.time, "monotonic", return_value=1000.0):
            self.assertFalse(
                harness._anti_flood_event_is_duplicate("100", "200", tracking_id)
            )
            self.assertTrue(
                harness._anti_flood_event_is_duplicate("100", "200", tracking_id)
            )

    def test_sequence_fallback_is_not_recallable_and_missing_id_is_still_counted(self):
        sequence_event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id="", message_seq=12),
            raw_event={},
        )
        harness = _Harness(anti_flood_rate_per_second=2)

        self.assertEqual(
            harness._anti_flood_event_message_identity(sequence_event),
            ("seq:12", ""),
        )
        self.assertEqual(
            harness._anti_flood_recallable_message_id("seq:12"),
            "",
        )

        with patch.object(anti_flood.time, "time", return_value=1000.0):
            for index in range(3):
                harness._record_message("100", "200", "", f"message-{index}")

            detected, info = harness._check_anti_flood("100", "200")
        self.assertTrue(detected)
        self.assertEqual(info["count"], 3)

    def test_cooldown_expiry_discards_messages_seen_while_suppressed(self):
        harness = _Harness()
        with patch.object(anti_flood.time, "time", return_value=1000.0):
            harness._record_message("100", "200", "before", "before penalty")
            harness._mark_anti_flood_penalty("100", "200", 30)
        with patch.object(anti_flood.time, "time", return_value=1010.0):
            harness._record_message("100", "200", "during", "during cooldown")
        with patch.object(anti_flood.time, "time", return_value=1030.0):
            self.assertFalse(harness._anti_flood_in_cooldown("100", "200"))

        self.assertNotIn("100", harness._anti_flood_data)


if __name__ == "__main__":
    unittest.main()
