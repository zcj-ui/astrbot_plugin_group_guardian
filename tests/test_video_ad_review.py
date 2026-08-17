"""v2.23.0 不确定视频广告管理员复核功能测试。

覆盖：
- storage.py：video_ad_reviews 表 CRUD（创建 / 列出 / 查询 / CAS 确认 / 放行）
- video_audit.py：_apply_video_audit 对「疑似广告」识别文本设置复核信号
- ad_backend.py：确认违规（学习指纹）/ 放行 handler 逻辑
- _conf_schema.json：3 个新配置项默认值
- web.py：3 个新路由注册
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub_astrbot():
    if "astrbot.core" in sys.modules:
        return
    if "astrbot" not in sys.modules:
        sys.modules["astrbot"] = types.ModuleType("astrbot")
    api = sys.modules.get("astrbot.api")
    if api is None:
        api = types.ModuleType("astrbot.api")
        sys.modules["astrbot.api"] = api
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    core = types.ModuleType("astrbot.core")
    platform = types.ModuleType("astrbot.core.platform")
    sources = types.ModuleType("astrbot.core.platform.sources")
    aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    event_module = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    event_module.AiocqhttpMessageEvent = object
    sys.modules.update({
        "astrbot": sys.modules["astrbot"],
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.platform": platform,
        "astrbot.core.platform.sources": sources,
        "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
    })


def _load(module_name, filename):
    _stub_astrbot()
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = _load("group_guardian_video_review_storage", "storage.py")


def _new_storage():
    tmp = tempfile.TemporaryDirectory(prefix="gg_video_review_")
    st = storage.SQLiteStorage(Path(tmp.name), str(ROOT))
    with st._connect() as conn:
        storage.SQLiteStorage._create_tables(conn)  # 只建表，跳过 seed 导入
    return st, tmp


class StorageReviewTests(unittest.TestCase):
    def test_create_list_get_resolve_flow(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_video_ad_review(
                "100", "200", "tester", "疑似广告：可能是推广", "fp_abc", "https://x.mp4"
            )
            self.assertGreater(rid, 0)
            lst = st.list_pending_video_ad_reviews()
            self.assertEqual(1, len(lst))
            self.assertEqual("pending", lst[0]["status"])
            self.assertEqual("100", lst[0]["group_id"])
            item = st.get_video_ad_review(rid)
            self.assertIsNotNone(item)
            self.assertEqual("fp_abc", item["fingerprint"])
            self.assertTrue(
                st.resolve_video_ad_review(rid, "confirmed", "admin")
            )
            self.assertFalse(
                st.resolve_video_ad_review(rid, "cleared", "admin2")
            )  # 已确认，CAS 拒绝
            self.assertEqual([], st.list_pending_video_ad_reviews())
            item2 = st.get_video_ad_review(rid)
            self.assertEqual("confirmed", item2["status"])
            self.assertEqual("admin", item2["reviewed_by"])
        finally:
            tmp.cleanup()

    def test_create_empty_and_invalid_status(self):
        st, tmp = _new_storage()
        try:
            self.assertEqual(0, st.create_video_ad_review("", "", ""))
            rid = st.create_video_ad_review("100", "200", "u", "t")
            self.assertGreater(rid, 0)
            self.assertFalse(st.resolve_video_ad_review(rid, "bogus", "admin"))
            self.assertEqual("pending", st.get_video_ad_review(rid)["status"])
        finally:
            tmp.cleanup()


class VideoAuditSignalTests(unittest.IsolatedAsyncioTestCase):
    """video_audit._apply_video_audit 对「疑似广告」识别文本设置复核信号。"""

    def setUp(self):
        video_audit = _load("group_guardian_video_review_va", "video_audit.py")

        class _Harness(video_audit.VideoAuditMixin):
            _AUDIT_MAX_CHARS = 100_000
            _recent_video_fingerprints = {}

            def __init__(self):
                self.config = {}
                self.cfg_values = {}
                self._init_video_audit_resources(4)

            def _cfg(self, key, default=True, group_id=None):
                return self.cfg_values.get(key, default)

            def _cfg_int(self, key, default=0, group_id=None):
                return int(self.cfg_values.get(key, default))

            @staticmethod
            def _bounded_audit_text(text, max_chars):
                return str(text or "")[:max_chars]

            async def _audit_all_videos(self, event, videos, group_id):
                return ["[视频第1帧] 疑似广告：可能是推广内容"]

        self.Harness = _Harness
        self.h = _Harness()

    async def test_suspicion_signal_set_when_enabled(self):
        self.h.cfg_values["video_audit_enabled"] = True
        self.h.cfg_values["llm_moderation_enabled"] = True
        self.h.cfg_values["video_ad_visual_enabled"] = True
        result = await self.h._apply_video_audit(
            "body", [("v", {}, "https://x.mp4")], None, "100"
        )
        self.assertIn("疑似广告", result)
        self.assertTrue(self.h._video_ad_review_signal)
        self.assertEqual("https://x.mp4", self.h._video_ad_review_source)

    async def test_signal_reset_when_videos_empty(self):
        result = await self.h._apply_video_audit("body", [], None, "100")
        self.assertEqual("body", result)
        self.assertFalse(self.h._video_ad_review_signal)

    async def test_no_signal_when_not_suspicion(self):
        class _H2(self.Harness):
            async def _audit_all_videos(self, event, videos, group_id):
                return ["[视频第1帧] 正常画面"]

        h2 = _H2()
        h2.cfg_values["video_audit_enabled"] = True
        h2.cfg_values["llm_moderation_enabled"] = True
        result = await h2._apply_video_audit("body", [("v", {}, "u")], None, "100")
        self.assertIn("正常画面", result)
        self.assertFalse(h2._video_ad_review_signal)



class _Req:
    def __init__(self, body):
        self._body = body

    async def get_json(self, **kw):
        return self._body


class AdBackendReviewHandlerTests(unittest.IsolatedAsyncioTestCase):
    """ad_backend 确认违规/放行 handler（mock 依赖与 quart）。"""

    def setUp(self):
        self.ab = _load("group_guardian_video_review_ab", "ad_backend.py")
        self.ab.jsonify = lambda *args, **kw: (args[0] if args else kw)
        self.ab.quart_request = _Req({})

        class _Harness(self.ab.AdBackendMixin):
            def __init__(self):
                self.config = {"video_ad_review_enabled": True}
                self._config_schema = {}
                self.cfg_values = {}
                self.learned = []
                self.logged = []
                self.banned_group = None
                self.banned_user = None

            def _cfg_int(self, key, default=0, group_id=None):
                return int(self.cfg_values.get(key, default))

            def _log_moderation(self, *args, **kwargs):
                self.logged.append(args)

            def _learn_video_fingerprint(self, fp):
                self.learned.append(fp)

            async def _get_client(self):
                return object()

            async def _call_group_api(self, client, action, name, **kwargs):
                self.banned_group = kwargs.get("group_id")
                self.banned_user = kwargs.get("user_id")
                return True, ""

        self.h = _Harness()
        self.h._storage = _FakeStorage()

    async def test_confirm_review(self):
        self.ab.quart_request = _Req({"review_id": 1, "reviewer": "admin"})
        result = await self.h._ad_backend_video_reviews_confirm()
        self.assertEqual("success", result["status"])
        self.assertTrue(result["banned"])
        self.assertTrue(result["learned_fingerprint"])
        self.assertEqual(["fp_1"], self.h.learned)
        self.assertEqual(1, self.h.banned_group)
        self.assertEqual(2, self.h.banned_user)
        self.assertEqual("confirmed", self.h._storage.reviews[1]["status"])

    async def test_clear_review(self):
        self.ab.quart_request = _Req({"review_id": 2, "reviewer": "admin"})
        result = await self.h._ad_backend_video_reviews_clear()
        self.assertEqual("success", result["status"])
        self.assertEqual("cleared", self.h._storage.reviews[2]["status"])

    async def test_confirm_non_pending_rejected(self):
        self.h._storage.reviews[1]["status"] = "cleared"
        self.ab.quart_request = _Req({"review_id": 1, "reviewer": "admin"})
        result = await self.h._ad_backend_video_reviews_confirm()
        self.assertEqual("error", result["status"])


class _FakeStorage:
    def __init__(self):
        self.reviews = {
            1: {
                "id": 1, "status": "pending", "group_id": "1", "user_id": "2",
                "user_name": "u", "msg_text": "疑似广告", "fingerprint": "fp_1",
            },
            2: {
                "id": 2, "status": "pending", "group_id": "1", "user_id": "3",
                "user_name": "u", "msg_text": "疑似广告2", "fingerprint": "",
            },
        }

    def get_video_ad_review(self, rid):
        return self.reviews.get(int(rid))

    def resolve_video_ad_review(self, rid, status, reviewer=""):
        item = self.reviews.get(int(rid))
        if not item or item["status"] != "pending":
            return False
        item["status"] = status
        item["reviewed_by"] = reviewer
        return True



class VideoReviewStaticChecks(unittest.TestCase):
    def test_schema_has_review_configs(self):
        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["video_ad_review_enabled"]["default"])
        self.assertTrue(schema["video_ad_review_recall"]["default"])
        self.assertTrue(schema["video_ad_review_notice"]["default"])

    def test_web_registers_review_routes(self):
        src = (ROOT / "web.py").read_text(encoding="utf-8")
        for route in (
            '"/ad_backend/video_reviews"',
            '"/ad_backend/video_reviews/confirm"',
            '"/ad_backend/video_reviews/clear"',
        ):
            self.assertIn(route, src)

    def test_storage_has_review_table_and_crud(self):
        src = (ROOT / "storage.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS video_ad_reviews", src)
        for method in (
            "def create_video_ad_review",
            "def list_pending_video_ad_reviews",
            "def resolve_video_ad_review",
        ):
            self.assertIn(method, src)

    def test_video_audit_has_suspicion_signal(self):
        src = (ROOT / "video_audit.py").read_text(encoding="utf-8")
        self.assertIn("_video_ad_review_signal", src)
        self.assertIn('"疑似广告"', src)


if __name__ == "__main__":
    unittest.main()

