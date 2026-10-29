"""Behavioral regression tests for the AI special-number (relay) detector.

The detector is default-OFF and must never fire without an explicit global
proof (two acknowledgement flags true + the exact acknowledgement phrase) *and*
a per-group double opt-in (mode + zero-guard). These tests drive the real
``_handle_message`` pipeline end to end so the existing gates
(precheck/whitelist/disclaimer, admin exemption, moderation switch), forward
handling (``scan_forward_msg``), the recall-only penalty and the truthful
logging of a rejected recall are all exercised through production code.

The project does not require AstrBot at test collection time, so the same tiny
import shim used by ``test_nested_forward`` is reused.
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import ai_relay  # noqa: E402  (module under test; plugin root on sys.path)


_ASTRBOT_CHAIN = (
    # (module name, is_package)
    ("astrbot", True),
    ("astrbot.api", True),
    ("astrbot.core", True),
    ("astrbot.core.platform", True),
    ("astrbot.core.platform.sources", True),
    ("astrbot.core.platform.sources.aiocqhttp", True),
    ("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event", False),
)


def _install_astrbot_stubs():
    """确保 moderation.py 依赖的 astrbot 模块链齐备。

    其它测试（如 test_ai_relay_config）可能只装了部分桩（仅 ``astrbot`` +
    ``astrbot.api``）；这里逐个补齐缺失模块并给父包挂上 ``__path__`` 与子模块
    属性，避免 ``astrbot not package`` / ``ModuleNotFoundError astrbot.core``。
    """
    for name, is_package in _ASTRBOT_CHAIN:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        if is_package and not hasattr(module, "__path__"):
            module.__path__ = []

    api = sys.modules["astrbot.api"]
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    event_module = sys.modules[
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    ]
    if not hasattr(event_module, "AiocqhttpMessageEvent"):
        event_module.AiocqhttpMessageEvent = object

    for parent_name, child_name in (
        ("astrbot", "astrbot.api"),
        ("astrbot", "astrbot.core"),
        ("astrbot.core", "astrbot.core.platform"),
        ("astrbot.core.platform", "astrbot.core.platform.sources"),
        ("astrbot.core.platform.sources",
         "astrbot.core.platform.sources.aiocqhttp"),
        ("astrbot.core.platform.sources.aiocqhttp",
         "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    ):
        setattr(sys.modules[parent_name], child_name.rsplit(".", 1)[1],
                sys.modules[child_name])


def _load_moderation():
    _install_astrbot_stubs()
    path = PLUGIN_ROOT / "moderation.py"
    spec = importlib.util.spec_from_file_location("group_guardian_moderation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


moderation = _load_moderation()

# The acknowledgement phrase the detector must require verbatim.
ACK_PHRASE = "我确认启用AI特殊检测并承担误撤回风险"
HIT_TEXT = "012"


class _Event:
    def __init__(self, chain, client=None, message_id="", message_seq=0,
                 timestamp=0, sender_name="tester"):
        self._chain = chain
        self.client = client
        self.raw_event = {"user_id": 2, "message_seq": message_seq,
                          "time": timestamp}
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self._sender_name = sender_name
        self.stop_calls = 0
        self.results = []

    def get_messages(self):
        return self._chain

    def get_sender_name(self):
        return self._sender_name

    def plain_result(self, text):
        item = types.SimpleNamespace(text=text)
        self.results.append(item)
        return item

    def stop_event(self):
        self.stop_calls += 1


class _Client:
    """Minimal OneBot client returning canned ``get_forward_msg`` payloads."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        return {"data": self.responses[str(message_id)]}


class _ContainsMatcher:
    def __init__(self, *needles):
        self.needles = needles

    def is_match(self, text):
        return any(needle in text for needle in self.needles)

    def first_match(self, text):
        for needle in self.needles:
            if needle in text:
                start = text.index(needle)
                return start + len(needle) - 1, needle, "test"
        return None


def _enabled_cfg(**overrides):
    """Global config with the detector fully armed, then apply overrides."""
    cfg = {
        "ai_relay_mode_enabled": True,
        "ai_relay_zero_guard_enabled": True,
        "ai_relay_ack_recall": True,
        "ai_relay_ack_false_positive": True,
        "ai_relay_risk_ack": ACK_PHRASE,
    }
    cfg.update(overrides)
    return cfg


class _AiRelayHarness(moderation.ModerationMixin):
    """Drive the real ``_handle_message`` with the AI-relay surface stubbed."""

    auto_moderate_enabled = True

    def __init__(self, cfg=None, group_cfg=None, recall_result=True,
                 admin=False, admin_exempt=False, pre_check=False):
        self._swear_matcher = _ContainsMatcher("never-present")
        self._compiled_lexicon = {}
        self.config = dict(cfg or {})
        self._group_cfg_values = dict(group_cfg or {})
        self.recall_result = recall_result
        self.admin = admin
        self.admin_exempt = admin_exempt
        self.pre_check = pre_check
        self.recalls = []
        self.ocr_calls = 0
        self.llm_calls = 0
        self.mutes = 0
        self.kicks = 0
        self.logs = []

    # ---- config surface (mirrors UtilitiesMixin._cfg semantics) ----
    @staticmethod
    def _parse_bool_str(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")

    def _cfg(self, key, default=True, group_id=None):
        if group_id and key in self._group_cfg_values:
            return self._parse_bool_str(self._group_cfg_values[key])
        if key in self.config:
            return self._parse_bool_str(self.config[key])
        return default

    def _cfg_int(self, key, default=0, group_id=None):
        return default

    def _cfg_str(self, key, default="", group_id=None):
        if group_id and key in self._group_cfg_values:
            return str(self._group_cfg_values[key])
        return str(self.config.get(key, default))

    @staticmethod
    def _lexicon_switch_map(group_id=None):
        return {}

    @staticmethod
    def _check_lexicon(text):
        return {}

    # ---- pipeline gates ----
    def _pre_check_message(self, _event, _group_id, _user_id):
        return self.pre_check

    @staticmethod
    def _get_group_id(_event):
        return "1"

    @staticmethod
    def _try_get_sender_id(_event):
        return "2"

    async def _is_exempt_official_bot(self, _event, _group_id, _user_id):
        return False

    async def _anti_flood_guard(self, _event, _group_id):
        return False, None

    async def _is_admin(self, _event):
        return self.admin

    async def _handle_user_blacklist(self, *_args):
        return False, None

    @staticmethod
    def _get_group_override(_group_id, _key):
        return None

    async def _handle_qq_favorite(self, *_args):
        return False, None

    # ---- downstream stages (must stay untouched on an AI-relay hit) ----
    async def _apply_ocr(self, text, _image_urls, _event, _group_id):
        self.ocr_calls += 1
        return text

    async def _call_llm_for_moderation(self, _event, _text, _hit_types, **_kwargs):
        self.llm_calls += 1
        return {"violation": False, "fallback": False}

    async def _mute_member(self, *_args, **_kwargs):
        self.mutes += 1
        return True

    async def _kick_member(self, *_args, **_kwargs):
        self.kicks += 1
        return True

    # ---- moderation side effects under test ----
    async def _recall_msg(self, _event, msg_id):
        self.recalls.append(str(msg_id))
        return self.recall_result

    def _log_moderation(self, group_id, user_id, user_name, text, action,
                        reason="", image_urls=None):
        self.logs.append(action)

    async def _get_client(self, event):
        return getattr(event, "client", None)

    @staticmethod
    def _extract_data_result(result):
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result


def _run(harness, event):
    async def consume():
        return [item async for item in harness._handle_message(event)]

    return asyncio.run(consume())


def _fire(cfg=None, group_cfg=None, text=HIT_TEXT, recall_result=True, **kwargs):
    """Drive one plain-text message through the pipeline; return state."""
    harness = _AiRelayHarness(cfg=cfg, group_cfg=group_cfg,
                              recall_result=recall_result, **kwargs)
    event = _Event([{"type": "text", "data": {"text": text}}], message_id="m1")
    items = _run(harness, event)
    return harness, event, items


def _fire_forward(cfg, text=HIT_TEXT):
    client = _Client({"root": {"messages": [{"message": [
        {"type": "text", "data": {"text": text}},
    ]}]}})
    event = _Event([{"type": "forward", "data": {"id": "root"}}],
                   client=client, message_id="m1")
    harness = _AiRelayHarness(cfg=cfg)
    items = _run(harness, event)
    return harness, event, items


class AiRelaySharedContractTests(unittest.TestCase):
    def test_risk_acknowledged_requires_two_flags_and_exact_phrase(self):
        base = {
            "ai_relay_ack_recall": True,
            "ai_relay_ack_false_positive": True,
            "ai_relay_risk_ack": ACK_PHRASE,
        }
        self.assertTrue(ai_relay.ai_relay_risk_acknowledged(base))
        self.assertFalse(ai_relay.ai_relay_risk_acknowledged({}))
        for mutate in (
            {"ai_relay_ack_recall": False},
            {"ai_relay_ack_false_positive": False},
            {"ai_relay_risk_ack": ACK_PHRASE + "。"},
            {"ai_relay_risk_ack": "我确认"},
        ):
            with self.subTest(mutate=mutate):
                cfg = dict(base)
                cfg.update(mutate)
                self.assertFalse(ai_relay.ai_relay_risk_acknowledged(cfg))

    def test_risk_acknowledged_rejects_falsy_strings_and_padding(self):
        # 假值字符串绝不能被当成真值；确认短语必须逐字一致（不做裁剪）。
        for falsy in ("false", "0", "no", "off", "", "否", None):
            with self.subTest(falsy=falsy):
                self.assertFalse(ai_relay.ai_relay_risk_acknowledged({
                    "ai_relay_ack_recall": falsy,
                    "ai_relay_ack_false_positive": "true",
                    "ai_relay_risk_ack": ACK_PHRASE,
                }))
        self.assertFalse(ai_relay.ai_relay_risk_acknowledged({
            "ai_relay_ack_recall": True,
            "ai_relay_ack_false_positive": True,
            "ai_relay_risk_ack": "  " + ACK_PHRASE + "  ",
        }))
        self.assertTrue(ai_relay.ai_relay_risk_acknowledged({
            "ai_relay_ack_recall": "true",
            "ai_relay_ack_false_positive": "1",
            "ai_relay_risk_ack": ACK_PHRASE,
        }))
        # 未知取值（非 0/1 的数字、nan、容器对象）不得因 Python 真值被当成已确认。
        for unknown in (2, -1, 1.0, float("nan"), [1], {"a": 1}):
            with self.subTest(unknown=unknown):
                self.assertFalse(ai_relay.ai_relay_risk_acknowledged({
                    "ai_relay_ack_recall": unknown,
                    "ai_relay_ack_false_positive": True,
                    "ai_relay_risk_ack": ACK_PHRASE,
                }))

    def test_zero_guard_pattern_is_broad_by_design(self):
        # 命中：0. 任意位置（含小数/版本号）与两侧非数字的 0XX / 00XX。
        for text in ("0.", "0.0", "0.5", "10.5", "版本 10.5", "012", "0012",
                     "x0012y", "前缀012后缀"):
            self.assertTrue(ai_relay.ai_relay_zero_guard_hit(text), text)
        # 未命中：普通数字串不得被误伤。
        for text in ("", "hello world", "123", "1234", "100", "1.0", "abc"):
            self.assertFalse(ai_relay.ai_relay_zero_guard_hit(text), text)


class AiRelayGateTests(unittest.TestCase):
    def test_off_by_default_never_recalls(self):
        harness, event, _ = _fire(cfg={})
        self.assertEqual(harness.recalls, [])
        self.assertEqual(harness.logs, [])
        self.assertEqual(event.stop_calls, 0)

    def test_config_gate_cases_block_detection(self):
        cases = {
            "mode_off": _enabled_cfg(ai_relay_mode_enabled=False),
            "zero_guard_off": _enabled_cfg(ai_relay_zero_guard_enabled=False),
            "missing_ack": {"ai_relay_mode_enabled": True,
                            "ai_relay_zero_guard_enabled": True},
            "wrong_phrase": _enabled_cfg(ai_relay_risk_ack="我确认"),
            "single_flag": _enabled_cfg(ai_relay_ack_false_positive=False),
            "false_string_mode": _enabled_cfg(ai_relay_mode_enabled="false"),
            "false_string_ack": _enabled_cfg(ai_relay_ack_recall="off"),
        }
        for name, cfg in cases.items():
            with self.subTest(case=name):
                harness, _, _ = _fire(cfg=cfg)
                self.assertEqual(harness.recalls, [])

    def test_string_flags_enable_detection(self):
        harness, _, _ = _fire(cfg={
            "ai_relay_mode_enabled": "true",
            "ai_relay_zero_guard_enabled": "1",
            "ai_relay_ack_recall": "true",
            "ai_relay_ack_false_positive": "1",
            "ai_relay_risk_ack": ACK_PHRASE,
        })
        self.assertEqual(harness.recalls, ["m1"])

    def test_existing_gates_block_detector(self):
        # 1) 审核总开关关闭：整条跳过，不做任何动作。
        harness = _AiRelayHarness(cfg=_enabled_cfg())
        harness.auto_moderate_enabled = False
        event = _Event([{"type": "text", "data": {"text": HIT_TEXT}}],
                       message_id="m1")
        _run(harness, event)
        self.assertEqual(harness.recalls, [])
        self.assertEqual(event.stop_calls, 0)

        # 2) precheck（白名单/免责声明/开关）拦截：不撤回、不记录。
        harness, _, _ = _fire(cfg=_enabled_cfg(), pre_check=True)
        self.assertEqual(harness.recalls, [])
        self.assertEqual(harness.logs, [])

        # 3) 管理员豁免：不撤回。
        harness, _, _ = _fire(cfg=_enabled_cfg(moderation_admin_exempt=True),
                              admin=True, admin_exempt=True)
        self.assertEqual(harness.recalls, [])


class AiRelayGroupOverrideTests(unittest.TestCase):
    def test_group_override_cases(self):
        cases = {
            # 全局主开关关闭 -> 撤销证明，群覆盖无法启用。
            "master_disable_revokes": (
                _enabled_cfg(ai_relay_mode_enabled=False),
                {"ai_relay_mode_enabled": True,
                 "ai_relay_zero_guard_enabled": True},
                []),
            # 群内开启 zero_guard 覆盖全局关闭。
            "group_enables": (
                _enabled_cfg(ai_relay_zero_guard_enabled=False),
                {"ai_relay_zero_guard_enabled": True},
                ["m1"]),
            # 群内关闭 mode 覆盖全局开启。
            "group_disables": (
                _enabled_cfg(),
                {"ai_relay_mode_enabled": False},
                []),
        }
        for name, (cfg, group_cfg, expected) in cases.items():
            with self.subTest(case=name):
                harness, _, _ = _fire(cfg=cfg, group_cfg=group_cfg)
                self.assertEqual(harness.recalls, expected)


class AiRelayHitTests(unittest.TestCase):
    def test_hit_recalls_only_without_mute_llm_or_ocr(self):
        harness, event, items = _fire(cfg=_enabled_cfg())
        self.assertEqual(harness.recalls, ["m1"])
        self.assertEqual(harness.mutes, 0)
        self.assertEqual(harness.kicks, 0)
        self.assertEqual(harness.llm_calls, 0)
        self.assertEqual(harness.ocr_calls, 0)
        self.assertEqual(event.stop_calls, 1)
        self.assertEqual(len(harness.logs), 1)
        self.assertIn("撤回", harness.logs[0])
        self.assertEqual(len(items), 1)

    def test_version_like_number_hits_intentionally(self):
        harness, _, _ = _fire(cfg=_enabled_cfg(), text="版本 10.5 发布")
        self.assertEqual(harness.recalls, ["m1"])

    def test_auto_notice_can_be_silenced(self):
        harness, _, items = _fire(cfg=_enabled_cfg(auto_moderate_notice=False))
        self.assertEqual(harness.recalls, ["m1"])
        self.assertEqual(items, [])
        self.assertEqual(len(harness.logs), 1)
        self.assertIn("撤回", harness.logs[0])

    def test_rejected_recall_logs_without_claiming_success(self):
        harness, _, items = _fire(cfg=_enabled_cfg(), recall_result=False)
        self.assertEqual(harness.recalls, ["m1"])
        self.assertEqual(harness.mutes, 0)
        self.assertEqual(harness.kicks, 0)
        self.assertEqual(harness.llm_calls, 0)
        self.assertEqual(items, [])
        self.assertEqual(len(harness.logs), 1)
        self.assertNotIn("撤回", harness.logs[0])


class AiRelayForwardTests(unittest.TestCase):
    def test_forward_content_follows_scan_forward_msg(self):
        cases = {
            "scan_forward_on": (_enabled_cfg(), ["m1"]),
            "scan_forward_off": (_enabled_cfg(scan_forward_msg=False), []),
            "detector_off": ({}, []),
        }
        for name, (cfg, expected) in cases.items():
            with self.subTest(case=name):
                harness, _, _ = _fire_forward(cfg)
                self.assertEqual(harness.recalls, expected)


class AiRelayPassThroughTests(unittest.TestCase):
    def test_non_hit_passes_through_untouched(self):
        harness, event, _ = _fire(cfg=_enabled_cfg(), text="hello world")
        self.assertEqual(harness.recalls, [])
        self.assertEqual(harness.logs, [])
        self.assertEqual(harness.mutes, 0)
        self.assertEqual(event.stop_calls, 0)

    def test_missing_message_id_passes_through_without_stopping(self):
        # 无消息 ID 时无法撤回：检测必须放行到后续正常审核，而不是空停事件。
        harness = _AiRelayHarness(cfg=_enabled_cfg())
        event = _Event([{"type": "text", "data": {"text": HIT_TEXT}}],
                       message_id="")
        items = _run(harness, event)
        self.assertEqual(harness.recalls, [])
        self.assertEqual(harness.logs, [])
        self.assertEqual(items, [])
        self.assertEqual(event.stop_calls, 0)
        # 后续审核阶段确实继续执行（OCR 阶段被调用）。
        self.assertEqual(harness.ocr_calls, 1)


if __name__ == "__main__":
    unittest.main()
