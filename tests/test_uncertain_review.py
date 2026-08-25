"""v2.32.0 不确定内容（LLM 无法确认）私信群管理员复核功能测试。

覆盖：
- storage.py：uncertain_reviews 表 CRUD（创建 / 列出 / 查询 / CAS 确认 / 放行）
- moderation.py：_normalize_llm_moderation_result 支持 violation="unknown" → uncertain
- moderation.py：_fetch_group_admin_ids 过滤群主/管理员；_notify_uncertain_admins_private 私信
- moderation.py：_submit_uncertain_review 落队 + 私信管理员 + 群内通知
- commands.py：确认复核 #N / 放行复核 #N（插件管理员 / 群管理员 / 非管理员拒绝）
- _conf_schema.json：4 个新配置项默认值
"""

import importlib.util
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
    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = object
    sys.modules["astrbot.api.event"] = api_event
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


storage = _load("group_guardian_uncertain_storage", "storage.py")


def _new_storage():
    tmp = tempfile.TemporaryDirectory(prefix="gg_uncertain_review_")
    st = storage.SQLiteStorage(Path(tmp.name), str(ROOT))
    with st._connect() as conn:
        storage.SQLiteStorage._create_tables(conn)  # 只建表，跳过 seed 导入
    return st, tmp


class StorageUncertainTests(unittest.TestCase):
    def test_create_list_get_resolve_flow(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_uncertain_review(
                "100", "200", "tester", "这段内容无法判断是否违规", "text"
            )
            self.assertGreater(rid, 0)
            lst = st.list_pending_uncertain_reviews()
            self.assertEqual(1, len(lst))
            self.assertEqual("pending", lst[0]["status"])
            self.assertEqual("100", lst[0]["group_id"])
            self.assertEqual("text", lst[0]["source"])
            item = st.get_uncertain_review(rid)
            self.assertIsNotNone(item)
            self.assertTrue(
                st.resolve_uncertain_review(rid, "confirmed", "admin")
            )
            self.assertFalse(
                st.resolve_uncertain_review(rid, "cleared", "admin2")
            )  # 已确认，CAS 拒绝
            self.assertEqual([], st.list_pending_uncertain_reviews())
            item2 = st.get_uncertain_review(rid)
            self.assertEqual("confirmed", item2["status"])
            self.assertEqual("admin", item2["reviewed_by"])
        finally:
            tmp.cleanup()

    def test_create_empty_and_invalid_status(self):
        st, tmp = _new_storage()
        try:
            self.assertEqual(0, st.create_uncertain_review("", "", ""))
            rid = st.create_uncertain_review("100", "200", "u", "t", "image")
            self.assertGreater(rid, 0)
            self.assertFalse(st.resolve_uncertain_review(rid, "bogus", "admin"))
            self.assertEqual("pending", st.get_uncertain_review(rid)["status"])
        finally:
            tmp.cleanup()


class LlmNormalizeUncertainTests(unittest.TestCase):
    """v2.32.0：LLM 判定三态归一化。"""

    def setUp(self):
        self.moderation = _load("group_guardian_uncertain_mod", "moderation.py")

        class _H(self.moderation.ModerationMixin):
            pass

        self.h = _H()

    def test_unknown_marks_uncertain(self):
        result = self.h._normalize_llm_moderation_result(
            {"violation": "unknown", "reason": "上下文不足"}
        )
        self.assertFalse(result["violation"])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["fallback"])

    def test_uncertain_synonyms(self):
        for value in ("疑似", "无法判断", "无法确认", "不确定", "无法判定"):
            result = self.h._normalize_llm_moderation_result(
                {"violation": value, "reason": "r"}
            )
            self.assertTrue(result["uncertain"], value)

    def test_true_false_unchanged(self):
        self.assertFalse(
            self.h._normalize_llm_moderation_result(
                {"violation": True, "reason": "r"}
            ).get("uncertain", False)
        )
        self.assertFalse(
            self.h._normalize_llm_moderation_result(
                {"violation": "false", "reason": "r"}
            ).get("uncertain", False)
        )

class FetchAdminsAndNotifyTests(unittest.IsolatedAsyncioTestCase):
    """_fetch_group_admin_ids 过滤群主/管理员 + _notify_uncertain_admins_private 私信。"""

    def setUp(self):
        self.moderation = _load("group_guardian_uncertain_mod2", "moderation.py")

        class _Client:
            async def call_action(self, action, **kwargs):
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {"user_id": 1001, "role": "owner"},
                        {"user_id": 1002, "role": "admin"},
                        {"user_id": 1003, "role": "member"},
                        {"user_id": 1004, "role": "admin"},
                    ],
                }

        class _H(self.moderation.ModerationMixin):
            def __init__(self):
                self.privates = []
                self.client = _Client()

            def _safe_int(self, value, default=0):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default

            async def _get_client(self):
                return self.client

            async def _call_group_api_result(self, client, action, name="", **kw):
                result = await client.call_action(action, **kw)
                return True, result.get("data") or [], ""

            async def _send_private_message(self, user_id, text):
                self.privates.append((str(user_id), str(text)))

        self.h = _H()

    async def test_fetch_group_admin_ids_filters_roles(self):
        admins = await self.h._fetch_group_admin_ids("100")
        self.assertEqual(["1001", "1002", "1004"], admins)

    async def test_notify_admins_private_sends_all_admins(self):
        sent = await self.h._notify_uncertain_admins_private(
            "100", "200", "tester", "无法确认的内容", 7, "text"
        )
        self.assertEqual(3, sent)
        self.assertEqual(3, len(self.h.privates))
        self.assertIn("确认复核 #7", self.h.privates[0][1])
        self.assertIn("放行复核 #7", self.h.privates[0][1])
        self.assertIn("无法确认的内容", self.h.privates[0][1])

    async def test_notify_video_source_uses_video_cmds(self):
        sent = await self.h._notify_uncertain_admins_private(
            "100", "200", "u", "疑似视频广告内容", 9, "video"
        )
        self.assertEqual(3, sent)
        self.assertIn("确认广告 #9", self.h.privates[0][1])
        self.assertIn("疑似视频广告", self.h.privates[0][1])


class SubmitUncertainReviewTests(unittest.IsolatedAsyncioTestCase):
    """_submit_uncertain_review：落队 + 私信管理员 + 群内通知。"""

    def setUp(self):
        self.moderation = _load("group_guardian_uncertain_mod3", "moderation.py")

        class _Storage:
            def __init__(self):
                self.created = None

            def create_uncertain_review(self, g, u, name, text, source):
                self.created = (g, u, name, text, source)
                return 42

        class _H(self.moderation.ModerationMixin):
            def __init__(self):
                self._storage = _Storage()
                self.bools = {"uncertain_review_private_admin": True,
                             "uncertain_review_notice": True}
                self.logs = []
                self.sent = 0

            def _cfg(self, key, default=False, group_id=None):
                return self.bools.get(key, default)

            def _log_moderation(self, *args):
                self.logs.append(args)

            async def _notify_uncertain_admins_private(self, *args):
                self.sent += 1
                return 1

        self.h = _H()

    async def _collect(self, coro, event):
        replies = []
        async for item in coro:
            replies.append(str(item.message_str))
        return replies

    async def test_submit_creates_review_and_notifies(self):
        event = types.SimpleNamespace(
            plain_result=lambda msg: types.SimpleNamespace(message_str=str(msg)),
            stop_event=lambda: None,
        )
        replies = await self._collect(
            self.h._submit_uncertain_review(
                event, "100", "200", "tester", "无法确认内容", "上下文不足",
                [], {}, [], "text",
            ),
            event,
        )
        self.assertEqual(("100", "200", "tester", "无法确认内容", "text"),
                         self.h._storage.created)
        self.assertEqual(1, self.h.sent)
        self.assertTrue(any("确认复核 #42" in r for r in replies))
        self.assertTrue(any("管理" in r for r in replies))


class _FakeCmdStorage:
    def __init__(self):
        self.reviews = {
            1: {"id": 1, "status": "pending", "group_id": "1", "user_id": "2",
                "user_name": "u", "msg_text": "无法确认内容"},
            2: {"id": 2, "status": "pending", "group_id": "1", "user_id": "3",
                "user_name": "u", "msg_text": "无法确认内容2"},
        }
        self.logs = []

    def get_uncertain_review(self, rid):
        return self.reviews.get(int(rid))

    def resolve_uncertain_review(self, rid, status, reviewer=""):
        item = self.reviews.get(int(rid))
        if not item or item["status"] != "pending":
            return False
        item["status"] = status
        item["reviewed_by"] = reviewer
        return True

    def log_moderation(self, *args):
        self.logs.append(args)


class UncertainCommandTests(unittest.IsolatedAsyncioTestCase):
    """「确认复核 #N / 放行复核 #N」命令（私聊/管理群，权限校验）。"""

    def setUp(self):
        commands = _load("group_guardian_uncertain_cmd", "commands.py")

        class _H(commands.CommandsMixin):
            def __init__(self):
                self._storage = _FakeCmdStorage()
                self.roles = {"admin1": "admin", "owner1": "owner", "member1": "member"}

            async def _is_plugin_admin(self, event):
                return bool(getattr(event, "admin", False))

            def _try_get_sender_id(self, event):
                return str(getattr(event, "sender_id", "") or "")

            async def _get_client(self):
                return object()

            async def _get_role_by_id(self, client, group_id, user_id):
                return self.roles.get(user_id, "")

            def _log_moderation(self, *args):
                self._storage.logs.append(args)

        self.h = _H()

    async def _collect(self, coro, event):
        if not hasattr(event, "plain_result"):
            event.plain_result = lambda msg: types.SimpleNamespace(message_str=str(msg))
        replies = []
        async for item in coro:
            replies.append(str(item.message_str))
        return replies

    async def test_confirm_by_plugin_admin_private(self):
        event = types.SimpleNamespace(
            admin=True, sender_id="admin1", message_str="确认复核 #1",
        )
        replies = await self._collect(self.h.cmd_review_uncertain_confirm(event), event)
        self.assertTrue(any("已确认违规" in r for r in replies))
        self.assertEqual("confirmed", self.h._storage.reviews[1]["status"])

    async def test_clear_by_group_admin(self):
        event = types.SimpleNamespace(
            admin=False, sender_id="admin1", message_str="放行复核 #2",
        )
        replies = await self._collect(self.h.cmd_review_uncertain_clear(event), event)
        self.assertTrue(any("已放行" in r for r in replies))
        self.assertEqual("cleared", self.h._storage.reviews[2]["status"])

    async def test_rejects_non_admin(self):
        event = types.SimpleNamespace(
            admin=False, sender_id="member1", message_str="确认复核 #1",
        )
        replies = await self._collect(self.h.cmd_review_uncertain_confirm(event), event)
        self.assertTrue(any("权限不足" in r for r in replies))
        self.assertEqual("pending", self.h._storage.reviews[1]["status"])

    async def test_rejects_unknown_id(self):
        event = types.SimpleNamespace(
            admin=True, sender_id="admin1", message_str="确认复核 #99",
        )
        replies = await self._collect(self.h.cmd_review_uncertain_confirm(event), event)
        self.assertTrue(any("未找到" in r for r in replies))

    async def test_rejects_bad_number(self):
        event = types.SimpleNamespace(
            admin=True, sender_id="admin1", message_str="确认复核 abc",
        )
        replies = await self._collect(self.h.cmd_review_uncertain_confirm(event), event)
        self.assertTrue(any("编号无效" in r for r in replies))


class UncertainSchemaTests(unittest.TestCase):
    def test_new_config_defaults(self):
        schema = json_load_schema()
        self.assertFalse(schema["uncertain_review_enabled"]["default"])
        self.assertFalse(schema["uncertain_review_private_admin"]["default"])
        self.assertTrue(schema["uncertain_review_notice"]["default"])
        self.assertFalse(schema["video_ad_review_private_admin"]["default"])

    def test_llm_prompt_three_state(self):
        src = (ROOT / "moderation.py").read_text(encoding="utf-8")
        self.assertIn('"unknown"', src)
        self.assertIn("uncertain", src)


def json_load_schema():
    import json
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
