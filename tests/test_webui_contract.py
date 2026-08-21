"""v2.21.0 重做：WebUI 响应契约 + 服务端身份/审批权限模型回归测试。

覆盖打回审查的验收项：
- WebUI 解包真实成功响应（safeGet 契约，页面不再误读 res.data）
- 权限伪造（前端不再自报操作者用户名，服务端忽略请求体 operator_name/operator_qq）
- 未授权 reject（reject 接口含身份解析 + 授权校验）
- 执行失败可重试（pending_web_operations failed 状态机）
- ad_backend 成功响应统一 {status, data}

无需安装 AstrBot：顶层 astrbot 包用 shim 代替。
"""

import importlib.util
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


def _read_source(name):
    return (_ROOT / name).read_text(encoding="utf-8")


class WebuiContractFrontendTests(unittest.TestCase):
    """Dashboard 前端契约回归（safeGet 解包 + 页面用法 + 不再自报身份）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("pages/dashboard/index.html")

    def test_safeget_unwraps_success_data(self):
        # safeGet/safePost 对 {status:'success', data:X} 解包为 X
        self.assertIn("function unwrapApiResponse", self.src)
        self.assertIn("r.status === 'success'", self.src)
        self.assertIn("hasOwnProperty.call(r, 'data')", self.src)
        self.assertIn("return unwrapApiResponse(r);", self.src)

    def test_approved_pages_use_unwrapped_result(self):
        # 成功响应解包后直接使用返回对象，不再读 res.data（修复 P1 空数据）
        for frag in (
            "renderAdminLists(res || {})",
            "var list = res || [];",
            "var d = s;",
            "var hs = b || [];",
            "var fps = f || [];",
            "var rows = e || [];",
            "Object.keys(c).map",
        ):
            self.assertIn(frag, self.src, frag)
        # 成功路径不应再残留 .data 误读（用精确变量绑定模式，避免误匹配 res.data 等防御式写法）
        for bad in ("var d = s.data", "var rows = e.data", "var fps = f.data"):
            self.assertNotIn(bad, self.src, bad)

    def test_operator_identity_not_collected_from_client(self):
        # P0：前端不再 prompt 收集操作者用户名，请求不再携带 operator_name
        self.assertNotIn("请输入操作者用户名", self.src)
        self.assertNotIn("operator_name: op.name", self.src)

    def test_approvals_failed_badge_rendered(self):
        # failed 状态在待处理列表可见（可重试提示）
        self.assertIn("执行失败·可重试", self.src)


class WebuiContractServerTests(unittest.TestCase):
    """服务端身份 / 审批授权静态契约回归。"""

    @classmethod
    def setUpClass(cls):
        cls.web = _read_source("web.py")
        cls.remote = _read_source("remote.py")

    def test_web_ignores_client_operator_name(self):
        # P0：web.py 不再读取请求体 operator_name / operator_qq
        self.assertNotIn('data.get("operator_name"', self.web)
        self.assertNotIn('data.get("operator_qq"', self.web)
        self.assertIn("_resolve_web_operator", self.web)

    def test_remote_resolve_web_operator_from_config(self):
        # 操作者身份只来自服务端 web_operator_bindings 配置解析
        self.assertIn("def _resolve_web_operator", self.remote)
        self.assertIn("web_operator_bindings", self.remote)

    def test_approvals_create_pre_authorized(self):
        # 创建待审批记录前先完成授权校验（P1）
        seg = self.web[self.web.find("async def _web_remote_execute"):self.web.find("async def _web_audit_logs")]
        self.assertIn("_check_remote_operator", seg)
        self.assertIn("create_pending_web_operation", seg)
        self.assertLess(seg.find("_check_remote_operator"), seg.find("create_pending_web_operation"))

    def test_approvals_reject_authorized(self):
        # reject 接口含身份解析 + 授权校验，不再仅凭 ID（P1）
        seg = self.web[self.web.find("async def _web_approvals_reject"):self.web.find("async def _web_get_super_admins")]
        self.assertIn("_resolve_web_operator", seg)
        self.assertIn("_check_remote_operator", seg)

    def test_approvals_approve_failed_marks_retryable(self):
        # 确认后执行：仅成功标记 executed；失败标记 failed 可重试（P1）
        seg = self.web[self.web.find("async def _web_approvals_approve"):self.web.find("async def _web_approvals_reject")]
        self.assertIn("mark_pending_web_executed", seg)
        self.assertIn("mark_pending_web_failed", seg)
        self.assertLess(seg.find('result.get("ok")'), seg.find("mark_pending_web_executed"))


class ApprovalRetryStorageTests(unittest.TestCase):
    """storage.py 审批状态机：failed 可重试 + 驳回记录操作者。"""

    @classmethod
    def setUpClass(cls):
        cls.SQLiteStorage = _load_storage().SQLiteStorage

    def make_storage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = self.SQLiteStorage(Path(tmp.name), str(_ROOT))
        with st._connect() as conn:
            st._create_tables(conn)
        return st

    def test_execute_failed_marks_failed_and_retryable(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="set_admin")
        self.assertTrue(st.approve_pending_web_operation(op_id, "乙", "10002"))
        st.mark_pending_web_failed(op_id, "网络错误")
        got = st.get_pending_web_operation(op_id)
        self.assertEqual("failed", got["status"])
        self.assertEqual(0, got["executed"])
        self.assertIn("网络错误", got["result"])
        # failed 记录仍在待处理列表（可见、可重试）
        self.assertTrue(any(x["id"] == op_id for x in st.list_pending_web_operations()))
        # 可重试：再次确认成功并标记 executed
        self.assertTrue(st.approve_pending_web_operation(op_id, "丙", "10003"))
        st.mark_pending_web_executed(op_id)
        got2 = st.get_pending_web_operation(op_id)
        self.assertEqual(1, got2["executed"])
        self.assertEqual("", got2["result"])

    def test_reject_records_operator(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="whole_ban")
        self.assertTrue(st.reject_pending_web_operation(op_id, "丙", "10003"))
        got = st.get_pending_web_operation(op_id)
        self.assertEqual("rejected", got["status"])
        self.assertEqual("丙", got["approver_name"])
        self.assertEqual("10003", got["approver_qq"])

    def test_approved_record_not_listed(self):
        st = self.make_storage()
        op_id = st.create_pending_web_operation(operator_name="甲", operator_qq="10001", action="set_admin")
        st.approve_pending_web_operation(op_id, "乙", "10002")
        st.mark_pending_web_executed(op_id)
        self.assertNotIn(op_id, [x["id"] for x in st.list_pending_web_operations()])


class AdBackendContractTests(unittest.TestCase):
    """ad_backend 成功响应契约：统一 {status, data} 且字段与前端一致。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("ad_backend.py")

    def test_stats_fields_match_frontend(self):
        for field in ("today_blocked", "total_blocked", "today_img", "today_video", "total_logs"):
            self.assertIn('"%s"' % field, self.src, field)

    def test_success_responses_use_status_data(self):
        self.assertIn('"status": "success"', self.src)
        self.assertIn('"data":', self.src)


if __name__ == "__main__":
    unittest.main()
