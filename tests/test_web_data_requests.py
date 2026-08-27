"""Runtime coverage for WebUI data and OneBot response handling."""

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "group_guardian_web_request_tests"


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    astrbot.api = api


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
automaton = types.ModuleType(f"{PACKAGE}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
utils = _load_module(f"{PACKAGE}.utils", "utils.py")
_load_module(f"{PACKAGE}.constants", "constants.py")
web = _load_module(f"{PACKAGE}.web", "web.py")


class _Request:
    def __init__(self, args=None, body=None):
        self.args = dict(args or {})
        self.body = body or {}

    async def get_json(self, **_kwargs):
        return self.body


class _Storage:
    def __init__(self, configured=None):
        self.configured = list(configured or [])

    def list_configured_groups(self):
        return list(self.configured)


class _Client:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.error:
            raise self.error
        return self.result


class _NoCacheClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, dict(kwargs)))
        if "no_cache" in kwargs:
            raise TypeError("unexpected keyword argument 'no_cache'")
        return self.responses.pop(0)


class _NestedNoCacheClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, dict(kwargs)))
        if "no_cache" in kwargs:
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "status": "failed",
                    "message": "no_cache is unsupported",
                },
            }
        return {"status": "ok", "retcode": 0, "data": []}


class _GenericNoCacheFailureClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, dict(kwargs)))
        if "no_cache" in kwargs:
            return {
                "status": "failed",
                "retcode": 1400,
                "message": "request parameter rejected",
            }
        return {"status": "ok", "retcode": 0, "data": []}


class _ActionFailedFixture(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__("action failed")


class _ActionFailedThenSuccessClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, dict(kwargs)))
        if "no_cache" in kwargs:
            raise _ActionFailedFixture({
                "status": "failed",
                "retcode": 1400,
                "message": "request parameter rejected",
            })
        return self.result


class _Context:
    def __init__(self, fail_suffix=""):
        self.calls = []
        self.fail_suffix = fail_suffix

    def register_web_api(self, path, handler, methods, desc):
        if self.fail_suffix and path.endswith(self.fail_suffix):
            raise RuntimeError("host rejected route")
        self.calls.append((path, handler, methods, desc))


class _Harness(web.WebMixin, utils.UtilitiesMixin):
    def __init__(self, result=None, *, configured=None, context=None):
        self.client = _Client(result)
        self.context = context
        self._storage = _Storage(configured)
        self._web_group_cache = {"ts": 0.0, "data": []}
        self._web_member_cache = {}
        self._moderation_logs = []
        self._group_white_set = set()
        self._group_black_set = set()
        self.group_white_list = []
        self.group_black_list = []
        self.user_black_list = []
        self.user_white_list = []

    async def _get_client(self, _event=None):
        return self.client

    @staticmethod
    def _today_start():
        return 0

    @staticmethod
    def _get_admin_list():
        return []

    def __getattr__(self, name):
        if name.startswith("_web_"):
            async def placeholder(*_args, **_kwargs):
                return {"status": "success"}

            placeholder.__name__ = name
            return placeholder
        raise AttributeError(name)


class WebDataRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        web.jsonify = lambda payload: payload
        web.quart_request = _Request()

    async def test_onebot_error_classifier_rejects_failed_and_malformed_packets(self):
        self.assertEqual(web.WebMixin._onebot_web_error([{"group_id": 1}]), "")
        self.assertEqual(
            web.WebMixin._onebot_web_error({"status": "ok", "retcode": 0}),
            "",
        )
        self.assertIn(
            "denied",
            web.WebMixin._onebot_web_error({
                "status": "failed", "retcode": 0, "message": "denied"
            }),
        )
        self.assertIn(
            "error",
            web.WebMixin._onebot_web_error({
                "status": "error", "retcode": 0, "msg": "error"
            }),
        )
        self.assertIn(
            "str",
            web.WebMixin._onebot_web_error("not a response"),
        )
        self.assertIn(
            "denied",
            web.WebMixin._onebot_web_error({"ok": False, "error": "denied"}),
        )
        self.assertEqual(
            web.WebMixin._onebot_web_error({
                "status": "ok", "retcode": "success", "data": [],
            }),
            "",
        )
        self.assertEqual(
            web.WebMixin._onebot_web_error({"code": 200, "data": []}),
            "",
        )
        self.assertIn(
            "gateway",
            web.WebMixin._onebot_web_error({
                "code": 502, "message": "gateway unavailable", "data": [],
            }),
        )
        self.assertIn(
            "offline",
            web.WebMixin._onebot_web_error({
                "statusCode": "503", "msg": "offline", "data": [],
            }),
        )

    def test_common_onebot_result_checker_handles_serialized_nested_errors(self):
        harness = _Harness()
        ok, error = harness._check_api_result(
            json.dumps({"status": "failed", "retcode": 100, "msg": "offline"})
        )
        self.assertFalse(ok)
        self.assertIn("offline", error)

        ok, error = harness._check_api_result({
            "status": "ok",
            "retcode": 0,
            "data": {"response": {"status": "error", "message": "nested"}},
        })
        self.assertFalse(ok)
        self.assertIn("nested", error)

        ok, error = harness._check_api_result(
            json.dumps({"status": "ok", "retcode": 0, "data": []})
        )
        self.assertTrue(ok)
        self.assertEqual("", error)

        ok, error = harness._check_api_result({"status": 0, "data": []})
        self.assertTrue(ok)
        self.assertEqual("", error)

        ok, error = harness._check_api_result({
            "status": "ok", "retcode": "success", "data": [],
        })
        self.assertTrue(ok)
        self.assertEqual("", error)

    async def test_web_call_retries_without_no_cache_when_adapter_rejects_it(self):
        harness = _Harness()
        client = _NoCacheClient([
            {"status": "ok", "retcode": 0, "data": []},
        ])

        result = await harness._call_onebot_web(
            client, "get_group_member_list", timeout=1,
            group_id=123, no_cache=True,
        )

        self.assertEqual(result["data"], [])
        self.assertEqual(len(client.calls), 2)
        self.assertIn("no_cache", client.calls[0][1])
        self.assertNotIn("no_cache", client.calls[1][1])

    async def test_web_call_detects_nested_no_cache_error_envelopes(self):
        harness = _Harness()
        client = _NestedNoCacheClient()

        result = await harness._call_onebot_web(
            client, "get_group_member_list", timeout=1,
            group_id=123, no_cache=True,
        )

        self.assertEqual(result["data"], [])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("no_cache", client.calls[1][1])

    async def test_web_call_retries_generic_no_cache_failure_envelopes(self):
        harness = _Harness()
        client = _GenericNoCacheFailureClient()

        result = await harness._call_onebot_web(
            client, "get_group_list", timeout=1, no_cache=True,
        )

        self.assertEqual(result["data"], [])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("no_cache", client.calls[1][1])

    async def test_web_call_retries_aiocqhttp_action_failed_payload(self):
        harness = _Harness()
        client = _ActionFailedThenSuccessClient({
            "status": "ok", "retcode": 0, "data": [],
        })

        result = await harness._call_onebot_web(
            client, "get_group_list", timeout=1, no_cache=True,
        )

        self.assertEqual(result["data"], [])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("no_cache", client.calls[1][1])

    async def test_force_group_refresh_does_not_send_unsupported_no_cache(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(harness.client.calls[0][0], "get_group_list")
        self.assertEqual(harness.client.calls[0][1], {})

    async def test_force_member_refresh_only_sends_standard_group_id(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            harness.client.calls[0][1], {"group_id": 123}
        )

    async def test_group_list_accepts_multiple_nested_data_wrappers(self):
        harness = _Harness({
            "status": "ok",
            "retcode": 0,
            "data": {"data": {"group_list": [
                {"group_id": 123, "group_name": "fixture", "member_count": 4},
            ]}},
        })
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["group_id"], "123")
        self.assertEqual(result["data"][0]["member_count"], 4)

    async def test_group_list_prefers_populated_group_list_over_empty_messages(self):
        harness = _Harness({
            "status": "ok",
            "retcode": 0,
            "data": {
                "messages": [],
                "group_list": [
                    {"group_id": 345, "group_name": "prefer", "member_count": 6},
                ],
            },
        })
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["group_id"], "345")
        self.assertEqual(result["data"][0]["group_name"], "prefer")

    async def test_group_list_accepts_camel_case_list_wrapper(self):
        harness = _Harness({
            "status": "ok",
            "retcode": 0,
            "data": {"groupList": [
                {"group_id": 234, "group_name": "camel", "member_count": 5},
            ]},
        })
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"], [{
            "group_id": "234",
            "group_name": "camel",
            "member_count": 5,
            "avatar": "https://p.qlogo.cn/gh/234/234/",
            "is_white": False,
            "is_black": False,
            "has_config": False,
            "today_blocked": 0,
        }])

    def test_web_list_checker_matches_message_list_extractor(self):
        result = {"data": {"messageList": [{"message_id": 1}]}}

        self.assertTrue(web.WebMixin._onebot_web_has_list_result(result))
        self.assertEqual(utils.UtilitiesMixin._extract_list_result(result), [
            {"message_id": 1},
        ])

    def test_dashboard_logs_use_server_pager_instead_of_slicing_100(self):
        src = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("var LOGS_STATE = { page: 1, pageSize: 50 };", src)
        self.assertIn("page_size: LOGS_STATE.pageSize", src)
        self.assertIn("renderPager(LOGS_STATE.page, LOGS_STATE.pageSize, total, 'logs')", src)
        self.assertNotIn("logs.slice(0, 100)", src)

    async def test_group_list_accepts_json_serialized_onebot_envelope(self):
        harness = _Harness(
            '{"status":"ok","retcode":0,"data":{"group_list":[]}}'
        )
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"], [])

    async def test_group_list_rejects_http_style_error_envelope(self):
        harness = _Harness({
            "code": 503,
            "message": "adapter unavailable",
            "data": [],
        })
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "error")
        self.assertIn("adapter unavailable", result["message"])

    async def test_successful_empty_group_list_is_cached_as_a_real_snapshot(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        web.quart_request = _Request({"force": "1"})

        first = await harness._web_get_groups()

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["data"], [])
        self.assertGreater(harness._web_group_cache["ts"], 0)
        self.assertEqual(len(harness.client.calls), 1)

        harness.client.result = {"status": "failed", "retcode": 100, "msg": "offline"}
        web.quart_request = _Request()
        second = await harness._web_get_groups()

        self.assertEqual(second["data"], [])
        self.assertEqual(len(harness.client.calls), 1)

    async def test_failed_group_packet_does_not_replace_cache_with_empty_success(self):
        harness = _Harness({
            "status": "failed", "retcode": 100, "message": "permission denied",
        })
        cached = [{"group_id": "123", "group_name": "cached"}]
        harness._web_group_cache = {"ts": 0.0, "data": cached}
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["stale"])
        self.assertEqual(result["data"], cached)
        self.assertEqual(harness._web_group_cache["data"], cached)

    async def test_failed_group_packet_without_fallback_is_an_error(self):
        harness = _Harness({"status": "failed", "retcode": 100, "msg": "denied"})
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "error")
        self.assertIn("denied", result["message"])

    async def test_nonempty_malformed_group_rows_do_not_erase_cache(self):
        harness = _Harness({
            "status": "ok", "retcode": 0,
            "data": [{"name": "missing group id"}, "not-a-group"],
        })
        cached = [{"group_id": "123", "group_name": "cached"}]
        harness._web_group_cache = {"ts": 1.0, "data": cached}
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["stale"])
        self.assertEqual(result["data"], cached)
        self.assertEqual(harness._web_group_cache["data"], cached)

    async def test_nonempty_malformed_group_rows_without_cache_are_an_error(self):
        harness = _Harness({
            "status": "ok", "retcode": 0,
            "data": [{"name": "missing group id"}],
        })
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "error")
        self.assertIn("malformed", result["message"])

    async def test_member_list_accepts_nested_group_members_payload(self):
        harness = _Harness({
            "status": "ok",
            "retcode": 0,
            "data": {"data": {"group_members": [
                {"user_id": 456, "nickname": "member", "role": "admin"},
            ]}},
        })
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["user_id"], "456")
        self.assertEqual(result["data"][0]["role"], "admin")

    async def test_member_list_skips_malformed_rows_and_normalizes_empty_fields(self):
        harness = _Harness({
            "status": "ok",
            "retcode": 0,
            "payload": {"members": [
                None,
                "not-a-member",
                {"user_id": None},
                {"user_id": 456, "nickname": None, "card": None, "role": None},
            ]},
        })
        web.quart_request = _Request({"group_id": "123"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"], [{
            "user_id": "456",
            "nickname": "456",
            "card": "",
            "display_name": "456",
            "role": "member",
            "title": "",
            "avatar": "https://q.qlogo.cn/headimg_dl?dst_uin=456&spec=640",
            "is_plugin_admin": False,
        }])
        self.assertEqual(harness.client.calls[-1][1], {"group_id": 123})

    async def test_failed_member_packet_without_cache_is_not_empty_success(self):
        harness = _Harness({"status": "failed", "retcode": 100, "msg": "denied"})
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "error")
        self.assertIn("denied", result["message"])

    async def test_nonempty_malformed_member_rows_do_not_erase_cache(self):
        harness = _Harness({
            "status": "ok", "retcode": 0,
            "data": [{"nickname": "missing user id"}, None],
        })
        cached = [{"user_id": "456", "display_name": "cached"}]
        harness._web_member_cache = {"123": {"ts": 1.0, "data": cached}}
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["stale"])
        self.assertEqual(result["data"], cached)
        self.assertEqual(harness._web_member_cache["123"]["data"], cached)

    async def test_nonempty_malformed_member_rows_without_cache_are_an_error(self):
        harness = _Harness({
            "status": "ok", "retcode": 0,
            "data": [{"nickname": "missing user id"}],
        })
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "error")
        self.assertIn("malformed", result["message"])

    async def test_successful_empty_member_list_is_cached_as_a_real_snapshot(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        web.quart_request = _Request({"group_id": "123", "force": "1"})

        first = await harness._web_get_group_members()

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["data"], [])
        self.assertGreater(harness._web_member_cache["123"]["ts"], 0)
        self.assertEqual(len(harness.client.calls), 1)

        harness.client.result = {"status": "failed", "retcode": 100, "msg": "offline"}
        web.quart_request = _Request({"group_id": "123"})
        second = await harness._web_get_group_members()

        self.assertEqual(second["data"], [])
        self.assertEqual(len(harness.client.calls), 1)

    async def test_success_packet_without_a_list_field_is_an_error(self):
        harness = _Harness({"status": "ok", "retcode": 0})
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "error")
        self.assertIn("no list data", result["message"])

    async def test_invalid_group_id_is_rejected_before_onebot_request(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        web.quart_request = _Request({"group_id": "not-a-number"})

        result = await harness._web_get_group_members()

        self.assertEqual(result["status"], "error")
        self.assertIn("无效", result["message"])
        self.assertEqual(harness.client.calls, [])

    async def test_older_cache_snapshot_cannot_replace_newer_one(self):
        harness = _Harness()

        harness._store_web_group_snapshot(20, [{"group_id": "new"}])
        harness._store_web_group_snapshot(10, [{"group_id": "old"}])

        self.assertEqual(harness._web_group_cache["data"][0]["group_id"], "new")

    async def test_real_snapshot_replaces_an_older_request_fallback(self):
        harness = _Harness()

        harness._store_web_group_snapshot(
            20, [{"group_id": "fallback"}], stale=True
        )
        harness._store_web_group_snapshot(
            10, [{"group_id": "real"}]
        )

        self.assertEqual(harness._web_group_cache["data"][0]["group_id"], "real")
        self.assertFalse(harness._web_group_cache["_stale"])

    def test_newer_group_fallback_cannot_replace_real_snapshot(self):
        harness = _Harness()

        harness._store_web_group_snapshot(
            20, [{"group_id": "real"}]
        )
        harness._store_web_group_snapshot(
            30, [{"group_id": "fallback"}], stale=True
        )

        self.assertEqual(harness._web_group_cache["data"][0]["group_id"], "real")
        self.assertFalse(harness._web_group_cache["_stale"])

    def test_member_snapshot_uses_real_data_over_newer_fallback(self):
        harness = _Harness()

        harness._store_web_member_snapshot(
            "123", 20, [{"user_id": "real"}]
        )
        harness._store_web_member_snapshot(
            "123", 30, [{"user_id": "fallback"}], stale=True
        )

        self.assertEqual(
            harness._web_member_cache["123"]["data"][0]["user_id"], "real"
        )
        self.assertFalse(harness._web_member_cache["123"]["_stale"])

    async def test_failed_older_request_reads_cache_completed_while_waiting(self):
        harness = _Harness({"status": "ok", "retcode": 0, "data": []})
        harness._web_group_cache = {
            "ts": 10.0, "data": [{"group_id": "old"}],
        }

        async def fail_after_newer_snapshot(*_args, **_kwargs):
            harness._web_group_cache = {
                "ts": 20.0, "data": [{"group_id": "new"}],
            }
            raise RuntimeError("late request failed")

        harness._call_onebot_web = fail_after_newer_snapshot
        web.quart_request = _Request({"force": "1"})

        result = await harness._web_get_groups()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["group_id"], "new")

    async def test_web_wrapper_returns_error_envelope_for_unhandled_exception(self):
        harness = _Harness()

        async def broken_handler():
            raise RuntimeError("fixture failure")

        wrapped = harness._wrap_web_handler(broken_handler)
        result = await wrapped()

        self.assertEqual(result, {
            "status": "error",
            "message": "fixture failure",
        })

    async def test_lexicon_export_errors_use_non_success_http_status(self):
        harness = _Harness()
        web.quart_request = _Request()

        result = await harness._web_export_lexicon_keywords()

        self.assertEqual(result[0]["status"], "error")
        self.assertEqual(result[1], 400)

    async def test_lexicon_export_uses_utf8_filename_and_csv_response(self):
        harness = _Harness()
        harness._storage.list_lexicon_keywords = (
            lambda category, query, limit, offset: [
                {"id": 1, "keyword": "测试"},
            ]
        )
        web.quart_request = _Request({"category": "广告", "q": ""})

        result = await harness._web_export_lexicon_keywords()

        self.assertEqual(result[1], 200)
        self.assertIn("text/csv", result[2]["Content-Type"])
        self.assertIn("filename*=UTF-8''", result[2]["Content-Disposition"])
        self.assertIn("%E5%B9%BF%E5%91%8A", result[2]["Content-Disposition"])
        self.assertIn("测试", result[0])

    async def test_log_export_storage_failure_uses_non_success_http_status(self):
        harness = _Harness()

        def fail_list_logs(**_kwargs):
            raise RuntimeError("storage unavailable")

        harness._storage.list_logs = fail_list_logs
        web.quart_request = _Request({"format": "csv"})

        result = await harness._web_export_logs()

        self.assertEqual(result[0]["status"], "error")
        self.assertIn("storage unavailable", result[0]["message"])
        self.assertEqual(result[1], 500)

    async def test_logs_page_returns_items_and_total_for_pager(self):
        harness = _Harness()
        rows = [
            {"id": idx, "group_id": "1", "user_id": "2", "action": "撤回"}
            for idx in range(1, 8)
        ]

        def list_logs(limit, offset, group_id="", user_id="", action=""):
            self.assertEqual(limit, 3)
            self.assertEqual(offset, 3)
            return rows[offset:offset + limit]

        harness._storage.list_logs = list_logs
        harness._storage.count_logs_filtered = lambda *args: 7
        harness._storage.feedback_for_log_ids = lambda ids: {
            4: {"verdict": "false_positive", "note": "", "review_status": "open"},
        }
        web.quart_request = _Request({"page": "2", "page_size": "3"})

        result = await harness._web_get_logs()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["page"], 2)
        self.assertEqual(result["data"]["page_size"], 3)
        self.assertEqual(result["data"]["total"], 7)
        self.assertEqual(result["data"]["offset"], 3)
        self.assertEqual([item["id"] for item in result["data"]["items"]], [4, 5, 6])
        self.assertEqual(result["data"]["items"][0]["review_verdict"], "false_positive")

    async def test_logs_offset_still_maps_to_page(self):
        harness = _Harness()
        harness._storage.list_logs = lambda limit, offset, *args: [
            {"id": offset + 1, "group_id": "1", "user_id": "2", "action": "撤回"}
        ]
        harness._storage.count_logs_filtered = lambda *args: 40
        harness._storage.feedback_for_log_ids = lambda ids: {}
        web.quart_request = _Request({"offset": "20", "page_size": "10"})

        result = await harness._web_get_logs()

        self.assertEqual(result["data"]["page"], 3)
        self.assertEqual(result["data"]["offset"], 20)
        self.assertEqual(result["data"]["page_size"], 10)

    async def test_one_bad_route_does_not_stop_remaining_registration(self):
        context = _Context("/providers")
        harness = _Harness(context=context)

        harness._register_web_apis()

        self.assertGreater(len(context.calls), 80)
        self.assertFalse(any(path.endswith("/providers") for path, *_ in context.calls))
        self.assertTrue(any(path.endswith("/stats") for path, *_ in context.calls))


if __name__ == "__main__":
    unittest.main()
