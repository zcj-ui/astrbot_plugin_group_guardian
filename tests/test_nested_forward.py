"""Focused regression tests for nested OneBot/AstrBot message extraction.

The project does not require AstrBot at test collection time, so a tiny import
shim is used when the host package is unavailable.
"""

import asyncio
import importlib.util
import json
import re
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch

import anti_flood


def _load_moderation():
    if "astrbot.api" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(debug=lambda *a, **k: None,
                                           warning=lambda *a, **k: None,
                                           info=lambda *a, **k: None,
                                           exception=lambda *a, **k: None)
        core = types.ModuleType("astrbot.core")
        platform = types.ModuleType("astrbot.core.platform")
        sources = types.ModuleType("astrbot.core.platform.sources")
        aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
        event_module = types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
        )
        event_module.AiocqhttpMessageEvent = object
        sys.modules.update({
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.core": core,
            "astrbot.core.platform": platform,
            "astrbot.core.platform.sources": sources,
            "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
        })
    path = Path(__file__).resolve().parents[1] / "moderation.py"
    spec = importlib.util.spec_from_file_location("group_guardian_moderation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


moderation = _load_moderation()
moderation_context = sys.modules[moderation.ModerationContextMixin.__module__]


def _load_utilities():
    package = types.ModuleType("group_guardian")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    automaton = types.ModuleType("group_guardian.automaton")
    automaton.KeywordAutomaton = object
    sys.modules.setdefault("group_guardian", package)
    sys.modules.setdefault("group_guardian.automaton", automaton)
    path = Path(__file__).resolve().parents[1] / "utils.py"
    spec = importlib.util.spec_from_file_location("group_guardian.utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utilities = _load_utilities()


class _Event:
    def __init__(self, chain, client=None, message_id="", message_seq=0, timestamp=0):
        self._chain = chain
        self.client = client
        self.raw_event = {
            "user_id": 2,
            "message_seq": message_seq,
            "time": timestamp,
        }
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.stop_calls = 0

    def get_messages(self):
        return self._chain

    def get_sender_name(self):
        return "tester"

    def stop_event(self):
        self.stop_calls += 1


class _ReadOnlyContextEvent:
    __slots__ = ("message_obj",)

    def __init__(self):
        self.message_obj = types.SimpleNamespace(message_id="")


class _RawCacheContextEvent:
    __slots__ = ("message_obj", "raw_event")

    def __init__(self):
        self.message_obj = types.SimpleNamespace(message_id="")
        self.raw_event = {}


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        return {"data": self.responses[str(message_id)]}


class _SlowClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        await asyncio.sleep(1)
        return {"data": {"messages": []}}


class _StaticClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        return self.result


class _Harness(moderation.ModerationMixin):
    async def _get_client(self, event):
        return event.client

    @staticmethod
    def _extract_data_result(result):
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result


class _FastTimeoutHarness(_Harness):
    _FORWARD_REQUEST_TIMEOUT = 0.01


class _FastTotalTimeoutHarness(_Harness):
    _FORWARD_REQUEST_TIMEOUT = 1.0
    _FORWARD_TOTAL_TIMEOUT = 0.01


class _AsyncGate:
    async def acquire(self):
        return True

    def release(self):
        return None


class _CombinedHarness(moderation.ModerationMixin, utilities.UtilitiesMixin):
    pass


class _LLMHarness(_Harness):
    config = {}

    def __init__(self, response):
        self.response = response
        self._llm_semaphore = _AsyncGate()
        self.last_prompt = ""

    async def _call_llm_safe(self, system_prompt, prompt):
        self.last_prompt = prompt
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    @staticmethod
    def _cfg_str(name, default="", group_id=""):
        return default

    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default


class Plain:
    def __init__(self, text):
        self.text = text


def _negative_squared_url(value):
    return "".join(
        chr(0x1F170 + ord(char.upper()) - ord("A"))
        if char.isascii() and char.isalpha()
        else char
        for char in value
    )


class Reply:
    def __init__(self, text):
        self.text = text
        self.id = "quoted"


class Node:
    def __init__(self, content, data=None):
        self.content = content
        self.data = data or {}


class Json:
    def __init__(self, data):
        self.data = data


class ComponentKind(str, Enum):
    Node = "Node"


class AdapterNode:
    type = ComponentKind.Node

    def __init__(self, content):
        self.content = content


class _FirstMatcher:
    def __init__(self, needle):
        self.needle = needle

    def first_match(self, text):
        start = text.index(self.needle)
        return start + len(self.needle) - 1, self.needle, "ac"


class _ContainsMatcher:
    def __init__(self, *needles):
        self.needles = needles

    def is_match(self, text):
        return any(needle in text for needle in self.needles)

    def first_match(self, text):
        matches = [
            (text.index(needle), needle)
            for needle in self.needles
            if needle in text
        ]
        if not matches:
            return None
        start, needle = min(matches)
        return start + len(needle) - 1, needle, "test"


class _CsBoundaryMatcher:
    @staticmethod
    def _is_ascii_word(char):
        return bool(char) and (char.isascii() and (char.isalnum() or char == "_"))

    def first_match(self, text):
        start = 0
        while True:
            start = text.find("cs", start)
            if start < 0:
                return None
            before = text[start - 1] if start else ""
            after_index = start + 2
            after = text[after_index] if after_index < len(text) else ""
            if not self._is_ascii_word(before) and not self._is_ascii_word(after):
                return start + 2, "cs-boundary", "regex"
            start += 1

    def is_match(self, text):
        return self.first_match(text) is not None


class _SeparatedAssholeMatcher:
    _pattern = re.compile(r"a\s*sshole", re.IGNORECASE)

    def first_match(self, text):
        match = self._pattern.search(text)
        if match is None:
            return None
        return match.end() - 1, match.group(0), "regex"

    def is_match(self, text):
        return self.first_match(text) is not None


class _StreamHarness(_Harness):
    def __init__(self, *needles):
        self._swear_matcher = _ContainsMatcher(*needles)
        self._compiled_lexicon = {}

    @staticmethod
    def _cfg(_name, default=None, group_id=""):
        return default

    @staticmethod
    def _lexicon_switch_map(group_id=None):
        return {}

    @staticmethod
    def _check_lexicon(_text):
        return {}


class _HandleHarness(_StreamHarness):
    auto_moderate_enabled = True

    def __init__(self):
        super().__init__("never-present")
        self.rule_penalties = 0
        self.rule_inputs = []
        self.llm_calls = 0
        self.llm_inputs = []

    @staticmethod
    def _get_group_id(_event):
        return "1"

    @staticmethod
    def _try_get_sender_id(_event):
        return "2"

    @staticmethod
    def _pre_check_message(_event, _group_id, _user_id):
        return False

    async def _anti_flood_guard(self, _event, _group_id):
        return False, None

    async def _is_admin(self, _event):
        return False

    async def _handle_user_blacklist(self, *_args):
        return False, None

    @staticmethod
    def _get_group_override(_group_id, _key):
        return None

    async def _handle_qq_favorite(self, *_args):
        return False, None

    async def _execute_rule_penalty(self, *_args, **_kwargs):
        self.rule_penalties += 1
        self.rule_inputs.append((_args[4], dict(_args[5])))
        if False:
            yield None

    async def _call_llm_for_moderation(self, _event, text, hit_types, **_kwargs):
        self.llm_calls += 1
        self.llm_inputs.append((text, dict(hit_types)))
        return {"violation": False, "fallback": False}


class _ObfuscatedUrlHandleHarness(_HandleHarness):
    def __init__(self, *, scan_ad=True, llm_enabled=False, is_admin=False,
                 admin_exempt=False):
        super().__init__()
        self.scan_ad = scan_ad
        self.llm_enabled = llm_enabled
        self.is_admin = is_admin
        self.admin_exempt = admin_exempt

    def _cfg(self, name, default=None, group_id=""):
        values = {
            "scan_ad": self.scan_ad,
            "llm_moderation_enabled": self.llm_enabled,
            "moderation_admin_exempt": self.admin_exempt,
        }
        return values.get(name, default)

    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default

    async def _is_admin(self, _event):
        return self.is_admin


class _ContextHandleHarness(_HandleHarness):
    def __init__(self, *, full_scan=False, llm_enabled=False):
        super().__init__()
        self.full_scan = full_scan
        self.llm_enabled = llm_enabled
        self.logged = []

    def _cfg(self, name, default=None, group_id=""):
        values = {
            "combine_detect_enabled": True,
            "llm_moderation_enabled": self.llm_enabled,
            "llm_moderation_always": self.full_scan,
        }
        return values.get(name, default)

    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default

    def _log_moderation(self, *args):
        self.logged.append(args)


class _ScreenshotHandleHarness(_ContextHandleHarness):
    def _cfg(self, name, default=None, group_id=""):
        if name == "ocr_enabled":
            return True
        return super()._cfg(name, default, group_id)

    async def _apply_ocr(self, text, image_urls, _event, _group_id):
        self.seen_image_urls = list(image_urls)
        ocr_text = "[OCR识图内容]\n日抛plus /xxxxxx"
        return f"{text}\n{ocr_text}".strip()


class _BaseDisabledHandleHarness(_ContextHandleHarness):
    def _cfg(self, name, default=None, group_id=""):
        if name == "base_decode_enabled":
            return False
        return super()._cfg(name, default, group_id)


class _OrderedImageHandleHarness(_ContextHandleHarness):
    def __init__(self):
        super().__init__(full_scan=True, llm_enabled=True)
        self.ocr_started = asyncio.Event()
        self.ocr_release = asyncio.Event()
        self.ocr_calls = 0

    async def _apply_ocr(self, text, image_urls, _event, _group_id):
        if not image_urls:
            return text
        self.ocr_calls += 1
        self.ocr_started.set()
        await self.ocr_release.wait()
        return "[OCR识图内容]\n日抛plus"


class _ContextLLMHarness(_LLMHarness):
    @staticmethod
    def _cfg_int(name, default=0, group_id=""):
        return default

    @staticmethod
    def _try_get_sender_id(_event):
        return "2"

    async def _get_client(self, _event):
        return None


class _FirstOnlyAutomaton:
    def first_match(self, text):
        return (1, "hit") if "hit" in text else None

    def iter_matches(self, text):
        raise AssertionError("screening must not materialize every match")


class NestedForwardTests(unittest.TestCase):
    def test_three_level_forward_and_deep_card_are_scanned(self):
        deep_card = json.dumps({"outer": {"meta": {"hidden": "deep-slur"}}})
        responses = {
            "root": {"messages": [{
                "sender": {"nickname": "root"},
                "message": [
                    {"type": "text", "data": {"text": "outer"}},
                    {"type": "forward", "data": {"id": "middle"}},
                ],
            }]},
            "middle": {"messages": [{
                "sender": {"nickname": "middle"},
                "message": [{"type": "node", "data": {"content": [
                    {"type": "app", "data": {"content": deep_card}},
                    {"type": "forward", "data": {"id": "leaf"}},
                ]}}],
            }]},
            "leaf": {"messages": [{
                "sender": {"nickname": "leaf"},
                "message": [{"type": "text", "data": {"text": "leaf-slur"}}],
            }]},
        }
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "root"}}], client)

        text, favorite = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("deep-slur", text)
        self.assertIn("leaf-slur", text)
        self.assertFalse(favorite)
        self.assertEqual(
            [item[1] for item in client.calls], ["root", "middle", "leaf"]
        )

    def test_remote_forward_images_are_returned_for_ocr(self):
        client = _Client({
            "root": {"messages": [{
                "message": [
                    {"type": "text", "data": {"text": "caption"}},
                    {
                        "type": "image",
                        "data": {"url": "https://example.com/forward.png"},
                    },
                ],
            }]},
        })
        event = _Event(
            [{"type": "forward", "data": {"id": "root"}}], client
        )

        text, favorite, images = asyncio.run(
            _Harness()._resolve_forward_messages(event, return_images=True)
        )

        self.assertIn("caption", text)
        self.assertIn("[图片]", text)
        self.assertFalse(favorite)
        self.assertEqual(images, ["https://example.com/forward.png"])

    def test_reply_content_is_still_ignored_inside_nodes(self):
        node = Node([Reply("quoted-slur"), Plain("actual text")], data={"uin": 123})
        event = _Event([node])
        harness = _Harness()

        text, images, has_forward = harness._parse_message_chain(event)

        self.assertEqual(text, "actual text")
        self.assertEqual(images, [])
        self.assertFalse(has_forward)

    def test_dict_cards_and_cyclic_forward_are_not_skipped(self):
        card = {"type": "json", "data": {"data": json.dumps({"x": {"y": "nested-card"}})}}
        event = _Event([card])
        harness = _Harness()
        self.assertTrue(harness._should_scan_message(event))
        self.assertIn("nested-card", harness._parse_message_chain(event)[0])

        responses = {"loop": {"messages": [{
            "message": [{"type": "forward", "data": {"id": "loop"}}]
        }]}}
        client = _Client(responses)
        loop_event = _Event([{"type": "forward", "data": {"id": "loop"}}], client)
        text, _ = asyncio.run(harness._resolve_forward_messages(loop_event))
        self.assertTrue(text)
        self.assertEqual(len(client.calls), 1)

    def test_node_id_reference_is_resolved(self):
        responses = {"node-ref": {"messages": [{
            "message": [{"type": "text", "data": {"text": "node-ref-text"}}]
        }]}}
        client = _Client(responses)
        event = _Event([{"type": "node", "data": {"id": "node-ref"}}], client)
        harness = _Harness()

        text, _, has_forward = harness._parse_message_chain(event)
        resolved, _ = asyncio.run(harness._resolve_forward_messages(event))

        self.assertEqual(text, "")
        self.assertTrue(has_forward)
        self.assertIn("node-ref-text", resolved)
        self.assertEqual(len(client.calls), 1)

    def test_empty_inline_node_content_does_not_hide_id_reference(self):
        responses = {"empty-node-ref": {"messages": [{
            "message": [{"type": "text", "data": {"text": "resolved-empty-node"}}]
        }]}}
        client = _Client(responses)
        event = _Event([{
            "type": "node",
            "data": {"id": "empty-node-ref", "content": []},
        }], client)
        harness = _Harness()

        self.assertTrue(harness._parse_message_chain(event)[2])
        resolved, _ = asyncio.run(harness._resolve_forward_messages(event))

        self.assertIn("resolved-empty-node", resolved)
        self.assertEqual(len(client.calls), 1)

    def test_forward_result_with_direct_node_segments_keeps_type_wrapper(self):
        responses = {
            "direct-node": {"nodes": [{"type": "node", "data": {"id": "direct-leaf"}}]},
            "direct-leaf": {"messages": [{
                "message": [{"type": "text", "data": {"text": "direct-node-text"}}]
            }]},
        }
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "direct-node"}}], client)

        resolved, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("direct-node-text", resolved)
        self.assertEqual([item[1] for item in client.calls], ["direct-node", "direct-leaf"])

    def test_astrbot_component_enum_value_is_normalized(self):
        event = _Event([AdapterNode([Plain("enum-node-text")])])

        text, _, has_forward = _Harness()._parse_message_chain(event)

        self.assertEqual(text, "enum-node-text")
        self.assertFalse(has_forward)

    def test_json_string_nested_inside_json_is_decoded(self):
        inner = json.dumps({"hidden": "\u50bb\u903c"}, ensure_ascii=True)
        outer = json.dumps({"payload": inner}, ensure_ascii=True)
        event = _Event([{"type": "json", "data": {"data": outer}}])

        text, _, _ = _Harness()._parse_message_chain(event)

        self.assertIn("\u50bb\u903c", text)

    def test_wide_card_container_stops_at_item_budget(self):
        budget = {"items": 0, "chars": 0}

        text = _Harness._flatten_payload_text(
            [{} for _ in range(_Harness._CARD_MAX_ITEMS * 4)],
            budget=budget,
        )

        self.assertEqual(text, "")
        self.assertEqual(budget["items"], _Harness._CARD_MAX_ITEMS)

    def test_recursive_forward_list_is_bounded(self):
        cyclic_content = []
        cyclic_content.append(cyclic_content)
        cyclic_content.append({"type": "text", "data": {"text": "after-cycle"}})
        responses = {"cyclic-inline": {"messages": [{"message": cyclic_content}]}}
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "cyclic-inline"}}], client)

        text, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("after-cycle", text)
        self.assertEqual(len(client.calls), 1)

    def test_deep_forward_lists_stop_at_depth_limit(self):
        content = {"type": "text", "data": {"text": "too-deep-text"}}
        for _ in range(_Harness._FORWARD_MAX_DEPTH + 10):
            content = [content]
        responses = {"deep-list": {"messages": [{"message": content}]}}
        event = _Event(
            [{"type": "forward", "data": {"id": "deep-list"}}],
            _Client(responses),
        )

        text, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u6df1\u5ea6\u4e0a\u9650", text)
        self.assertNotIn("too-deep-text", text)
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_lookup_has_per_request_timeout(self):
        client = _SlowClient()
        event = _Event([{"type": "forward", "data": {"id": "slow"}}], client)

        text, _, scan = asyncio.run(_FastTimeoutHarness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u83b7\u53d6\u5931\u8d25", text)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_lookups_share_a_total_timeout_budget(self):
        client = _SlowClient()
        event = _Event([
            {"type": "forward", "data": {"id": f"slow-{index}"}}
            for index in range(5)
        ], client)

        harness = _StreamHarness("never-present")
        harness._FORWARD_REQUEST_TIMEOUT = 1.0
        harness._FORWARD_TOTAL_TIMEOUT = 0.01
        _, _, scan = asyncio.run(harness._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_budget_expired_before_request_marks_scan_incomplete(self):
        client = _StaticClient({"data": {"messages": [{"message": "unused"}]}})
        event = _Event([{"type": "forward", "data": {"id": "expired"}}], client)
        harness = _Harness()
        harness._FORWARD_TOTAL_TIMEOUT = 0

        text, _, scan = asyncio.run(harness._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u603b\u8d85\u65f6\u4e0a\u9650", text)
        self.assertEqual(client.calls, [])
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_api_failure_envelope_is_not_treated_as_content(self):
        client = _StaticClient({
            "status": "failed", "retcode": 100,
            "data": {"messages": [{"message": "must-not-be-audited"}]},
        })
        event = _Event([{"type": "forward", "data": {"id": "failed"}}], client)

        text, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u83b7\u53d6\u5931\u8d25", text)
        self.assertNotIn("must-not-be-audited", text)
        self.assertTrue(scan["hits"]["oversized"])

    def test_empty_forward_response_marks_scan_incomplete(self):
        client = _StaticClient({"data": {"messages": []}})
        event = _Event([{"type": "forward", "data": {"id": "empty"}}], client)

        _, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_missing_forward_client_marks_scan_incomplete(self):
        event = _Event([{"type": "forward", "data": {"id": "no-client"}}])

        _, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_failed_forward_lookups_are_request_bounded(self):
        client = _Client({})
        event = _Event([
            {"type": "forward", "data": {"id": f"missing-{index}"}}
            for index in range(_Harness._FORWARD_MAX_REQUESTS + 20)
        ], client)

        asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertEqual(len(client.calls), _Harness._FORWARD_MAX_REQUESTS)

    def test_forward_sender_nickname_is_not_audited_as_message_text(self):
        responses = {"nickname": {"messages": [{
            "sender": {"nickname": "nickname-slur"},
            "message": [{"type": "text", "data": {"text": "benign body"}}],
        }]}}
        event = _Event([{"type": "forward", "data": {"id": "nickname"}}], _Client(responses))

        text, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("benign body", text)
        self.assertNotIn("nickname-slur", text)

    def test_anti_flood_formatter_reuses_recursive_extractor(self):
        nested = Node([Json(json.dumps({"deep": {"text": "formatter-card"}})), Plain("formatter-text")])
        formatted = _CombinedHarness()._format_message_content([nested])

        self.assertIn("formatter-card", formatted)
        self.assertIn("formatter-text", formatted)
        self.assertNotIn("[Node]", formatted)

    def test_forward_scan_switch_excludes_inline_nodes_everywhere(self):
        nested = Node([
            Plain("nested-slur"),
            {"type": "image", "data": {"url": "https://example.com/nested.jpg"}},
        ])
        event = _Event([Plain("outer-text"), nested])
        harness = _Harness()

        enabled_text, enabled_images, _ = harness._parse_message_chain(event)
        disabled_text, disabled_images, has_forward = harness._parse_message_chain(
            event, include_forward_content=False)
        disabled_formatted = _CombinedHarness()._format_message_content(
            [Plain("outer-text"), nested], include_forward_content=False)

        self.assertIn("nested-slur", enabled_text)
        self.assertTrue(enabled_images)
        self.assertEqual(disabled_text, "outer-text")
        self.assertEqual(disabled_images, [])
        self.assertTrue(has_forward)
        self.assertNotIn("nested-slur", disabled_formatted)

    def test_qq_favorite_marker_inside_node_is_found(self):
        card = json.dumps({"meta": {"detail": {"url": "https://sharechain.qq.com/path"}}})
        event = _Event([Node([Json(card)])])

        found = asyncio.run(_Harness()._check_qq_favorite_non_forward(event))

        self.assertTrue(found)

    def test_stream_scan_sees_node_after_stored_text_limit(self):
        harness = _StreamHarness("deep-slur")
        event = _Event([
            Plain("a" * harness._FORWARD_MAX_CHARS),
            Node([Plain("deep-slur")]),
        ])

        text, _, _, scan = harness._parse_message_chain(
            event, group_id="1", return_scan=True
        )
        evidenced = harness._append_stream_rule_evidence(text, [scan])

        self.assertNotIn("deep-slur", text)
        self.assertTrue(scan["hits"]["swear"])
        self.assertIn("deep-slur", evidenced)

    def test_stream_scan_matches_across_recursive_leaves(self):
        harness = _StreamHarness("split-slur")
        event = _Event([Node([Plain("split-"), Node([Plain("slur")])])])

        _, _, _, scan = harness._parse_message_chain(
            event, group_id="1", return_scan=True
        )

        self.assertTrue(scan["hits"]["swear"])

    def test_stream_scan_keeps_unbounded_separator_across_recursive_leaves(self):
        harness = _StreamHarness("unused")
        harness._swear_matcher = _SeparatedAssholeMatcher()
        event = _Event([
            Plain("x" * harness._FORWARD_MAX_CHARS),
            Node([Plain("a" + (" " * 200)), Node([Plain("sshole")])]),
        ])

        text, _, _, scan = harness._parse_message_chain(
            event, group_id="1", return_scan=True
        )

        self.assertNotIn("sshole", text)
        self.assertTrue(scan["hits"]["swear"])

    def test_stream_scan_defers_ascii_right_boundary_until_next_leaf(self):
        harness = _StreamHarness("unused")
        harness._swear_matcher = _CsBoundaryMatcher()

        _, _, _, joined_scan = harness._parse_message_chain(
            _Event([Plain("cs"), Plain("go")]),
            group_id="1",
            return_scan=True,
        )
        _, _, _, standalone_scan = harness._parse_message_chain(
            _Event([Plain("cs")]),
            group_id="1",
            return_scan=True,
        )

        self.assertFalse(joined_scan["hits"].get("swear", False))
        self.assertTrue(standalone_scan["hits"]["swear"])

    def test_stream_scan_continues_through_truncated_json_card(self):
        harness = _StreamHarness("card-slur")
        card = json.dumps({
            "padding": "a" * harness._CARD_MAX_CHARS,
            "hidden": "card-slur",
        })
        event = _Event([Json(card)])

        text, _, _, scan = harness._parse_message_chain(
            event, group_id="1", return_scan=True
        )

        self.assertNotIn("card-slur", text)
        self.assertTrue(scan["hits"]["swear"])

    def test_stream_scan_preserves_card_field_boundaries_after_truncation(self):
        harness = _StreamHarness("unused")
        harness._swear_matcher = _CsBoundaryMatcher()
        card = json.dumps({
            "padding": "a" * harness._CARD_MAX_CHARS,
            "token": "cs",
            "suffix": "go",
        })

        text, _, _, scan = harness._parse_message_chain(
            _Event([Json(card)]), group_id="1", return_scan=True
        )

        self.assertNotIn("cs", text)
        self.assertTrue(scan["hits"]["swear"])

    def test_forward_stream_evidence_survives_front_padding(self):
        harness = _StreamHarness("forward-slur")
        responses = {"padded": {"messages": [{"message": [
            {"type": "text", "data": {"text": "a" * harness._FORWARD_MAX_CHARS}},
            {"type": "text", "data": {"text": "forward-slur"}},
        ]}]}}
        event = _Event(
            [{"type": "forward", "data": {"id": "padded"}}],
            _Client(responses),
        )

        text, _, scan = asyncio.run(harness._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))
        evidenced = harness._append_stream_rule_evidence(text, [scan])

        self.assertNotIn("forward-slur", text)
        self.assertTrue(scan["hits"]["swear"])
        self.assertIn("forward-slur", evidenced)

    def test_stream_scan_marks_content_over_full_audit_limit(self):
        harness = _StreamHarness("never-present")
        event = _Event([Plain("a" * (moderation.STREAM_RULE_SCAN_MAX_CHARS + 1))])

        _, _, _, scan = harness._parse_message_chain(
            event, group_id="1", return_scan=True
        )

        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_stream_scan_marks_unvisited_nodes_after_structure_limit(self):
        harness = _StreamHarness("late-slur")
        chain = [Plain("") for _ in range(harness._INLINE_MAX_NODES)]
        chain.append(Plain("late-slur"))

        text, _, _, scan = harness._parse_message_chain(
            _Event(chain), group_id="1", return_scan=True
        )

        self.assertNotIn("late-slur", text)
        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_oversized_message_uses_local_penalty_without_llm(self):
        harness = _HandleHarness()
        event = _Event([
            Plain("a" * (moderation.STREAM_RULE_SCAN_MAX_CHARS + 1))
        ])

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)

    def test_failed_forward_lookup_uses_local_penalty_without_llm(self):
        harness = _HandleHarness()
        event = _Event(
            [{"type": "forward", "data": {"id": "failed"}}],
            _StaticClient({"status": "failed", "retcode": 100}),
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)

    def test_negative_squared_url_uses_one_local_penalty_without_llm(self):
        harness = _ObfuscatedUrlHandleHarness()
        url = "https://catfk.com/shop/bugbugteam"
        event = _Event(
            [Plain(_negative_squared_url(url))],
            message_id="boxed-url", message_seq=901, timestamp=901,
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)
        audit_text, hit_types = harness.rule_inputs[0]
        self.assertTrue(hit_types["obfuscated_url"])
        self.assertIn("[异形字符归一化链接]", audit_text)
        self.assertIn(url.upper(), audit_text)

    def test_disguised_url_respects_disabled_ad_scan(self):
        harness = _ObfuscatedUrlHandleHarness(scan_ad=False)
        event = _Event(
            [Plain(_negative_squared_url("https://catfk.com/shop/bugbugteam"))],
            message_id="boxed-url-disabled", message_seq=902, timestamp=902,
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 0)
        self.assertEqual(harness.llm_calls, 0)

    def test_admin_content_is_scanned_unless_exemption_is_enabled(self):
        async def consume(harness, event):
            return [item async for item in harness._handle_message(event)]

        scanned = _ObfuscatedUrlHandleHarness(is_admin=True, admin_exempt=False)
        asyncio.run(consume(scanned, _Event(
            [Plain(_negative_squared_url("https://catfk.com/shop/bugbugteam"))],
            message_id="admin-scanned", message_seq=903, timestamp=903,
        )))
        self.assertEqual(scanned.rule_penalties, 1)

        exempt = _ObfuscatedUrlHandleHarness(is_admin=True, admin_exempt=True)
        asyncio.run(consume(exempt, _Event(
            [Plain(_negative_squared_url("https://catfk.com/shop/bugbugteam"))],
            message_id="admin-exempt", message_seq=904, timestamp=904,
        )))
        self.assertEqual(exempt.rule_penalties, 0)
        self.assertEqual(exempt.llm_calls, 0)

    def test_duplicate_boxed_url_delivery_has_one_penalty(self):
        harness = _ObfuscatedUrlHandleHarness()
        payload = [Plain(_negative_squared_url("https://catfk.com/shop/bugbugteam"))]

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        first = _Event(
            payload, message_id="duplicate-boxed-url", message_seq=905,
            timestamp=905,
        )
        duplicate = _Event(
            payload, message_id="duplicate-boxed-url", message_seq=905,
            timestamp=905,
        )
        asyncio.run(consume(first))
        asyncio.run(consume(duplicate))

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)
        self.assertEqual(duplicate.stop_calls, 1)

    def test_anti_flood_duplicate_stops_event_propagation(self):
        class GuardHarness:
            @staticmethod
            def _try_get_sender_id(_event):
                return "2"

            @staticmethod
            def _cfg(name, default=True, group_id=""):
                return True if name == "anti_flood_enabled" else default

            @staticmethod
            async def _is_admin(_event):
                return False

            @staticmethod
            def _anti_flood_event_message_identity(event):
                return anti_flood.AntiFloodMixin._anti_flood_event_message_identity(event)

            @staticmethod
            def _anti_flood_event_is_duplicate(_group_id, _user_id, _msg_id):
                return True

        event = _Event([], message_id="retransmitted")
        blocked, notice = asyncio.run(
            moderation.ModerationMixin._anti_flood_guard(GuardHarness(), event, "1")
        )

        self.assertTrue(blocked)
        self.assertIsNone(notice)
        self.assertEqual(event.stop_calls, 1)

    def test_recall_only_flood_cooldown_is_not_the_recall_message_threshold(self):
        class GuardHarness(anti_flood.AntiFloodMixin):
            def __init__(self):
                self.cooldowns = []
                self._init_anti_flood()

            @staticmethod
            def _try_get_sender_id(_event):
                return "2"

            @staticmethod
            async def _is_admin(_event):
                return False

            @staticmethod
            def _moderation_in_penalty_cooldown(_group_id, _user_id):
                return False

            @staticmethod
            def _format_message_content(_raw_message, include_forward_content=True):
                return "fixture"

            @staticmethod
            def _check_anti_flood(_group_id, _user_id):
                return True, {
                    "rate": "每秒", "count": 2, "limit": 1,
                    "total_msgs": 2, "msg_ids": [],
                }

            def _cfg(self, name, default=True, group_id=""):
                values = {
                    "anti_flood_enabled": True,
                    "anti_flood_recall_enabled": False,
                    "appeal_enabled": False,
                }
                return values.get(name, default)

            def _cfg_int(self, name, default=0, group_id=""):
                values = {
                    "anti_flood_mute_duration": 0,
                    "anti_flood_recall_threshold": 999999,
                }
                return values.get(name, default)

            def _mark_anti_flood_penalty(self, group_id, user_id, cooldown_seconds):
                self.cooldowns.append((group_id, user_id, cooldown_seconds))

            @staticmethod
            def _log_moderation(*_args):
                return None

        harness = GuardHarness()
        event = _Event([], message_id="recall-only")
        event.message_obj.message = []
        blocked, _notice = asyncio.run(
            moderation.ModerationMixin._anti_flood_guard(harness, event, "1")
        )

        self.assertTrue(blocked)
        self.assertEqual([("1", "2", 30)], harness.cooldowns)

    def test_llm_recall_only_does_not_suppress_later_messages_with_ban_cooldown(self):
        class RecallOnlyHarness(moderation.ModerationMixin):
            def __init__(self):
                self.cooldowns = []

            @staticmethod
            def _moderation_in_penalty_cooldown(_group_id, _user_id):
                return False

            @staticmethod
            def _anti_flood_in_cooldown(_group_id, _user_id):
                return False

            @staticmethod
            async def _recall_msg(*_args):
                return None

            @staticmethod
            async def _recall_extra_messages(*_args):
                return None

            @staticmethod
            def _log_moderation(*_args):
                return None

            @staticmethod
            def _cfg_int(name, default=0, group_id=""):
                return 1800 if name == "moderation_ban_duration" else default

            @staticmethod
            def _cfg(name, default=True, group_id=""):
                if name in {"llm_moderation_ban", "auto_moderate_notice"}:
                    return False
                return default

            def _mark_moderation_penalty(self, group_id, user_id, cooldown_seconds):
                self.cooldowns.append((group_id, user_id, cooldown_seconds))

        harness = RecallOnlyHarness()
        event = _Event([], message_id="llm-recall-only")

        async def consume():
            return [item async for item in harness._execute_llm_penalty(
                event, "1", "2", "tester", "fixture", "violation", "ad", [], []
            )]

        self.assertEqual([], asyncio.run(consume()))
        self.assertEqual([], harness.cooldowns)

    def test_anti_flood_entry_counts_message_without_an_id(self):
        class EntryHarness(anti_flood.AntiFloodMixin):
            def __init__(self):
                self.values = {
                    "anti_flood_enabled": True,
                    "anti_flood_rate_per_second": 2,
                    "anti_flood_rate_per_minute": 0,
                    "anti_flood_rate_per_hour": 0,
                    "repeat_detect_enabled": False,
                    "long_text_detect_enabled": False,
                    "anti_flood_mute_duration": 0,
                    "anti_flood_recall_enabled": False,
                    "appeal_enabled": False,
                }
                self._init_anti_flood()
                self.recorded = []

            @staticmethod
            def _try_get_sender_id(_event):
                return "2"

            @staticmethod
            async def _is_admin(_event):
                return False

            @staticmethod
            def _moderation_in_penalty_cooldown(_group_id, _user_id):
                return False

            @staticmethod
            def _format_message_content(_raw_message, include_forward_content=True):
                return "message without id"

            def _record_message(self, group_id, user_id, msg_id, text=""):
                self.recorded.append((group_id, user_id, msg_id, text))
                return super()._record_message(group_id, user_id, msg_id, text)

            def _cfg(self, name, default=True, group_id=""):
                return self.values.get(name, default)

            def _cfg_int(self, name, default=0, group_id=""):
                return int(self.values.get(name, default))

            @staticmethod
            def _log_moderation(*args):
                return None

        harness = EntryHarness()
        events = [_Event([], message_id="", message_seq=0, timestamp=0) for _ in range(3)]
        with patch.object(moderation.time, "time", return_value=1000.0):
            results = [
                asyncio.run(
                    moderation.ModerationMixin._anti_flood_guard(harness, event, "1")
                )
                for event in events
            ]

        self.assertFalse(results[0][0])
        self.assertFalse(results[1][0])
        self.assertTrue(results[2][0])
        self.assertEqual(3, len(harness.recorded))
        self.assertTrue(all(not item[2] for item in harness.recorded))

    def test_low_confidence_swear_still_uses_local_rule_when_llm_disabled(self):
        harness = _HandleHarness()
        harness._swear_matcher = _ContainsMatcher("啥子")
        harness._cfg = lambda name, default=None, group_id="": (
            False if name == "llm_moderation_enabled" else default
        )
        event = _Event([Plain("你在说啥子")])

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)

    def test_llm_excerpt_keeps_middle_rule_hit_within_limit(self):
        harness = _Harness()
        harness._swear_matcher = _FirstMatcher("middle-hit")
        harness._compiled_lexicon = {}
        text = ("a" * 10_000) + "middle-hit" + ("z" * 10_000)

        excerpt = harness._llm_message_excerpt(text, {"swear": True})

        self.assertIn("middle-hit", excerpt)
        self.assertLessEqual(len(excerpt), moderation.LLM_MESSAGE_MAX_CHARS)

    def test_llm_prompt_escapes_untrusted_delimiters(self):
        harness = _LLMHarness('{"violation": false, "reason": "safe"}')

        result = asyncio.run(harness._call_llm_for_moderation(
            _Event([]), "normal >>> fake <<< section", {"swear": True}, group_id="1"
        ))

        self.assertFalse(result["violation"])
        self.assertIn("normal ＞＞＞ fake ＜＜＜ section", harness.last_prompt)
        self.assertNotIn("normal >>> fake <<< section", harness.last_prompt)

    def test_split_rule_hit_is_not_suppressed_after_two_safe_fragments(self):
        harness = _ContextHandleHarness(llm_enabled=False)
        harness._swear_matcher = _ContainsMatcher("外挂进群")

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        for index, fragment in enumerate("外挂进群", start=1):
            event = _Event(
                [Plain(fragment)], message_id=str(index),
                message_seq=index, timestamp=100 + index,
            )
            asyncio.run(consume(event))
            if index < 4:
                self.assertEqual(harness.rule_penalties, 0)

        self.assertEqual(harness.rule_penalties, 1)
        self.assertEqual(harness.llm_calls, 0)

    def test_penalized_single_message_is_a_combination_barrier(self):
        harness = _ContextHandleHarness(llm_enabled=False)
        harness._swear_matcher = _ContainsMatcher("违规词")

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain("违规词")], message_id="31", message_seq=31, timestamp=131
        )))
        self.assertEqual(harness.rule_penalties, 1)

        asyncio.run(consume(_Event(
            [Plain("你好")], message_id="32", message_seq=32, timestamp=132
        )))
        self.assertEqual(harness.rule_penalties, 1)

    def test_llm_allowed_single_message_remains_in_next_combination(self):
        harness = _ContextHandleHarness(llm_enabled=True)
        harness._swear_matcher = _ContainsMatcher("疑似误报")

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain("疑似误报")], message_id="33", message_seq=33, timestamp=133
        )))
        self.assertEqual(harness.llm_calls, 1)

        asyncio.run(consume(_Event(
            [Plain("正常后续")], message_id="34", message_seq=34, timestamp=134
        )))
        self.assertEqual(harness.llm_calls, 2)
        self.assertIn("疑似误报正常后续", harness.llm_inputs[-1][0])

    def test_later_text_waits_for_earlier_image_ocr_without_sequence(self):
        async def scenario():
            harness = _OrderedImageHandleHarness()

            async def consume(event):
                return [item async for item in harness._handle_message(event)]

            first = _Event(
                [{"type": "image", "data": {"url": "first.png"}}],
                message_id="image-1", timestamp=200,
            )
            second = _Event(
                [Plain("/xxxxxx")], message_id="text-2", timestamp=200,
            )
            first_task = asyncio.create_task(consume(first))
            await asyncio.wait_for(harness.ocr_started.wait(), timeout=1)
            second_task = asyncio.create_task(consume(second))
            await asyncio.sleep(0.02)
            self.assertFalse(second_task.done())

            harness.ocr_release.set()
            await asyncio.gather(first_task, second_task)

            self.assertEqual(harness.ocr_calls, 1)
            self.assertEqual(harness.llm_calls, 2)
            self.assertIn("日抛plus/xxxxxx", harness.llm_inputs[-1][0])

        asyncio.run(scenario())

    def test_duplicate_image_event_is_deduplicated_before_ocr(self):
        async def scenario():
            harness = _OrderedImageHandleHarness()

            async def consume(event):
                return [item async for item in harness._handle_message(event)]

            event = _Event(
                [{"type": "image", "data": {"url": "same.png"}}],
                message_id="same-image", timestamp=201,
            )
            first_task = asyncio.create_task(consume(event))
            await asyncio.wait_for(harness.ocr_started.wait(), timeout=1)
            duplicate_task = asyncio.create_task(consume(event))
            await asyncio.sleep(0.02)
            self.assertTrue(duplicate_task.done())

            harness.ocr_release.set()
            await asyncio.gather(first_task, duplicate_task)
            self.assertEqual(harness.ocr_calls, 1)

        asyncio.run(scenario())

    def test_missing_message_id_does_not_duplicate_one_event_in_context(self):
        harness = _ContextHandleHarness(llm_enabled=False)
        harness._swear_matcher = _ContainsMatcher("哈哈")
        event = _Event([Plain("哈")], message_id="", message_seq=0, timestamp=140)

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        queue = harness._moderation_context_data[("1", "2")]
        self.assertEqual(len(queue), 1)
        self.assertEqual(harness.rule_penalties, 0)

    def test_missing_message_id_keys_remain_unique_across_many_events(self):
        harness = _CombinedHarness()
        keys = []
        for _ in range(6000):
            event = _ReadOnlyContextEvent()
            key = harness._context_message_key(event)
            self.assertEqual(harness._context_message_key(event), key)
            keys.append(key)

        self.assertEqual(len(set(keys)), len(keys))
        self.assertLessEqual(
            len(harness._context_event_key_fallback),
            moderation_context.CONTEXT_EVENT_KEY_FALLBACK_MAX,
        )

    def test_missing_message_id_key_can_be_cached_in_raw_event(self):
        harness = _CombinedHarness()
        event = _RawCacheContextEvent()

        key = harness._context_message_key(event)

        self.assertEqual(harness._context_message_key(event), key)
        self.assertEqual(
            event.raw_event[moderation_context._CONTEXT_EVENT_KEY_ATTR], key
        )

    def test_nested_onebot_message_ids_share_a_canonical_context_key(self):
        harness = _CombinedHarness()

        def event_with(raw_event):
            event = _RawCacheContextEvent()
            event.raw_event = raw_event
            return event

        events = [
            event_with({"message_id": "adapter-42"}),
            event_with({"msg_id": "adapter-42"}),
            event_with({
                "data": {"event": {"raw_message": {"msg_id": "adapter-42"}}},
            }),
        ]

        self.assertEqual(
            {harness._context_message_key(event) for event in events},
            {"message:adapter-42"},
        )

    def test_nested_message_sequence_is_used_only_when_message_id_is_empty(self):
        harness = _CombinedHarness()

        def event_with(raw_event):
            event = _RawCacheContextEvent()
            event.raw_event = raw_event
            return event

        first = event_with({
            "data": {"event": {"raw_message": {
                "message_seq": 314, "time": 1234,
            }}},
        })
        second = event_with({"seq": "314", "timestamp": "1234"})
        empty = event_with({"message_id": 0, "msg_id": None, "message_seq": 0})

        self.assertEqual("sequence:314", harness._context_message_key(first))
        self.assertEqual(
            harness._context_message_key(first),
            harness._context_message_key(second),
        )
        self.assertEqual((314, 1234), harness._event_message_order(first))
        self.assertTrue(harness._context_message_key(empty).startswith("event:"))

    def test_combined_deduplication_is_per_candidate_signature(self):
        harness = _ContextHandleHarness(llm_enabled=False)
        first = _Event([Plain("外")], message_id="1", message_seq=1, timestamp=101)
        second = _Event([Plain("挂")], message_id="2", message_seq=2, timestamp=102)
        third = _Event([Plain("进")], message_id="3", message_seq=3, timestamp=103)

        self.assertEqual(
            harness._collect_combined_text(first, "1", "2", "外"),
            ("", [], ""),
        )
        combined, ids, signature = harness._collect_combined_text(
            second, "1", "2", "挂"
        )
        self.assertTrue(combined)
        self.assertEqual(ids, ["1"])
        harness._mark_combined_handled("1", "2", signature)
        self.assertTrue(harness._combined_in_cooldown("1", "2", signature))

        newer, newer_ids, newer_signature = harness._collect_combined_text(
            third, "1", "2", "进"
        )
        self.assertTrue(newer)
        self.assertEqual(newer_ids, ["1", "2"])
        self.assertNotEqual(newer_signature, signature)
        self.assertFalse(
            harness._combined_in_cooldown("1", "2", newer_signature)
        )

    def test_combined_signature_store_has_a_hard_capacity(self):
        harness = _CombinedHarness()
        limit = moderation_context.COMBINED_HANDLED_MAX_ENTRIES

        for index in range(limit + 20):
            harness._mark_combined_handled(
                "1", "2", f"signature-{index}", seconds=3600
            )

        self.assertEqual(len(harness._combined_handled), limit)
        self.assertFalse(
            harness._combined_in_cooldown("1", "2", "signature-0")
        )
        self.assertTrue(harness._combined_in_cooldown(
            "1", "2", f"signature-{limit + 19}"
        ))

    def test_pending_candidate_does_not_block_new_fragment_signature(self):
        harness = _ContextHandleHarness(llm_enabled=True)
        first = _Event([Plain("啥")], message_id="51", message_seq=51, timestamp=151)
        second = _Event([Plain("子外")], message_id="52", message_seq=52, timestamp=152)
        third = _Event([Plain("挂")], message_id="53", message_seq=53, timestamp=153)

        harness._collect_combined_text(first, "1", "2", "啥")
        combined, ids, signature = harness._collect_combined_text(
            second, "1", "2", "子外"
        )
        self.assertIn("啥子外", combined)
        harness._set_moderation_combine_state(
            second, "1", "2", ids, "pending"
        )

        newer, _, newer_signature = harness._collect_combined_text(
            third, "1", "2", "挂"
        )
        self.assertIn("外挂", newer)
        self.assertNotEqual(newer_signature, signature)

    def test_full_scan_calls_llm_without_local_rule_hit(self):
        harness = _ContextHandleHarness(full_scan=True, llm_enabled=True)
        event = _Event(
            [Plain("普通聊天内容")], message_id="10",
            message_seq=10, timestamp=110,
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.rule_penalties, 0)
        self.assertEqual(harness.llm_calls, 1)
        self.assertEqual(len(harness.logged), 1)

    def test_normal_ai_mode_reviews_split_promotion_without_local_hit(self):
        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain("日抛plus")], message_id="71",
            message_seq=71, timestamp=171,
        )))
        self.assertEqual(harness.llm_calls, 0)

        asyncio.run(consume(_Event(
            [Plain("/xxxxxx")], message_id="72",
            message_seq=72, timestamp=172,
        )))

        self.assertEqual(harness.llm_calls, 1)
        audit_text, hit_types = harness.llm_inputs[0]
        self.assertIn("日抛plus/xxxxxx", audit_text)
        self.assertTrue(hit_types["context_scan"])

    def test_normal_ai_mode_skips_two_ordinary_long_messages(self):
        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain("今天下午三点开会讨论项目进度")], message_id="81",
            message_seq=81, timestamp=181,
        )))
        asyncio.run(consume(_Event(
            [Plain("收到，我会提前准备相关材料")], message_id="82",
            message_seq=82, timestamp=182,
        )))

        self.assertEqual(harness.llm_calls, 0)

    def test_normal_ai_mode_skips_common_short_acknowledgements(self):
        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        for index, text in enumerate(("好的", "收到", "谢谢"), start=85):
            asyncio.run(consume(_Event(
                [Plain(text)], message_id=str(index),
                message_seq=index, timestamp=200 + index,
            )))

        self.assertEqual(harness.llm_calls, 0)

    def test_normal_ai_mode_reviews_one_character_fragments_without_local_hit(self):
        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain("x")], message_id="83", message_seq=83, timestamp=183,
        )))
        self.assertEqual(harness.llm_calls, 0)

        asyncio.run(consume(_Event(
            [Plain("y")], message_id="84", message_seq=84, timestamp=184,
        )))

        self.assertEqual(harness.llm_calls, 1)
        audit_text, hit_types = harness.llm_inputs[0]
        self.assertIn("xy", audit_text)
        self.assertTrue(hit_types["context_scan"])

    def test_normal_ai_mode_sends_ocr_screenshot_to_llm_without_local_hit(self):
        harness = _ScreenshotHandleHarness(full_scan=False, llm_enabled=True)
        event = _Event([{
            "type": "image",
            "data": {"url": "https://example.com/promo.png"},
        }], message_id="73", message_seq=73, timestamp=173)

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(harness.llm_calls, 1)
        self.assertEqual(
            harness.seen_image_urls, ["https://example.com/promo.png"]
        )
        audit_text, hit_types = harness.llm_inputs[0]
        self.assertIn("日抛plus /xxxxxx", audit_text)
        self.assertTrue(hit_types["image_scan"])

    def test_normal_ai_mode_reviews_base64_decoded_content(self):
        import base64

        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)
        encoded = base64.b64encode(
            "日抛plus /xxxxxx 加我微信".encode("utf-8")
        ).decode("ascii")
        event = _Event(
            [Plain(encoded)], message_id="74", message_seq=74, timestamp=174
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(1, harness.llm_calls)
        audit_text, hit_types = harness.llm_inputs[0]
        self.assertIn("日抛plus /xxxxxx 加我微信", audit_text)
        self.assertTrue(hit_types["encoded_scan"])

    def test_disabled_base_decode_does_not_trigger_semantic_review(self):
        import base64

        harness = _BaseDisabledHandleHarness(full_scan=False, llm_enabled=True)
        encoded = base64.b64encode(
            "日抛plus /xxxxxx 加我微信".encode("utf-8")
        ).decode("ascii")
        event = _Event(
            [Plain(encoded)], message_id="741", message_seq=741, timestamp=1741
        )

        async def consume():
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume())

        self.assertEqual(0, harness.llm_calls)

    def test_split_base64_is_reassembled_then_decoded(self):
        import base64

        harness = _ContextHandleHarness(full_scan=False, llm_enabled=True)
        encoded = base64.b64encode(
            "日抛plus /xxxxxx 加我微信".encode("utf-8")
        ).decode("ascii")
        # 选在一个两侧单独都不能形成可信解码证据的位置，确保本用例真正验证
        # “跨消息重组后才命中”，而不是首段已可读导致的两次独立审核。
        split_at = 21
        self.assertFalse(moderation.decode_base_evidence(encoded[:split_at]))
        self.assertFalse(moderation.decode_base_evidence(encoded[split_at:]))

        async def consume(event):
            return [item async for item in harness._handle_message(event)]

        asyncio.run(consume(_Event(
            [Plain(encoded[:split_at])],
            message_id="75", message_seq=75, timestamp=175,
        )))
        asyncio.run(consume(_Event(
            [Plain(encoded[split_at:])],
            message_id="76", message_seq=76, timestamp=176,
        )))

        self.assertEqual(1, harness.llm_calls)
        audit_text, hit_types = harness.llm_inputs[0]
        self.assertIn("日抛plus /xxxxxx 加我微信", audit_text)
        self.assertTrue(hit_types["encoded_scan"])

    def test_llm_prompt_keeps_local_sender_fragments_when_history_is_unavailable(self):
        harness = _ContextLLMHarness(
            '{"violation": false, "reason": "context checked"}'
        )
        first = _Event([Plain("外")], message_id="21", message_seq=21, timestamp=121)
        second = _Event([Plain("挂")], message_id="22", message_seq=22, timestamp=122)
        current = _Event([Plain("进群")], message_id="23", message_seq=23, timestamp=123)
        future = _Event([Plain("未来消息")], message_id="24", message_seq=24, timestamp=124)
        harness._record_moderation_context(first, "1", "2", "tester", "外")
        harness._record_moderation_context(second, "1", "2", "tester", "挂")
        harness._record_moderation_context(current, "1", "2", "tester", "进群")
        harness._record_moderation_context(future, "1", "2", "tester", "未来消息")

        result = asyncio.run(harness._call_llm_for_moderation(
            current, "进群", {"full_scan": True}, group_id="1"
        ))

        self.assertFalse(result["violation"])
        self.assertIn("【同一发送者近期分段（旧到新）】", harness.last_prompt)
        self.assertIn("先发“日抛plus”", harness.last_prompt)
        self.assertIn("[消息 21] 外", harness.last_prompt)
        self.assertIn("[消息 22] 挂", harness.last_prompt)
        self.assertLess(
            harness.last_prompt.index("[消息 21] 外"),
            harness.last_prompt.index("[消息 22] 挂"),
        )
        self.assertNotIn("[消息 23] 进群", harness.last_prompt)
        self.assertNotIn("未来消息", harness.last_prompt)

    def test_local_arrival_cutoff_excludes_future_fragment_without_sequence(self):
        harness = _ContextLLMHarness(
            '{"violation": false, "reason": "context checked"}'
        )
        previous = _Event([Plain("前文")], message_id="41", timestamp=200)
        current = _Event([Plain("当前")], message_id="42", timestamp=200)
        future = _Event([Plain("后文")], message_id="43", timestamp=200)
        harness._record_moderation_context(previous, "1", "2", "tester", "前文")
        harness._record_moderation_context(current, "1", "2", "tester", "当前")
        harness._record_moderation_context(future, "1", "2", "tester", "后文")

        asyncio.run(harness._call_llm_for_moderation(
            current, "当前", {"full_scan": True}, group_id="1"
        ))

        self.assertIn("[消息 41] 前文", harness.last_prompt)
        self.assertNotIn("[消息 43] 后文", harness.last_prompt)

    def test_local_sequence_order_wins_over_late_registration(self):
        harness = _ContextLLMHarness(
            '{"violation": false, "reason": "context checked"}'
        )
        current = _Event(
            [Plain("当前")], message_id="62", message_seq=62, timestamp=262
        )
        delayed_old = _Event(
            [Plain("延迟旧消息")], message_id="60", message_seq=60,
            timestamp=999,
        )
        newer_old = _Event(
            [Plain("较新旧消息")], message_id="61", message_seq=61,
            timestamp=100,
        )
        harness._record_moderation_context(
            current, "1", "2", "tester", "当前"
        )
        harness._record_moderation_context(
            delayed_old, "1", "2", "tester", "延迟旧消息"
        )
        harness._record_moderation_context(
            newer_old, "1", "2", "tester", "较新旧消息"
        )

        entries = harness._recent_sender_entries(
            "1", "2",
            current_context_key=harness._context_message_key(current),
            before_seq=62,
            before_time=262,
            before_arrival=1,
        )

        self.assertEqual(
            [entry["message_id"] for entry in entries], ["60", "61"]
        )

    def test_local_context_user_store_has_a_hard_capacity(self):
        harness = _CombinedHarness()
        limit = moderation_context.LOCAL_CONTEXT_MAX_USERS
        for index in range(limit + 20):
            harness._record_moderation_context(
                _Event([Plain("x")], message_id=str(index)),
                "1", str(index), "tester", "x",
            )

        self.assertEqual(len(harness._moderation_context_data), limit)
        self.assertNotIn(("1", "0"), harness._moderation_context_data)

    def test_lexicon_screening_stops_after_first_match(self):
        harness = _CombinedHarness()
        harness._compiled_lexicon = {"swear": _FirstOnlyAutomaton()}

        result = harness._check_lexicon("hit repeatedly hit")

        self.assertEqual(result, {"swear": True})

    def test_llm_failures_fail_closed_for_strict_local_rules(self):
        event = _Event([])
        harness = _Harness()
        for response in ("not-json", RuntimeError("provider down"), asyncio.TimeoutError()):
            result = asyncio.run(_LLMHarness(response)._call_llm_for_moderation(
                event, "cs", {"swear": True}, group_id="1"
            ))
            self.assertTrue(result["fallback"])
            self.assertTrue(harness._llm_failure_requires_rule_penalty(
                result, {"swear": True}, "cs"
            ))

        fallback = {"violation": False, "reason": "failed", "fallback": True}
        self.assertFalse(harness._llm_failure_requires_rule_penalty(
            fallback, {"ad": True}, "这是一个普通问题"
        ))
        self.assertTrue(harness._llm_failure_requires_rule_penalty(
            fallback, {"oversized": True}
        ))
        self.assertFalse(harness._llm_failure_requires_rule_penalty(
            fallback, {"political": True}
        ))
        self.assertFalse(harness._llm_failure_requires_rule_penalty(
            {"violation": False, "fallback": False}, {"swear": True}
        ))

    def test_llm_fallback_distinguishes_ambiguous_and_clear_swear_hits(self):
        fallback = {"violation": False, "reason": "failed", "fallback": True}
        harness = _StreamHarness("啥子", "cs")

        for text in ("你在说啥子", "啥子啥子"):
            self.assertFalse(harness._llm_failure_requires_rule_penalty(
                fallback, {"swear": True}, text
            ))
        for text in ("cs", "啥子 cs"):
            self.assertTrue(harness._llm_failure_requires_rule_penalty(
                fallback, {"swear": True}, text
            ))

        for text in ("这是一个普通问题", "我们聊一下课程项目", "需要了解一下"):
            self.assertFalse(harness._llm_failure_requires_rule_penalty(
                fallback, {"ad": True}, text
            ))

    def test_learned_keywords_never_fail_closed_without_llm(self):
        """自适应学习词（learned_ad/learned_swear）在 LLM 失效时不得未经确认就撤回。"""
        harness = _Harness()
        fallback = {"violation": False, "fallback": True}
        # 仅学习词命中 → LLM 降级时应放行（返回 False），不 fail-closed
        for hits in ({"learned_swear": True}, {"learned_ad": True},
                     {"learned_swear": True, "learned_ad": True}):
            self.assertFalse(harness._llm_failure_requires_rule_penalty(
                fallback, dict(hits), "谁要AI中转账号"
            ))
        # 但人工词库的 swear 命中仍按原逻辑 fail-closed（回归保护）
        self.assertTrue(harness._llm_failure_requires_rule_penalty(
            fallback, {"swear": True}, "cs"
        ))

    def test_moderation_llm_rejects_numeric_boolean_values(self):
        event = _Event([])
        for numeric in (0, 1, -1, 0.5):
            result = asyncio.run(_LLMHarness(
                json.dumps({"violation": numeric, "reason": "malformed"})
            )._call_llm_for_moderation(
                event, "flagged", {"swear": True}, group_id="1"
            ))
            self.assertFalse(result["violation"])
            self.assertTrue(result["fallback"])


if __name__ == "__main__":
    unittest.main()
