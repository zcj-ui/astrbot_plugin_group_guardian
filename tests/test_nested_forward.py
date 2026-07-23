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
    def __init__(self, chain, client=None):
        self._chain = chain
        self.client = client
        self.raw_event = {}

    def get_messages(self):
        return self._chain

    def get_sender_name(self):
        return "tester"


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


class Plain:
    def __init__(self, text):
        self.text = text


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
        self.llm_calls = 0

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
        if False:
            yield None

    async def _call_llm_for_moderation(self, *_args, **_kwargs):
        self.llm_calls += 1
        return {"violation": False, "fallback": False}


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
        self.assertEqual([item[1] for item in client.calls], ["root", "middle", "leaf"])

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
