"""v2.19.0 WebUI 远程操作安全增强回归测试。

覆盖：
- storage.py：pending_web_operations 审批 CRUD（创建 / 列出 / CAS 确认 / 驳回 / 过期 / 执行标记）
- storage.py：web_audit_logs 新字段（operator_ip / before_value / after_value）落库与幂等迁移
- remote.py：_remote_execute 审计透传操作人 IP 与修改前后值
- web.py / _conf_schema.json：审批路由、_request_ip、双审批集成、新配置项静态检查

无需安装 AstrBot：顶层 astrbot 包用 shim 代替。
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


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


def _load_module(filename, alias):
    path = _ROOT / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[alias] = module
    return module


def _load_storage():
    _stub_astrbot()
    if "lexicon_migration" not in sys.modules:
        _load_module("lexicon_migration.py", "lexicon_migration")
    if "storage_group" not in sys.modules:
        _load_module("storage_group.py", "storage_group")
    if "gg_storage" not in sys.modules:
        _load_module("storage.py", "gg_storage")
    return sys.modules["gg_storage"]


def _load_remote():
    _stub_astrbot()
    path = _ROOT / "remote.py"
    spec = importlib.util.spec_from_file_location("gg_remote_mixin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(name):
    return (_ROOT / name).read_text(encoding="utf-8")


def _run(coro):
    import asyncio

    try:
    def test_create_and_list_pending(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(
            operator_name="甲", operator_qq="10001", operator_ip="1.2.3.4",
            group_id="100", action="whole_ban", params='{"enable":true}',
        )
        self.assertGreater(op_id, 0)
        lst = st.list_pending_web_operations()
        self.assertEqual(1, len(lst))
        item = lst[0]
        self.assertEqual("pending", item["status"])
        self.assertEqual("甲", item["operator_name"])
        self.assertEqual("10001", item["operator_qq"])
        self.assertEqual("1.2.3.4", item["operator_ip"])
        self.assertEqual("100", item["group_id"])
        self.assertEqual("whole_ban", item["action"])
        self.assertGreater(item["expire_at"], item["ts"])

    def test_approve_cas_single_winner(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="set_admin")
        self.assertTrue(st.approve_pending_web_operation(op_id, "乙", "10002", "5.6.7.8"))
        # 第二次确认（含并发/重复点击）必须失败
        self.assertFalse(st.approve_pending_web_operation(op_id, "丙", "10003", "9.9.9.9"))
        got = st.get_pending_web_operation(op_id)
        self.assertEqual("approved", got["status"])
        self.assertEqual("乙", got["approver_name"])
        self.assertEqual("10002", got["approver_qq"])
        self.assertEqual("5.6.7.8", got["approver_ip"])

    def test_reject_then_approve_fails(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="whole_ban")
        self.assertTrue(st.reject_pending_web_operation(op_id))
        self.assertFalse(st.approve_pending_web_operation(op_id, "乙", "10002"))
        self.assertEqual("rejected", st.get_pending_web_operation(op_id)["status"])

    def test_expire_removes_from_list_and_blocks_approve(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="kick")
        # 直接改写 expire_at 为过去时间，模拟超时
        with st._connect() as conn:
            conn.execute("UPDATE pending_web_operations SET expire_at=1 WHERE id=?", (op_id,))
            conn.commit()
        self.assertEqual([], st.list_pending_web_operations())
        self.assertFalse(st.approve_pending_web_operation(op_id, "乙", "10002"))
        st.expire_pending_web_operations()
        self.assertEqual("expired", st.get_pending_web_operation(op_id)["status"])

    def test_mark_executed(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="set_admin")
        st.mark_pending_web_executed(op_id)
        self.assertEqual(1, st.get_pending_web_operation(op_id)["executed"])

    def test_audit_logs_store_ip_and_before_after(self):
        st = self.make_storage()
        st.record_web_audit(
            operator_name="乙", operator_qq="10002", group_id="100",
            action="set_admin", target_user="123", params='{"user_id":"123"}',
            result="成功", message="设置管理员 成功 1/1",
            operator_ip="203.0.113.9", before_value="member", after_value="admin",
        )
        logs = st.list_web_audit_logs()
        self.assertEqual(1, len(logs))
        item = logs[0]
        self.assertEqual("203.0.113.9", item["operator_ip"])
        self.assertEqual("member", item["before_value"])
        self.assertEqual("admin", item["after_value"])
        self.assertTrue(item["time"])

    def test_migration_is_idempotent(self):

class RemoteAuditFieldTests(unittest.TestCase):
    """_remote_execute 审计透传操作人 IP 与修改前后值。"""

    @classmethod
    def setUpClass(cls):
        remote_module = _load_remote()
        cls.RemoteMixin = remote_module.RemoteMixin

        class _AuditStorage:
            def __init__(self):
                self.records = []

            def record_web_audit(self, **kwargs):
                self.records.append(kwargs)

            def is_group_super_admin(self, group_id, user_id):
                return False

        class _Harness:
            def __init__(self, cfg=None, roles=None):
                self._cfg_values = cfg or {}
                self._roles = roles or {}
                self._storage = _AuditStorage()
                self._client = types.SimpleNamespace()
                self._group_black_set = set()
                self._group_white_set = set()

            def _cfg(self, key, default=True, group_id=None):
                return self._cfg_values.get(key, default)

            def _cfg_str(self, key, default="", group_id=None):
                return default

            def _cfg_check(self, cfg_key, cn_name, group_id=None):
                return True, ""

            def _get_all_admin_ids(self):
                return set()

            async def _get_client(self, _platform=None):
                return self._client

            async def _get_role_by_id(self, client, group_id, user_id):
                return self._roles.get(str(user_id), "member")

            async def _remote_execute_single(self, client, gid, action, user_id, params):
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

        cls.Harness = type("Harness", (_Harness, cls.RemoteMixin), {})

    def make(self, **kw):
        return self.Harness(**kw)

    def test_audit_records_operator_ip(self):
        h = self.make()
        r = _run(h._remote_execute("100", "kick", {"user_id": "5"}, operator_qq="10001",
                                   operator_name="管理员", operator_ip="203.0.113.7"))
        self.assertTrue(r.get("ok"))
        rec = h._storage.records[-1]
        self.assertEqual("203.0.113.7", rec["operator_ip"])
        self.assertEqual("管理员", rec["operator_name"])

    def test_audit_records_before_after_for_set_admin(self):
        h = self.make(roles={"123": "owner"})
        r = _run(h._remote_execute("100", "set_admin", {"user_id": "123"},
                                   operator_qq="10001", operator_ip="10.0.0.1"))
        self.assertTrue(r.get("ok"))
        rec = h._storage.records[-1]
        self.assertIn("owner", str(rec["before_value"]))    # 修改前角色
        self.assertIn("设置管理员", str(rec["after_value"]))  # 修改后摘要

    def test_audit_rejected_path_keeps_ip(self):
        h = self.make()
        r = _run(h._remote_execute("100", "kick", {"user_id": "5"}, operator_qq="99999",
                                   operator_ip="8.8.4.4"))
        self.assertFalse(r.get("ok"))
        rec = h._storage.records[-1]
        self.assertEqual("拒绝", rec["result"])
        self.assertEqual("8.8.4.4", rec["operator_ip"])

        st = self.make_storage()
        # 重复建表/补列不应报错（旧库升级场景）
        with st._connect() as conn:
            st._create_tables(conn)
        with st._connect() as conn:

class WebStaticTests(unittest.TestCase):
    """web.py / _conf_schema.json / 前端静态结构检查。"""

    def setUp(self):
        self.web_src = _read_source("web.py")

    def test_web_registers_approval_routes(self):
        for route in ('"/approvals/list"', '"/approvals/approve"', '"/approvals/reject"'):
            self.assertIn(route, self.web_src)
        for handler in ("def _web_approvals_list", "def _web_approvals_approve", "def _web_approvals_reject"):
            self.assertIn(handler, self.web_src)

    def test_web_has_request_ip_and_approval_helpers(self):
        self.assertIn("def _request_ip", self.web_src)
        self.assertIn("X-Forwarded-For", self.web_src)
        self.assertIn("def _approval_actions", self.web_src)

    def test_web_remote_execute_integrates_dual_approval(self):
        self.assertIn("web_remote_dual_approval_enabled", self.web_src)
        self.assertIn("create_pending_web_operation", self.web_src)
        self.assertIn("operator_ip", self.web_src)

    def test_schema_new_configs(self):
        schema = json.loads(_read_source("_conf_schema.json"))
        self.assertTrue(schema["web_remote_confirm_required"]["default"])
        self.assertFalse(schema["web_remote_dual_approval_enabled"]["default"])
        self.assertEqual("set_admin,unset_admin,whole_ban",
                         schema["web_remote_approval_actions"]["default"])

    def test_frontend_has_confirm_and_approvals_panel(self):
        html = _read_source("pages/dashboard/index.html")
        self.assertIn("highRiskConfirm", html)
        self.assertIn("__hrCk", html)
        self.assertIn("approvals/list", html)
        self.assertIn("approvals/approve", html)
        self.assertIn("approvalList", html)
        self.assertIn("auditLogList", html)
        self.assertIn("web_remote_confirm_required", html)


if __name__ == "__main__":
    unittest.main()

            cols = {r["name"] for r in conn.execute("PRAGMA table_info(web_audit_logs)").fetchall()}
        self.assertTrue({"operator_ip", "before_value", "after_value"} <= cols)


        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except RuntimeError:
        return asyncio.run(coro)


storage_mod = None


class ApprovalStorageTests(unittest.TestCase):
    """真实 SQLiteStorage 的审批表与审计字段落库测试。"""

    @classmethod
    def setUpClass(cls):
        global storage_mod
        storage_mod = _load_storage()
        cls.SQLiteStorage = storage_mod.SQLiteStorage

    def make_storage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = self.SQLiteStorage(Path(tmp.name), str(_ROOT))
        with st._connect() as conn:
            st._create_tables(conn)  # 只建表，跳过 seed 导入，保持测试轻量
        return st
