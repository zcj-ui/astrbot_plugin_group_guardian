"""v2.15.0 Web 后台远程操作权限模型、身份绑定与审计日志的回归测试。

覆盖 remote.py 的：
- _resolve_operator_from_bindings（Dashboard 用户名 → QQ 绑定解析）
- _check_remote_operator（全局插件管理员 / 群超管 / 群主 / 群管理员 / 成员拒绝）
- _remote_execute 的授权校验与审计记录（拒绝 / 放行 / 默认兼容）

以及 storage.py / web.py 的静态结构检查（web_audit_logs 表、record_web_audit、/audit_logs 路由）。

无需安装 AstrBot：顶层 astrbot 包用 shim 代替。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _stub_astrbot():
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})


def _load_remote():
    _stub_astrbot()
    path = Path(__file__).resolve().parents[1] / "remote.py"
    spec = importlib.util.spec_from_file_location("gg_remote_mixin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(name):
    return (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")


class _AuditStorage:
    """模拟 storage 的最小审计存储（记录 record_web_audit 调用）。"""

    def __init__(self, super_admins=()):
        self.records = []
        self._super_admins = set(str(x) for x in super_admins)

    def is_group_super_admin(self, group_id, user_id):
        return str(user_id) in self._super_admins

    def record_web_audit(self, **kwargs):
        self.records.append(kwargs)

    def list_web_audit_logs(self, limit=100, group_id=""):
        return list(self.records)


class _Harness(object):
    """为 RemoteMixin 提供最小依赖的测试 harness（基类在 setUpClass 中切换为 RemoteMixin）。"""

    def __init__(self, cfg=None, admin_ids=(), super_admins=(), roles=None, bindings=""):
        self._cfg_values = cfg or {}
        self._bindings = bindings
        self._admin_ids = set(str(x) for x in admin_ids)
        self._roles = roles or {}
        self._client = types.SimpleNamespace()
        self._storage = _AuditStorage(super_admins)
        self._group_black_set = set()
        self._group_white_set = set()
        self.single_results = []

    # ---- RemoteMixin 依赖 ----
    def _cfg(self, key, default=True, group_id=None):
        return self._cfg_values.get(key, default)

    def _cfg_str(self, key, default="", group_id=None):
        if key == "web_operator_bindings":
            return self._bindings
        return default

    def _cfg_check(self, cfg_key, cn_name, group_id=None):
        return True, ""

    def _get_all_admin_ids(self):
        return set(self._admin_ids)

    async def _get_client(self, _platform=None):
        return self._client

    async def _get_role_by_id(self, client, group_id, user_id):
        return self._roles.get(str(user_id), "member")

    async def _remote_execute_single(self, client, gid, action, user_id, params):
        self.single_results.append((gid, action, user_id))
        return True, ""

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    @staticmethod
    def _clamp_int(v, d=0, lo=None, hi=None):
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = d
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _log_moderation(self, *args, **kwargs):
        pass


def _run(coro):
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except RuntimeError:
        return asyncio.run(coro)


remote_module = None


class WebRemoteAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global remote_module
        remote_module = _load_remote()
        cls.RemoteMixin = remote_module.RemoteMixin
        # 动态组合 _Harness + RemoteMixin（_Harness 在前，stub 方法优先于 RemoteMixin 真实实现）
        cls.Harness = type("Harness", (_Harness, remote_module.RemoteMixin), {})

    def make(self, **kw):
        return self.Harness(**kw)

    # ---------- 身份绑定解析 ----------
    def test_bindings_resolve_username_to_qq(self):
        h = self.make(bindings="admin:10001; zhangsan:10002")
        self.assertEqual(("zhangsan", "10002"), h._resolve_operator_from_bindings("zhangsan", ""))
        self.assertEqual(("admin", "10001"), h._resolve_operator_from_bindings("admin", ""))

    def test_bindings_comma_separated(self):
        h = self.make(bindings="admin:10001,zhangsan:10002")
        self.assertEqual(("zhangsan", "10002"), h._resolve_operator_from_bindings("zhangsan", ""))

    def test_bindings_explicit_qq_ignored(self):
        # v2.21.0 安全加固：请求体自报 QQ 一律忽略，操作者身份只来自服务端绑定
        h = self.make(bindings="admin:10001")
        self.assertEqual(("", ""), h._resolve_operator_from_bindings("", "99999"))
        # 绑定用户名解析不受请求体显式 QQ 影响
        self.assertEqual(("admin", "10001"), h._resolve_operator_from_bindings("admin", "99999"))

    def test_bindings_unmapped_username(self):
        h = self.make(bindings="admin:10001")
        self.assertEqual(("nobody", ""), h._resolve_operator_from_bindings("nobody", ""))

    # ---------- 授权校验：角色分级 ----------
    def test_operator_plugin_admin_allowed(self):
        h = self.make(admin_ids=["10001"])
        ok, role, msg = _run(h._check_remote_operator("100", "10001"))
        self.assertTrue(ok)
        self.assertEqual("plugin_admin", role)

    def test_operator_group_super_admin_allowed(self):
        h = self.make(super_admins=["20002"])
        ok, role, msg = _run(h._check_remote_operator("100", "20002"))
        self.assertTrue(ok)
        self.assertEqual("group_super_admin", role)

    def test_operator_owner_allowed(self):
        h = self.make(roles={"30003": "owner"})
        ok, role, msg = _run(h._check_remote_operator("100", "30003"))
        self.assertTrue(ok)
        self.assertEqual("owner", role)

    def test_operator_group_admin_allowed(self):
        h = self.make(roles={"40004": "admin"})
        ok, role, msg = _run(h._check_remote_operator("100", "40004"))
        self.assertTrue(ok)
        self.assertEqual("admin", role)

    def test_operator_member_rejected(self):
        h = self.make(roles={"50005": "member"})
        ok, role, msg = _run(h._check_remote_operator("100", "50005"))
        self.assertFalse(ok)
        self.assertEqual("member", role)
        self.assertIn("权限不足", msg)

    def test_operator_missing_identity_rejected(self):
        h = self.make()
        ok, role, msg = _run(h._check_remote_operator("100", ""))
        self.assertFalse(ok)
        self.assertIn("缺少操作者身份", msg)

    # ---------- _remote_execute：强制校验开关 ----------
    def test_remote_execute_require_operator_rejects_missing_identity(self):
        h = self.make(cfg={"web_remote_require_operator": True})
        result = _run(h._remote_execute("100", "kick", {"user_id": "5"}))
        self.assertFalse(result.get("ok"))
        self.assertIn("缺少操作者身份", result.get("message", ""))
        self.assertEqual([], h.single_results)
        # 拒绝也要审计
        self.assertEqual(1, len(h._storage.records))
        self.assertEqual("拒绝", h._storage.records[0]["result"])

    def test_remote_execute_require_operator_rejects_member(self):
        h = self.make(cfg={"web_remote_require_operator": True}, roles={"50005": "member"})
        result = _run(h._remote_execute("100", "kick", {"user_id": "5"}, operator_qq="50005"))
        self.assertFalse(result.get("ok"))
        self.assertIn("权限不足", result.get("message", ""))
        self.assertEqual([], h.single_results)
        self.assertEqual(1, len(h._storage.records))

    def test_remote_execute_require_operator_allows_plugin_admin(self):
        h = self.make(cfg={"web_remote_require_operator": True}, admin_ids=["10001"])
        result = _run(h._remote_execute("100", "kick", {"user_id": "5"}, operator_qq="10001"))
        self.assertTrue(result.get("ok"))
        self.assertEqual([(100, "kick", "5")], h.single_results)
        # 成功也要审计，记录操作者身份与目标群
        rec = h._storage.records[0]
        self.assertEqual("10001", rec["operator_qq"])
        self.assertEqual("100", rec["group_id"])
        self.assertEqual("kick", rec["action"])
        self.assertEqual("成功", rec["result"])

    def test_remote_execute_legacy_mode_without_operator(self):
        # 默认关闭强制校验：保持旧行为可执行，但审计中操作者字段为空
        h = self.make(cfg={}, admin_ids=["10001"])
        result = _run(h._remote_execute("100", "kick", {"user_id": "5"}))
        self.assertTrue(result.get("ok"))
        self.assertEqual([(100, "kick", "5")], h.single_results)
        self.assertEqual(1, len(h._storage.records))
        self.assertEqual("", h._storage.records[0]["operator_qq"])

    def test_remote_execute_operator_always_validated_when_provided(self):
        # 即使开关关闭，只要提供了操作者身份就校验（越权成员仍被拒）
        h = self.make(cfg={}, roles={"50005": "member"})
        result = _run(h._remote_execute("100", "kick", {"user_id": "5"}, operator_qq="50005"))
        self.assertFalse(result.get("ok"))
        self.assertIn("权限不足", result.get("message", ""))

    def test_remote_execute_owner_allowed(self):
        h = self.make(cfg={"web_remote_require_operator": True}, roles={"30003": "owner"})
        result = _run(h._remote_execute("100", "ban", {"user_id": "5", "duration_minutes": 10}, operator_qq="30003"))
        self.assertTrue(result.get("ok"))
        self.assertEqual([(100, "ban", "5")], h.single_results)


class StorageAuditStructureTests(unittest.TestCase):
    """storage.py / web.py 静态结构检查。"""

    def test_storage_creates_web_audit_logs_table(self):
        src = _read_source("storage.py")
        self.assertIn("CREATE TABLE IF NOT EXISTS web_audit_logs", src)
        self.assertIn("operator_name", src)
        self.assertIn("operator_qq", src)
        self.assertIn("idx_web_audit_ts", src)

    def test_storage_has_audit_methods(self):
        src = _read_source("storage.py")
        self.assertIn("def record_web_audit", src)
        self.assertIn("def list_web_audit_logs", src)

    def test_web_registers_audit_route_and_handler(self):
        src = _read_source("web.py")
        self.assertIn('"/audit_logs"', src)
        self.assertIn("def _web_audit_logs", src)

    def test_web_remote_execute_passes_operator(self):
        src = _read_source("web.py")
        self.assertIn("_resolve_operator_from_bindings", src)
        self.assertIn("operator_qq=operator_qq", src)

    def test_config_schema_has_new_keys(self):
        import json

        schema = json.loads(_read_source("_conf_schema.json"))
        self.assertIn("web_operator_bindings", schema)
        self.assertIn("web_remote_require_operator", schema)
        self.assertFalse(schema["web_remote_require_operator"]["default"])


if __name__ == "__main__":
    unittest.main()
