"""AI 特殊检测（特殊数字）WebUI 配置门禁的行为回归测试。

覆盖：
- 全局总开关/检测器 false→true 激活必须在同一次 payload 携带三项确认；
- 已启用状态下未变更的保存可沿用已存储的完整确认；
- 已启用但确认不完整时拒绝保存；
- 显式关闭总开关时无条件撤销三项确认（即使 payload 携带旧确认）；
- 持久化失败时回滚（含确认撤销）；
- 无关配置保存的原子性（拒绝时不产生任何部分写入）；
- 按群设置的启用开关仅在「由未启用变为启用」时需要全局完整确认、确认项不可按群覆盖；
- 删除/清空群覆盖仅在继承后会使该群启用时才拦截；
- 复制时启用需全局确认，且三项确认永不复制。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "group_guardian_ai_relay_config_tests"

ACK_TEXT = "我确认启用AI特殊检测并承担误撤回风险"

_SCHEMA = {
    "ai_relay_mode_enabled": {"type": "bool", "default": False, "description": "AI特殊检测总开关"},
    "ai_relay_zero_guard_enabled": {"type": "bool", "default": False, "description": "特殊数字检测"},
    "ai_relay_ack_recall": {"type": "bool", "default": False},
    "ai_relay_ack_false_positive": {"type": "bool", "default": False},
    "ai_relay_risk_ack": {"type": "string", "default": ""},
    "scan_swear": {"type": "bool", "default": True},
}


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
ai_relay = _load_module(f"{PACKAGE}.ai_relay", "ai_relay.py")
web = _load_module(f"{PACKAGE}.web", "web.py")


class _Request:
    def __init__(self, args=None, body=None):
        self.args = dict(args or {})
        self.body = body or {}

    async def get_json(self, **_kwargs):
        return self.body


class _Storage:
    def __init__(self, groups=None):
        self.groups = {str(k): dict(v) for k, v in (groups or {}).items()}

    def get_group_configs(self, group_id):
        return dict(self.groups.get(str(group_id), {}))

    def set_group_config(self, group_id, key, value):
        self.groups.setdefault(str(group_id), {})[key] = value

    def delete_group_config(self, group_id, key):
        self.groups.get(str(group_id), {}).pop(key, None)

    def clear_group_configs(self, group_id):
        return len(self.groups.pop(str(group_id), {}) or {})

    def list_configured_groups(self):
        return list(self.groups)


class _Harness(web.WebMixin, utils.UtilitiesMixin):
    def __init__(self, config=None, *, save_ok=True, groups=None):
        self._config_schema = {k: dict(v) for k, v in _SCHEMA.items()}
        self.config = dict(config or {})
        self._storage = _Storage(groups)
        self._save_ok = save_ok
        self._group_cfg_cache = {}
        self._group_white_set = set()

    def _save_config_safe(self):
        return self._save_ok


def _proof_payload():
    return {
        "ai_relay_ack_recall": True,
        "ai_relay_ack_false_positive": True,
        "ai_relay_risk_ack": ACK_TEXT,
    }


def _enabled_config(**extra):
    config = {
        "ai_relay_mode_enabled": True,
        "ai_relay_zero_guard_enabled": True,
        "ai_relay_ack_recall": True,
        "ai_relay_ack_false_positive": True,
        "ai_relay_risk_ack": ACK_TEXT,
    }
    config.update(extra)
    return config


class AiRelayConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        web.jsonify = lambda payload: payload
        web.quart_request = _Request()

    # ---------- 全局激活 ----------

    async def test_activation_with_full_proof_in_same_payload_succeeds(self):
        h = _Harness({"ai_relay_mode_enabled": False, "ai_relay_zero_guard_enabled": False})
        body = {"ai_relay_mode_enabled": True}
        body.update(_proof_payload())
        web.quart_request = _Request(body=body)
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertTrue(h.config["ai_relay_mode_enabled"])
        self.assertTrue(ai_relay.ai_relay_risk_acknowledged(h.config))

    async def test_detector_activation_with_full_proof_succeeds(self):
        h = _Harness({"ai_relay_mode_enabled": True, "ai_relay_zero_guard_enabled": False})
        body = {"ai_relay_zero_guard_enabled": True}
        body.update(_proof_payload())
        web.quart_request = _Request(body=body)
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertTrue(h.config["ai_relay_zero_guard_enabled"])

    async def test_activation_without_proof_is_rejected(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        web.quart_request = _Request(body={"ai_relay_mode_enabled": True})
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertFalse(h.config["ai_relay_mode_enabled"])

    async def test_activation_with_incomplete_proof_is_rejected(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        web.quart_request = _Request(body={
            "ai_relay_mode_enabled": True,
            "ai_relay_ack_recall": True,
        })
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertFalse(h.config["ai_relay_mode_enabled"])
        self.assertFalse(bool(h.config.get("ai_relay_ack_recall")))

    async def test_activation_with_wrong_phrase_is_rejected(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        body = {"ai_relay_mode_enabled": True}
        body.update(_proof_payload())
        body["ai_relay_risk_ack"] = "我确认"
        web.quart_request = _Request(body=body)
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertFalse(h.config["ai_relay_mode_enabled"])

    # ---------- 已启用状态下的保存 ----------

    async def test_unchanged_enabled_save_uses_stored_proof(self):
        h = _Harness(_enabled_config(scan_swear=True))
        web.quart_request = _Request(body={"scan_swear": False})
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertFalse(h.config["scan_swear"])
        self.assertEqual(h.config["ai_relay_risk_ack"], ACK_TEXT)

    async def test_enabled_with_incomplete_stored_proof_rejects_unrelated_save(self):
        h = _Harness({
            "ai_relay_mode_enabled": True,
            "ai_relay_zero_guard_enabled": True,
            "scan_swear": True,
        })
        web.quart_request = _Request(body={"scan_swear": False})
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertTrue(h.config["scan_swear"])

    # ---------- 显式关闭总开关：撤销确认 ----------

    async def test_explicit_master_false_revokes_proofs_even_with_stale_resubmit(self):
        h = _Harness(_enabled_config())
        body = {"ai_relay_mode_enabled": False}
        body.update(_proof_payload())
        web.quart_request = _Request(body=body)
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertFalse(h.config["ai_relay_mode_enabled"])
        self.assertFalse(bool(h.config.get("ai_relay_ack_recall")))
        self.assertFalse(bool(h.config.get("ai_relay_ack_false_positive")))
        self.assertEqual(h.config.get("ai_relay_risk_ack"), "")
        self.assertFalse(ai_relay.ai_relay_risk_acknowledged(h.config))

    async def test_activation_rejects_noncanonical_confirmation_flags(self):
        for value in ("y", "t", "是"):
            with self.subTest(value=value):
                h = _Harness({"ai_relay_mode_enabled": False})
                body = {"ai_relay_mode_enabled": True}
                body.update(_proof_payload())
                body["ai_relay_ack_recall"] = value
                web.quart_request = _Request(body=body)
                resp = await h._web_update_config()
                self.assertEqual(resp["status"], "error")
                self.assertFalse(h.config["ai_relay_mode_enabled"])

    async def test_explicit_master_false_permits_disable_with_stale_detector_true(self):
        # 陈旧前端可能在关闭总开关的同一次 payload 里带上 detector=true；必须放行并撤销确认。
        h = _Harness({
            "ai_relay_mode_enabled": True,
            "ai_relay_zero_guard_enabled": False,
            "ai_relay_ack_recall": True,
            "ai_relay_ack_false_positive": True,
            "ai_relay_risk_ack": ACK_TEXT,
        })
        web.quart_request = _Request(body={
            "ai_relay_mode_enabled": False,
            "ai_relay_zero_guard_enabled": True,
        })
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertFalse(h.config["ai_relay_mode_enabled"])
        self.assertFalse(ai_relay.ai_relay_risk_acknowledged(h.config))

    async def test_master_false_revocation_rolls_back_on_save_failure(self):
        h = _Harness(_enabled_config(), save_ok=False)
        body = {"ai_relay_mode_enabled": False}
        body.update(_proof_payload())
        web.quart_request = _Request(body=body)
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertTrue(h.config["ai_relay_mode_enabled"])
        self.assertEqual(h.config["ai_relay_risk_ack"], ACK_TEXT)
        self.assertTrue(ai_relay.ai_relay_risk_acknowledged(h.config))

    # ---------- 无关保存与原子性 ----------

    async def test_unrelated_update_when_disabled_needs_no_proof(self):
        h = _Harness({"ai_relay_mode_enabled": False, "scan_swear": True})
        web.quart_request = _Request(body={"scan_swear": False})
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "success")
        self.assertFalse(h.config["scan_swear"])

    async def test_rejection_is_atomic_no_partial_mutation(self):
        h = _Harness({"ai_relay_mode_enabled": False, "scan_swear": True})
        web.quart_request = _Request(body={
            "scan_swear": False,
            "ai_relay_mode_enabled": True,
            "ai_relay_ack_recall": True,
        })
        resp = await h._web_update_config()
        self.assertEqual(resp["status"], "error")
        self.assertTrue(h.config["scan_swear"])
        self.assertFalse(h.config["ai_relay_mode_enabled"])
        self.assertFalse(bool(h.config.get("ai_relay_ack_recall")))

    # ---------- 按群覆盖：仅「由未启用变为启用」时才要求全局确认 ----------

    async def test_group_set_enable_toggle_requires_global_proof_when_it_activates(self):
        h = _Harness({"ai_relay_mode_enabled": False}, groups={
            "123": {"ai_relay_zero_guard_enabled": "true"},
        })
        web.quart_request = _Request(body={
            "group_id": "123", "key": "ai_relay_mode_enabled", "value": True,
        })
        resp = await h._web_set_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(
            h._storage.get_group_configs("123"),
            {"ai_relay_zero_guard_enabled": "true"},
        )

    async def test_group_set_enable_toggle_allowed_with_global_proof(self):
        h = _Harness(_enabled_config(), groups={
            "123": {"ai_relay_zero_guard_enabled": "true"},
        })
        web.quart_request = _Request(body={
            "group_id": "123", "key": "ai_relay_mode_enabled", "value": True,
        })
        resp = await h._web_set_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123")["ai_relay_mode_enabled"], "true")

    async def test_group_set_single_toggle_without_activating_needs_no_proof(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        web.quart_request = _Request(body={
            "group_id": "123", "key": "ai_relay_mode_enabled", "value": True,
        })
        resp = await h._web_set_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123")["ai_relay_mode_enabled"], "true")

    async def test_group_set_proof_key_is_rejected(self):
        h = _Harness(_enabled_config())
        web.quart_request = _Request(body={
            "group_id": "123", "key": "ai_relay_risk_ack", "value": ACK_TEXT,
        })
        resp = await h._web_set_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_batch_activation_requires_global_proof(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        web.quart_request = _Request(body={
            "group_id": "123",
            "items": {"ai_relay_mode_enabled": True, "ai_relay_zero_guard_enabled": True,
                      "scan_swear": False},
        })
        resp = await h._web_batch_set_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_batch_activation_allowed_with_global_proof(self):
        h = _Harness(_enabled_config())
        web.quart_request = _Request(body={
            "group_id": "123",
            "items": {"ai_relay_mode_enabled": True, "ai_relay_zero_guard_enabled": True,
                      "scan_swear": False},
        })
        resp = await h._web_batch_set_group_config()
        self.assertEqual(resp["status"], "success")
        stored = h._storage.get_group_configs("123")
        self.assertEqual(stored["ai_relay_mode_enabled"], "true")
        self.assertEqual(stored["ai_relay_zero_guard_enabled"], "true")
        self.assertEqual(stored["scan_swear"], "false")

    async def test_group_batch_non_activating_needs_no_proof(self):
        h = _Harness({"ai_relay_mode_enabled": False})
        web.quart_request = _Request(body={
            "group_id": "123",
            "items": {"ai_relay_mode_enabled": True, "scan_swear": False},
        })
        resp = await h._web_batch_set_group_config()
        self.assertEqual(resp["status"], "success")
        stored = h._storage.get_group_configs("123")
        self.assertEqual(stored["ai_relay_mode_enabled"], "true")
        self.assertEqual(stored["scan_swear"], "false")

    # ---------- 继承：删除/清空仅在「继承后启用」时才拦截 ----------

    async def test_group_delete_true_override_while_global_off_is_allowed(self):
        # 删除 true 覆盖、全局关闭 = 由启用变为未启用，属于收紧，不应拦截。
        h = _Harness({"ai_relay_mode_enabled": False, "ai_relay_zero_guard_enabled": False}, groups={
            "123": {"ai_relay_mode_enabled": "true"},
        })
        web.quart_request = _Request(body={"group_id": "123", "key": "ai_relay_mode_enabled"})
        resp = await h._web_delete_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_delete_off_override_while_global_off_is_allowed(self):
        h = _Harness({"ai_relay_mode_enabled": False, "ai_relay_zero_guard_enabled": False}, groups={
            "123": {"ai_relay_mode_enabled": "false"},
        })
        web.quart_request = _Request(body={"group_id": "123", "key": "ai_relay_mode_enabled"})
        resp = await h._web_delete_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_delete_inheritance_activation_requires_global_proof(self):
        # 全局两项已开但确认不完整；删除群的 false 覆盖会让该群继承启用 -> 需全局确认。
        h = _Harness({
            "ai_relay_mode_enabled": True,
            "ai_relay_zero_guard_enabled": True,
        }, groups={"123": {"ai_relay_mode_enabled": "false"}})
        web.quart_request = _Request(body={"group_id": "123", "key": "ai_relay_mode_enabled"})
        resp = await h._web_delete_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(h._storage.get_group_configs("123"), {"ai_relay_mode_enabled": "false"})

    async def test_group_delete_inheritance_activation_allowed_with_global_proof(self):
        h = _Harness(_enabled_config(), groups={"123": {"ai_relay_mode_enabled": "false"}})
        web.quart_request = _Request(body={"group_id": "123", "key": "ai_relay_mode_enabled"})
        resp = await h._web_delete_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_clear_inheritance_activation_requires_global_proof(self):
        h = _Harness({
            "ai_relay_mode_enabled": True,
            "ai_relay_zero_guard_enabled": True,
        }, groups={"123": {"ai_relay_mode_enabled": "false", "scan_swear": "false"}})
        web.quart_request = _Request(body={"group_id": "123"})
        resp = await h._web_clear_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(
            h._storage.get_group_configs("123"),
            {"ai_relay_mode_enabled": "false", "scan_swear": "false"},
        )

    async def test_group_clear_inheritance_activation_allowed_with_global_proof(self):
        h = _Harness(_enabled_config(), groups={
            "123": {"ai_relay_mode_enabled": "false", "scan_swear": "false"},
        })
        web.quart_request = _Request(body={"group_id": "123"})
        resp = await h._web_clear_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    async def test_group_clear_true_overrides_while_global_off_is_allowed(self):
        h = _Harness({"ai_relay_mode_enabled": False, "ai_relay_zero_guard_enabled": False}, groups={
            "123": {"ai_relay_mode_enabled": "true", "scan_swear": "false"},
        })
        web.quart_request = _Request(body={"group_id": "123"})
        resp = await h._web_clear_group_config()
        self.assertEqual(resp["status"], "success")
        self.assertEqual(h._storage.get_group_configs("123"), {})

    # ---------- 复制：启用需全局确认，确认项永不复制 ----------

    async def test_group_copy_activation_requires_global_proof(self):
        h = _Harness({"ai_relay_mode_enabled": False}, groups={
            "100": {"ai_relay_mode_enabled": "true", "ai_relay_zero_guard_enabled": "true"},
        })
        web.quart_request = _Request(body={"source_group_id": "100", "target_group_id": "200"})
        resp = await h._web_copy_group_config()
        self.assertEqual(resp["status"], "error")
        self.assertEqual(h._storage.get_group_configs("200"), {})

    async def test_group_copy_activation_allowed_with_global_proof_and_never_copies_proofs(self):
        h = _Harness(_enabled_config(), groups={
            "100": {
                "ai_relay_mode_enabled": "true",
                "ai_relay_zero_guard_enabled": "true",
                "scan_swear": "false",
                "ai_relay_ack_recall": "true",
                "ai_relay_ack_false_positive": "true",
                "ai_relay_risk_ack": ACK_TEXT,
            },
        })
        web.quart_request = _Request(body={"source_group_id": "100", "target_group_id": "200"})
        resp = await h._web_copy_group_config()
        self.assertEqual(resp["status"], "success")
        stored = h._storage.get_group_configs("200")
        self.assertEqual(stored.get("ai_relay_mode_enabled"), "true")
        self.assertEqual(stored.get("ai_relay_zero_guard_enabled"), "true")
        self.assertEqual(stored.get("scan_swear"), "false")
        for key in ("ai_relay_ack_recall", "ai_relay_ack_false_positive", "ai_relay_risk_ack"):
            self.assertNotIn(key, stored)


if __name__ == "__main__":
    unittest.main()
