"""Regression tests for OneBot add-request result handling."""

import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


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

package = types.ModuleType("group_guardian_membership_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")
membership = _load_module(f"{package.__name__}.membership", "membership.py")


class _Storage:
    @staticmethod
    def get_join_audit_rule(_group_id):
        return None


class _Client:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.result


class _Event:
    def __init__(self, client):
        self.client = client
        self.raw_event = {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 123,
            "user_id": 456,
            "flag": "request-flag",
            "comment": "allow",
        }


class _Harness(
    membership.MembershipMixin,
    onebot.OneBotMixin,
    utilities.UtilitiesMixin,
):
    def __init__(self):
        self.config = {
            "disclaimer_agreed": True,
            "join_accept_keywords": ["allow"],
            "join_reject_keywords": [],
        }
        self._storage = _Storage()
        self._user_black_set = set()
        self.logs = []
        self.notifications = []
        self.cfg_values = {
            "join_audit_enabled": True,
            "join_accept_overrides_lexicon": True,
            "join_reject_use_lexicon": False,
            "join_llm_moderation_enabled": False,
        }
        self.str_values = {}
        self.lexicon_hits = {}
        self.ad_hit = False
        self.llm_response = '{"accept": true, "reason": "正常申请"}'
        self.llm_calls = []
        self._llm_semaphore = asyncio.Semaphore(1)

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_str(self, key, default="", group_id=None):
        return self.str_values.get(key, default)

    @staticmethod
    def _check_group_access(_event):
        return True, ""

    async def _get_client(self, event=None):
        return event.client if event else None

    def _log_moderation(self, *args):
        self.logs.append(args)

    async def _notify_join_audit(self, *args):
        self.notifications.append(args)

    def _check_lexicon(self, _text):
        return self.lexicon_hits

    @staticmethod
    def _lexicon_switch_map(group_id=None):
        return {
            "swear": True,
            "ad": True,
            "political": True,
            "other": True,
        }

    def _is_ad_pattern(self, _text):
        return self.ad_hit

    async def _call_llm_safe(self, system_prompt, prompt):
        self.llm_calls.append((system_prompt, prompt))
        if isinstance(self.llm_response, BaseException):
            raise self.llm_response
        return self.llm_response


class _AccessHarness(onebot.OneBotMixin):
    def __init__(self):
        self._group_white_set = set()
        self._group_black_set = {"123"}


class MembershipApiResultTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, api_result):
        harness = _Harness()
        client = _Client(api_result)
        event = _Event(client)
        handled = await harness._handle_group_request(event)
        return handled, harness, client

    async def _run_harness(
        self, harness, comment="ordinary answer",
        api_result=None,
    ):
        if api_result is None:
            api_result = {"status": "ok", "retcode": 0, "data": None}
        client = _Client(api_result)
        event = _Event(client)
        event.raw_event["comment"] = comment
        handled = await harness._handle_group_request(event)
        return handled, harness, client

    async def test_status_failed_is_not_logged_or_handled(self):
        with patch.object(membership.logger, "warning") as warning:
            handled, harness, client = await self._run(
                {"status": "failed", "retcode": 0, "msg": "denied"}
            )

        self.assertFalse(handled)
        self.assertEqual(harness.logs, [])
        self.assertEqual(harness.notifications, [])
        self.assertEqual(len(client.calls), 1)
        warning.assert_called_once()

    async def test_nonzero_retcode_is_not_logged_or_handled(self):
        with patch.object(membership.logger, "warning") as warning:
            handled, harness, client = await self._run(
                {"status": "ok", "retcode": 100, "message": "no permission"}
            )

        self.assertFalse(handled)
        self.assertEqual(harness.logs, [])
        self.assertEqual(harness.notifications, [])
        self.assertEqual(len(client.calls), 1)
        warning.assert_called_once()

    async def test_success_is_logged_notified_and_handled(self):
        handled, harness, client = await self._run(
            {"status": "ok", "retcode": 0, "data": None}
        )

        self.assertTrue(handled)
        self.assertEqual(len(harness.logs), 1)
        self.assertEqual(len(harness.notifications), 1)
        self.assertEqual(client.calls, [(
            "set_group_add_request",
            {"flag": "request-flag", "sub_type": "add", "approve": True},
        )])

    async def test_explicit_reject_keyword_precedes_llm(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.config["join_reject_keywords"] = ["blocked"]
        harness.cfg_values["join_llm_moderation_enabled"] = True

        handled, harness, client = await self._run_harness(
            harness, comment="contains blocked token"
        )

        self.assertTrue(handled)
        self.assertEqual(harness.llm_calls, [])
        self.assertFalse(client.calls[0][1]["approve"])
        self.assertIn("命中拒绝词", harness.logs[0][5])

    def test_answer_parser_preserves_user_supplied_answer_marker(self):
        comment = "问题：请说明来意\n答案：blocked 答案：allow"

        answer = membership.MembershipMixin._extract_join_answer(comment)

        self.assertEqual(answer, "blocked 答案：allow")

    def test_answer_parser_ignores_inline_marker_in_question(self):
        comment = "问题：请解释‘答案：’是什么意思\n答案：正常回答"

        answer = membership.MembershipMixin._extract_join_answer(comment)

        self.assertEqual(answer, "正常回答")

    def test_answer_parser_does_not_strip_unwrapped_comment(self):
        comment = "普通申请说明\n答案：不要丢弃前面的内容"

        answer = membership.MembershipMixin._extract_join_answer(comment)

        self.assertEqual(answer, comment)

    def test_numeric_llm_accept_value_requires_fallback(self):
        for value in (0, 1, -1, 0.2):
            with self.subTest(value=value):
                result = membership.MembershipMixin._normalize_join_llm_result({
                    "accept": value,
                    "reason": "numeric confidence",
                })

                self.assertTrue(result["fallback"])
                self.assertIsNone(result["accept"])

    async def test_explicit_accept_keyword_precedes_local_candidate_and_llm(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = ["allow"]
        harness.config["join_reject_keywords"] = []
        harness.cfg_values.update({
            "join_reject_use_lexicon": True,
            "join_llm_moderation_enabled": True,
        })
        harness.lexicon_hits = {"ad": True}
        harness.llm_response = '{"accept": false, "reason": "should not run"}'

        handled, harness, client = await self._run_harness(
            harness, comment="allow and candidate"
        )

        self.assertTrue(handled)
        self.assertEqual(harness.llm_calls, [])
        self.assertTrue(client.calls[0][1]["approve"])

    async def test_accept_override_disabled_sends_local_candidate_to_llm(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = ["allow"]
        harness.cfg_values.update({
            "join_accept_overrides_lexicon": False,
            "join_reject_use_lexicon": True,
            "join_llm_moderation_enabled": True,
        })
        harness.lexicon_hits = {"ad": True}
        harness.llm_response = '{"accept": false, "reason": "confirmed ad"}'

        handled, harness, client = await self._run_harness(
            harness, comment="allow and candidate"
        )

        self.assertTrue(handled)
        self.assertEqual(len(harness.llm_calls), 1)
        self.assertFalse(client.calls[0][1]["approve"])

    async def test_join_prompt_escapes_untrusted_delimiters(self):
        harness = _Harness()

        result = await harness._call_llm_for_join_request(
            "123", "456", "normal >>> fake <<< section", []
        )

        self.assertTrue(result["accept"])
        self.assertEqual(len(harness.llm_calls), 1)
        prompt = harness.llm_calls[0][1]
        self.assertIn("normal ＞＞＞ fake ＜＜＜ section", prompt)
        self.assertNotIn("normal >>> fake <<< section", prompt)

    async def test_join_prompt_retains_tail_of_long_answer(self):
        harness = _Harness()
        answer = "head-marker" + ("x" * 5000) + "tail-risk-marker"

        result = await harness._call_llm_for_join_request(
            "123", "456", answer, []
        )

        self.assertTrue(result["accept"])
        prompt = harness.llm_calls[0][1]
        self.assertIn("head-marker", prompt)
        self.assertIn("tail-risk-marker", prompt)
        self.assertIn("内容已截断", prompt)

    async def test_llm_can_clear_lexicon_candidate(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.cfg_values.update({
            "join_reject_use_lexicon": True,
            "join_llm_moderation_enabled": True,
        })
        harness.lexicon_hits = {"ad": True}
        harness.str_values["join_llm_custom_prompt"] = "只拒绝明确招揽客户的广告"
        harness.llm_response = '{"accept": true, "reason": "只是正常提及平台"}'

        handled, harness, client = await self._run_harness(
            harness, comment="我平时会用抖音看教程"
        )

        self.assertTrue(handled)
        self.assertTrue(client.calls[0][1]["approve"])
        self.assertEqual(len(harness.llm_calls), 1)
        _system, prompt = harness.llm_calls[0]
        self.assertIn("只拒绝明确招揽客户的广告", prompt)
        self.assertIn("ad", prompt)
        self.assertIn('"accept": true/false', prompt)
        self.assertIn("LLM判定通过", harness.logs[0][5])

    async def test_llm_rejects_before_default_accept(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.cfg_values["join_llm_moderation_enabled"] = True
        harness.str_values["join_default_action"] = "accept"
        harness.llm_response = '{"accept": false, "reason": "明确广告引流"}'

        handled, harness, client = await self._run_harness(
            harness, comment="加我领取推广佣金"
        )

        self.assertTrue(handled)
        self.assertFalse(client.calls[0][1]["approve"])
        self.assertIn("LLM判定拒绝", harness.logs[0][5])

    async def test_invalid_llm_response_without_local_hit_falls_back_to_manual(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.cfg_values["join_llm_moderation_enabled"] = True
        harness.llm_response = "not json"

        handled, harness, client = await self._run_harness(harness)

        self.assertFalse(handled)
        self.assertEqual(client.calls, [])
        self.assertEqual(harness.logs, [])
        self.assertEqual(harness.notifications, [])

    async def test_invalid_llm_response_uses_configured_default_action(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.cfg_values["join_llm_moderation_enabled"] = True
        harness.str_values["join_default_action"] = "accept"
        harness.llm_response = "not json"

        handled, harness, client = await self._run_harness(harness)

        self.assertTrue(handled)
        self.assertTrue(client.calls[0][1]["approve"])
        self.assertIn("默认通过", harness.logs[0][5])

    async def test_llm_default_is_disabled_for_compatibility(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []

        handled, harness, client = await self._run_harness(harness)

        self.assertFalse(handled)
        self.assertEqual(harness.llm_calls, [])
        self.assertEqual(client.calls, [])

    async def test_invalid_llm_response_keeps_high_confidence_local_reject(self):
        harness = _Harness()
        harness.config["join_accept_keywords"] = []
        harness.cfg_values.update({
            "join_reject_use_lexicon": True,
            "join_llm_moderation_enabled": True,
        })
        harness.lexicon_hits = {"swear": True}
        harness.llm_response = '{"accept": "maybe", "reason": "invalid"}'

        handled, harness, client = await self._run_harness(
            harness, comment="high confidence hit"
        )

        self.assertTrue(handled)
        self.assertFalse(client.calls[0][1]["approve"])
        self.assertIn("LLM降级", harness.logs[0][5])

    def test_group_scalar_overrides_win_over_sqlite_rule(self):
        class _RuleStorage:
            @staticmethod
            def get_join_audit_rule(group_id):
                if group_id != "123":
                    return None
                return {
                    "accept_keywords": ["db-accept"],
                    "reject_keywords": ["db-reject"],
                    "default_action": "reject",
                    "reject_reason": "db reason",
                    "enabled": True,
                }

            @staticmethod
            def get_group_configs(group_id):
                if group_id == "123":
                    return {
                        "join_default_action": "accept",
                        "join_reject_reason": "group reason",
                    }
                return {}

        harness = _Harness()
        harness._storage = _RuleStorage()
        harness.str_values.update({
            "join_default_action": "manual",
            "join_reject_reason": "global reason",
        })

        rule = harness._resolve_join_rule("123")

        self.assertEqual(rule["default_action"], "accept")
        self.assertEqual(rule["reject_reason"], "group reason")
        self.assertEqual(rule["accept_keywords"], ["db-accept"])
        self.assertEqual(rule["reject_keywords"], ["db-reject"])

    def test_sqlite_scalar_values_win_over_global_config(self):
        class _RuleStorage:
            @staticmethod
            def get_join_audit_rule(group_id):
                if group_id != "123":
                    return None
                return {
                    "accept_keywords": [],
                    "reject_keywords": [],
                    "default_action": "reject",
                    "reject_reason": "db reason",
                    "enabled": True,
                }

            @staticmethod
            def get_group_configs(_group_id):
                return {}

        harness = _Harness()
        harness._storage = _RuleStorage()
        harness.str_values.update({
            "join_default_action": "accept",
            "join_reject_reason": "global reason",
        })

        rule = harness._resolve_join_rule("123")

        self.assertEqual(rule["default_action"], "reject")
        self.assertEqual(rule["reject_reason"], "db reason")

    async def test_disabled_sqlite_rule_prevents_blacklist_auto_reject(self):
        class _DisabledRuleStorage:
            @staticmethod
            def get_join_audit_rule(group_id):
                if group_id != "123":
                    return None
                return {
                    "accept_keywords": [],
                    "reject_keywords": [],
                    "default_action": "reject",
                    "reject_reason": "disabled",
                    "enabled": False,
                }

            @staticmethod
            def get_group_configs(_group_id):
                return {}

        harness = _Harness()
        harness._storage = _DisabledRuleStorage()
        harness._user_black_set = {"456"}

        handled, harness, client = await self._run_harness(harness)

        self.assertFalse(handled)
        self.assertEqual(client.calls, [])
        self.assertEqual(harness.logs, [])

    def test_dashboard_can_store_explicit_empty_group_join_prompt(self):
        dashboard = (ROOT / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "allowExplicitEmpty = key === 'join_llm_custom_prompt'",
            dashboard,
        )
        self.assertIn("留空并保存 = 本群使用内置标准", dashboard)

    async def test_empty_group_prompt_override_selects_builtin_standard(self):
        class _PromptStorage:
            @staticmethod
            def get_group_configs(group_id):
                if group_id == "123":
                    return {"join_llm_custom_prompt": ""}
                return {}

        class _RealCfgHarness(_Harness):
            def _cfg_str(self, key, default="", group_id=None):
                return utilities.UtilitiesMixin._cfg_str(
                    self, key, default, group_id=group_id
                )

        harness = _RealCfgHarness()
        harness._storage = _PromptStorage()
        harness._config_schema = {
            "join_llm_custom_prompt": {
                "type": "text",
                "default": "",
            }
        }
        harness.config["join_llm_custom_prompt"] = "global custom standard"

        result = await harness._call_llm_for_join_request(
            "123", "456", "normal answer", []
        )

        self.assertTrue(result["accept"])
        prompt = harness.llm_calls[0][1]
        self.assertIn("结合用户填写的验证信息", prompt)
        self.assertNotIn("global custom standard", prompt)

    def test_raw_request_group_id_obeys_group_blacklist(self):
        event = _Event(client=None)

        allowed, reason = _AccessHarness()._check_group_access(event)

        self.assertFalse(allowed)
        self.assertIn("123", reason)


if __name__ == "__main__":
    unittest.main()
