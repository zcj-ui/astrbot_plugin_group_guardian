"""Regression tests for notice-driven and polling-based card monitoring."""

import ast
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    event_api = sys.modules.setdefault(
        "astrbot.api.event", types.ModuleType("astrbot.api.event")
    )
    event_api.AstrMessageEvent = object
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
    aiocqhttp_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
        ),
    )
    aiocqhttp_event.AiocqhttpMessageEvent = object
    astrbot.api = api
    astrbot.core = core
    core.platform = platform
    platform.sources = sources
    sources.aiocqhttp = aiocqhttp


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
card_monitor = _load_module("group_guardian_card_monitor", "card_monitor.py")
scheduler = _load_module("group_guardian_scheduler", "scheduler.py")


class _Storage:
    def __init__(self, configured=None):
        self.configured = list(configured or [])

    def list_card_protected(self, group_id=""):
        return []

    def list_configured_groups(self):
        return list(self.configured)

    def get_card_protected(self, group_id, user_id):
        return None


class _Client:
    def __init__(self, group_result, member_result):
        self.group_result = group_result
        self.member_result = member_result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_group_list":
            return self.group_result
        if action == "get_group_member_list":
            return self.member_result
        raise AssertionError(f"unexpected action: {action}")


class _MemberInfoClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.call_kwargs = []

    async def call_action(self, action, **kwargs):
        self.calls += 1
        self.call_kwargs.append(dict(kwargs))
        self.assert_action = action
        return self.responses.pop(0)


class _SnapshotRaceClient(_Client):
    def __init__(self, group_result, member_result, on_member_list):
        super().__init__(group_result, member_result)
        self.on_member_list = on_member_list

    async def call_action(self, action, **kwargs):
        if action == "get_group_member_list":
            self.on_member_list()
        return await super().call_action(action, **kwargs)


class _Event:
    def __init__(self, raw_event):
        self.raw_event = raw_event


class _CardHarness(card_monitor.CardMonitorMixin):
    def __init__(self, client=None):
        self.client = client
        self.config = {"disclaimer_agreed": True}
        self._storage = _Storage()
        self._group_white_set = set()
        self._group_black_set = set()
        self.processed = []
        self.logged = []
        self.cfg_values = {}
        self.group_cfg_values = {}

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_data_result(result):
        if isinstance(result, dict) and "data" in result:
            return result.get("data")
        return result

    @classmethod
    def _extract_list_result(cls, result):
        result = cls._extract_data_result(result)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("members", "items", "list", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    @staticmethod
    def _check_api_result(result, action_name=""):
        if isinstance(result, dict):
            retcode = result.get("retcode", 0)
            if result.get("status") == "failed" or retcode not in (0, None):
                return False, result.get("message") or result.get("msg") or str(retcode)
        return True, ""

    def _cfg(self, key, default=True, group_id=None):
        values = {
            "enabled": True,
            "card_monitor_enabled": True,
            "card_sync_enabled": True,
            "card_log_enabled": False,
            "card_protect_enabled": False,
            "card_audit_link_only": False,
            "card_audit_enabled": False,
            "card_audit_admin_exempt": True,
        }
        if group_id is not None and (str(group_id), key) in self.group_cfg_values:
            return self.group_cfg_values[(str(group_id), key)]
        return self.cfg_values.get(key, values.get(key, default))

    async def _get_client(self, event=None):
        return self.client

    @staticmethod
    def _get_raw_event(event):
        return event.raw_event

    @staticmethod
    def _check_group_access(event):
        # Simulate an AstrBot notice wrapper whose get_group_id path is empty.
        return True, ""

    def _log_card_change(self, *args):
        self.logged.append(args)


class _RecordingCardHarness(_CardHarness):
    async def _process_card_values(self, *args, **kwargs):
        self.processed.append((args, kwargs))
        group_id, user_id, _old, new = args[:4]
        self._remember_card_snapshot(group_id, user_id, new)
        return False


class _RestoreFailureHarness(_CardHarness):
    def _cfg(self, key, default=True, group_id=None):
        if key == "card_audit_link_only":
            return True
        if key == "card_log_enabled":
            return True
        return super()._cfg(key, default, group_id)

    async def _restore_card(self, group_id, user_id, card):
        return False


class _ProtectedStorage(_Storage):
    @staticmethod
    def list_card_protected(group_id=""):
        if group_id and str(group_id) != "100":
            return []
        return [{
            "group_id": "100",
            "user_id": "200",
            "protected_card": "required-card",
        }]

    @staticmethod
    def get_card_protected(group_id, user_id):
        return "required-card"


class _ProtectedJoinHarness(_CardHarness):
    def __init__(self):
        super().__init__()
        self._storage = _ProtectedStorage()
        self.restores = []

    def _cfg(self, key, default=True, group_id=None):
        if key == "card_protect_enabled":
            return True
        return super()._cfg(key, default, group_id)

    async def _restore_card(self, group_id, user_id, card):
        self.restores.append((group_id, user_id, card))
        return True


class _AsyncGate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CardLlmHarness(_CardHarness):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self._llm_semaphore = _AsyncGate()

    async def _call_llm_safe(self, system_prompt, prompt):
        return self.response

    @staticmethod
    def _normalize_llm_moderation_result(data):
        value = data.get("violation") if isinstance(data, dict) else None
        if not isinstance(value, bool):
            return {"violation": False, "fallback": True}
        return {"violation": value, "fallback": False}


class _CardAuditAdminHarness(_CardHarness):
    def __init__(
        self, client=None, roles=None, admin_ids=None, protected=False,
        admin_exempt=True,
    ):
        super().__init__(client)
        self.roles = dict(roles or {})
        self.admin_ids = {str(value) for value in (admin_ids or set())}
        self.protected = protected
        self.admin_exempt = admin_exempt
        self.role_calls = []
        self.restores = []
        if protected:
            self._storage = _ProtectedStorage()

    def _cfg(self, key, default=True, group_id=None):
        values = {
            "card_audit_link_only": True,
            "card_audit_admin_exempt": self.admin_exempt,
            "card_log_enabled": True,
            "card_monitor_notify": False,
            "card_protect_enabled": self.protected,
        }
        if key in values:
            return values[key]
        return super()._cfg(key, default, group_id)

    def _get_all_admin_ids(self):
        return set(self.admin_ids)

    async def _get_member_role(self, event, group_id, user_id):
        key = (str(group_id), str(user_id))
        self.role_calls.append(key)
        return self.roles.get(key, "member")

    async def _restore_card(self, group_id, user_id, card):
        self.restores.append((group_id, user_id, card))
        return True


class CardMonitorTests(unittest.TestCase):
    def test_raw_notice_group_id_still_obeys_blacklist(self):
        harness = _CardHarness()
        harness._group_black_set = {"100"}
        event = _Event({
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 200,
        })

        result = asyncio.run(
            harness._process_card_values(
                "100", "200", "old", "new", event=event, source="join", force=True
            )
        )

        self.assertFalse(result)
        self.assertEqual(harness.logged, [])

    def test_group_increase_ignores_bot_itself(self):
        client = _Client({"data": []}, {"data": []})
        harness = _CardHarness(client)
        event = _Event({
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 200,
            "self_id": 200,
        })

        result = asyncio.run(harness._handle_group_increase(event))

        self.assertFalse(result)
        self.assertEqual(client.calls, [])

    def test_member_info_failed_wrapper_is_not_accepted_as_data(self):
        client = _MemberInfoClient([
            {
                "status": "failed",
                "retcode": 100,
                "data": {"card": "stale-bad", "nickname": "stale"},
            },
            {
                "status": "ok",
                "retcode": 0,
                "data": {"card": "fresh", "nickname": "member"},
            },
        ])
        harness = _CardHarness(client)

        member = asyncio.run(harness._fetch_member_card(client, "100", "200"))

        self.assertEqual(member, ("fresh", "member"))
        self.assertEqual(client.calls, 2)

    def test_member_info_without_card_field_is_not_treated_as_empty_card(self):
        client = _MemberInfoClient([
            {"status": "ok", "retcode": 0, "data": {"nickname": "member"}},
            {"status": "ok", "retcode": 0, "data": {"nickname": "member"}},
        ])
        harness = _CardHarness(client)

        member = asyncio.run(harness._fetch_member_card(client, "100", "200"))

        self.assertIsNone(member)

    def test_member_info_bypasses_protocol_cache(self):
        client = _MemberInfoClient([
            {"status": "ok", "retcode": 0, "data": {"card": "fresh", "nickname": "member"}},
        ])
        harness = _CardHarness(client)

        member = asyncio.run(harness._fetch_member_card(client, "100", "200"))

        self.assertEqual(member, ("fresh", "member"))
        self.assertEqual(client.call_kwargs, [{
            "group_id": 100,
            "user_id": 200,
            "no_cache": True,
        }])

    def test_failed_member_list_does_not_erase_snapshot(self):
        client = _Client(
            {"status": "ok", "retcode": 0, "data": [{"group_id": 100}]},
            {"retcode": 100, "data": []},
        )
        harness = _CardHarness(client)
        harness._card_snapshots = {"100": {"200": "known-card"}}

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(harness._card_snapshots, {"100": {"200": "known-card"}})

    def test_empty_member_list_does_not_erase_snapshot(self):
        client = _Client(
            {"status": "ok", "retcode": 0, "data": [{"group_id": 100}]},
            {"status": "ok", "retcode": 0, "data": []},
        )
        harness = _CardHarness(client)
        harness._card_snapshots = {"100": {"200": "known-card"}}

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(harness._card_snapshots, {"100": {"200": "known-card"}})

    def test_member_list_without_card_field_preserves_snapshot(self):
        client = _Client(
            {"status": "ok", "retcode": 0, "data": [{"group_id": 100}]},
            {"status": "ok", "retcode": 0, "data": [
                {"user_id": 200, "nickname": "member"}
            ]},
        )
        harness = _RecordingCardHarness(client)
        harness._card_snapshots = {"100": {"200": "known-card"}}

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(harness.processed, [])
        self.assertEqual(harness._card_snapshots, {"100": {"200": "known-card"}})

    def test_first_sync_is_baseline_then_change_is_processed(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "first", "nickname": "n"}]},
        )
        harness = _RecordingCardHarness(client)

        asyncio.run(harness._sync_group_cards())
        self.assertEqual(harness.processed, [])
        self.assertEqual(harness._card_snapshots["100"]["200"], "first")

        client.member_result = {
            "data": [{"user_id": 200, "card": "second", "nickname": "n"}]
        }
        asyncio.run(harness._sync_group_cards())

        self.assertEqual(len(harness.processed), 1)
        self.assertEqual(harness.processed[0][0][2:4], ("first", "second"))

    def test_first_sync_enforces_existing_protected_member(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "wrong", "nickname": "n"}]},
        )
        harness = _ProtectedJoinHarness()
        harness.client = client

        changed = asyncio.run(harness._sync_group_cards())

        self.assertEqual(changed, 1)
        self.assertEqual(harness.restores, [("100", "200", "required-card")])
        self.assertEqual(harness._card_snapshots["100"]["200"], "required-card")

    def test_new_protection_is_enforced_without_card_change(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "wrong", "nickname": "n"}]},
        )
        harness = _ProtectedJoinHarness()
        harness.client = client
        harness._storage = _Storage()

        asyncio.run(harness._sync_group_cards())
        harness._storage = _ProtectedStorage()
        changed = asyncio.run(harness._sync_group_cards())

        self.assertEqual(changed, 1)
        self.assertEqual(harness.restores, [("100", "200", "required-card")])
        self.assertEqual(harness._card_snapshots["100"]["200"], "required-card")

    def test_disabled_plugin_skips_join_card_query(self):
        client = _MemberInfoClient([
            {"status": "ok", "retcode": 0, "data": {"card": "bad", "nickname": "n"}},
        ])
        harness = _CardHarness(client)
        harness.cfg_values["enabled"] = False
        event = _Event({
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 200,
        })

        result = asyncio.run(harness._handle_group_increase(event))

        self.assertFalse(result)
        self.assertEqual(client.calls, 0)

    def test_group_enabled_override_allows_join_card_query(self):
        client = _MemberInfoClient([
            {"status": "ok", "retcode": 0, "data": {"card": "normal", "nickname": "n"}},
        ])
        harness = _CardHarness(client)
        harness.cfg_values["enabled"] = False
        harness.group_cfg_values[("100", "enabled")] = True
        event = _Event({
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 200,
        })

        asyncio.run(harness._handle_group_increase(event))

        self.assertEqual(client.calls, 1)

    def test_unaccepted_disclaimer_skips_join_card_query(self):
        client = _MemberInfoClient([
            {"status": "ok", "retcode": 0, "data": {"card": "bad", "nickname": "n"}},
        ])
        harness = _CardHarness(client)
        harness.config["disclaimer_agreed"] = False
        event = _Event({
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 200,
        })

        asyncio.run(harness._handle_group_increase(event))

        self.assertEqual(client.calls, 0)

    def test_pending_join_forces_audit_even_with_stale_equal_snapshot(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "same", "nickname": "n"}]},
        )
        harness = _RecordingCardHarness(client)
        harness._card_snapshots = {"100": {"200": "same"}}
        harness._card_pending_members = {("100", "200")}

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(len(harness.processed), 1)
        args, kwargs = harness.processed[0]
        self.assertEqual(args[2:4], ("", "same"))
        self.assertTrue(kwargs["force"])
        self.assertNotIn(("100", "200"), harness._card_pending_members)

    def test_successful_sync_retries_then_drops_pending_member_not_in_list(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 201, "card": "present", "nickname": "n"}]},
        )
        harness = _RecordingCardHarness(client)
        harness._card_pending_members = {("100", "200")}

        asyncio.run(harness._sync_group_cards())
        self.assertIn(("100", "200"), harness._card_pending_members)
        asyncio.run(harness._sync_group_cards())
        self.assertIn(("100", "200"), harness._card_pending_members)
        asyncio.run(harness._sync_group_cards())

        self.assertNotIn(("100", "200"), harness._card_pending_members)

    def test_poll_response_does_not_override_newer_event_snapshot(self):
        harness = _RecordingCardHarness()
        harness._card_snapshots = {"100": {"200": "old"}}
        client = _SnapshotRaceClient(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "stale-poll", "nickname": "n"}]},
            lambda: harness._remember_card_snapshot("100", "200", "event-new"),
        )
        harness.client = client

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(harness.processed, [])
        self.assertEqual(harness._card_snapshots["100"]["200"], "event-new")

    def test_pending_poll_does_not_override_newer_event_snapshot(self):
        harness = _RecordingCardHarness()
        harness._card_pending_members = {("100", "200")}
        client = _SnapshotRaceClient(
            {"data": [{"group_id": 100}]},
            {"data": [{"user_id": 200, "card": "stale-poll", "nickname": "n"}]},
            lambda: harness._remember_card_snapshot("100", "200", "event-new"),
        )
        harness.client = client

        asyncio.run(harness._sync_group_cards())

        self.assertEqual(harness.processed, [])
        self.assertEqual(harness._card_snapshots["100"]["200"], "event-new")
        self.assertNotIn(("100", "200"), harness._card_pending_members)

    def test_failed_restore_keeps_target_snapshot_for_polling_retry(self):
        harness = _RestoreFailureHarness()

        result = asyncio.run(
            harness._process_card_values(
                "100", "200", "safe", "https://bad.example", source="event"
            )
        )

        self.assertFalse(result)
        self.assertEqual(harness._card_snapshots["100"]["200"], "safe")
        self.assertEqual(harness.logged[0][-1], "违规还原失败")

    def test_failed_restore_notifies_admin(self):
        class _H(_RestoreFailureHarness):
            def __init__(self):
                super().__init__()
                self.notices = []

            async def _notify_card_group(self, group_id, text):
                self.notices.append((group_id, text))

        harness = _H()
        asyncio.run(
            harness._process_card_values(
                "100", "200", "safe", "https://bad.example", source="event"
            )
        )
        self.assertTrue(harness.notices)
        self.assertEqual("100", harness.notices[0][0])
        self.assertIn("失败", harness.notices[0][1])
        self.assertIn("请检查", harness.notices[0][1])

    def test_empty_join_card_still_enforces_protected_value(self):
        harness = _ProtectedJoinHarness()

        restored = asyncio.run(
            harness._process_card_values(
                "100", "200", "", "", source="join", force=True
            )
        )

        self.assertTrue(restored)
        self.assertEqual(harness.restores, [("100", "200", "required-card")])
        self.assertEqual(harness._card_snapshots["100"]["200"], "required-card")

    def test_invalid_llm_json_uses_local_card_hits(self):
        harness = _CardLlmHarness('{"reason": "missing violation"}')

        hit_result = asyncio.run(
            harness._card_llm_violation("100", "200", "suspect", {"swear": True})
        )
        no_hit_result = asyncio.run(
            harness._card_llm_violation("100", "200", "ordinary", {})
        )

        self.assertTrue(hit_result)
        self.assertFalse(no_hit_result)

    def test_sync_reuses_owner_role_for_card_audit_exemption(self):
        client = _Client(
            {"data": [{"group_id": 100}]},
            {"data": [{
                "user_id": 200,
                "card": "https://new.example",
                "nickname": "owner",
                "role": "owner",
            }]},
        )
        harness = _CardAuditAdminHarness(client)
        harness._card_snapshots = {"100": {"200": "old-card"}}

        changed = asyncio.run(harness._sync_group_cards())

        self.assertEqual(changed, 0)
        self.assertEqual(harness.restores, [])
        self.assertEqual(harness.role_calls, [])
        self.assertEqual(harness._card_snapshots["100"]["200"], "https://new.example")
        self.assertEqual(harness.logged[0][-1], "记录")

    def test_event_queries_group_admin_role_for_card_audit_exemption(self):
        harness = _CardAuditAdminHarness(roles={("100", "201"): "admin"})

        restored = asyncio.run(
            harness._process_card_values(
                "100", "201", "old-card", "https://new.example", source="event"
            )
        )

        self.assertFalse(restored)
        self.assertEqual(harness.restores, [])
        self.assertEqual(harness.role_calls, [("100", "201")])
        self.assertEqual(harness.logged[0][-1], "记录")

    def test_plugin_admin_is_exempt_without_role_lookup(self):
        harness = _CardAuditAdminHarness(admin_ids={"202"})

        restored = asyncio.run(
            harness._process_card_values(
                "100", "202", "old-card", "https://new.example", source="event"
            )
        )

        self.assertFalse(restored)
        self.assertEqual(harness.restores, [])
        self.assertEqual(harness.role_calls, [])
        self.assertEqual(harness.logged[0][-1], "记录")

    def test_ordinary_member_is_still_audited(self):
        harness = _CardAuditAdminHarness()

        restored = asyncio.run(
            harness._process_card_values(
                "100", "203", "old-card", "https://new.example",
                source="sync", member_role="member",
            )
        )

        self.assertTrue(restored)
        self.assertEqual(harness.restores, [("100", "203", "old-card")])
        self.assertEqual(harness.role_calls, [])
        self.assertEqual(harness.logged[0][-1], "违规还原")

    def test_admin_exemption_can_be_disabled(self):
        harness = _CardAuditAdminHarness(admin_exempt=False)

        restored = asyncio.run(
            harness._process_card_values(
                "100", "204", "old-card", "https://new.example",
                source="sync", member_role="owner",
            )
        )

        self.assertTrue(restored)
        self.assertEqual(harness.restores, [("100", "204", "old-card")])
        self.assertEqual(harness.role_calls, [])
        self.assertEqual(harness.logged[0][-1], "违规还原")

    def test_protected_admin_card_is_still_forcibly_restored(self):
        harness = _CardAuditAdminHarness(admin_ids={"200"}, protected=True)

        restored = asyncio.run(
            harness._process_card_values(
                "100", "200", "old-card", "https://new.example",
                source="sync", member_role="owner",
            )
        )

        self.assertTrue(restored)
        self.assertEqual(harness.restores, [("100", "200", "required-card")])
        self.assertEqual(harness.role_calls, [])
        self.assertEqual(harness.logged[0][-1], "保护还原")
        self.assertEqual(harness._card_snapshots["100"]["200"], "required-card")


class _SchedulerStorage:
    @staticmethod
    def list_configured_groups():
        return ["100"]


class _SchedulerHarness(scheduler.SchedulerMixin):
    def __init__(self):
        self.config = {"disclaimer_agreed": True}
        self._storage = _SchedulerStorage()
        self._group_white_set = set()
        self._card_sync_known_groups = set()

    @staticmethod
    def _cfg(key, default=True, group_id=None):
        if group_id == "100" and key in ("enabled", "card_sync_enabled"):
            return True
        values = {
            "enabled": False,
            "card_monitor_enabled": True,
            "card_sync_enabled": False,
        }
        return values.get(key, default)


class SchedulerTests(unittest.TestCase):
    def test_single_group_sync_override_survives_global_disable(self):
        self.assertTrue(_SchedulerHarness()._card_sync_any_group_enabled())

    def test_card_sync_requires_disclaimer(self):
        harness = _SchedulerHarness()
        harness.config["disclaimer_agreed"] = False

        self.assertFalse(harness._card_sync_any_group_enabled())


class WebConfigSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((ROOT / "web.py").read_text(encoding="utf-8"))
        cls.web_class = next(
            node for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WebMixin"
        )

    def _class_literal(self, name):
        assignment = next(
            node for node in self.web_class.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        )
        return ast.literal_eval(assignment.value)

    def test_card_sync_settings_are_exposed_but_interval_is_global(self):
        keys = self._class_literal("_CARD_MONITOR_KEYS")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")

        self.assertIn("card_sync_enabled", keys)
        self.assertIn("card_sync_interval", keys)
        self.assertNotIn("card_sync_enabled", excluded)
        self.assertIn("card_sync_interval", excluded)

        dashboard = (ROOT / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("'card_sync_enabled','card_sync_interval'", dashboard)
        self.assertIn("data-card-type=\"int\"", dashboard)


if __name__ == "__main__":
    unittest.main()
