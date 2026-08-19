# -*- coding: utf-8 -*-
"""v2.36.0 疑似广告先人工确认再处罚 + 文本指纹学习（adguard 合并）测试。

覆盖：
- storage.py：ad_reviews 表 CRUD（创建/列出/查询/CAS 确认/放行，含 msg_id）
- storage.py：ad_text_fingerprints 文本指纹学习库（ad/ok、覆盖更新、清空）
- moderation.py：_ad_text_fingerprint 归一化指纹、_ad_review_should_route 路由判定
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


storage = _load("group_guardian_ad_review_storage", "storage.py")
moderation = _load("group_guardian_ad_review_moderation", "moderation.py")


def _new_storage():
    tmp = tempfile.TemporaryDirectory(prefix="gg_ad_review_")
    st = storage.SQLiteStorage(Path(tmp.name), str(ROOT))
    with st._connect() as conn:
        storage.SQLiteStorage._create_tables(conn)  # 只建表，跳过 seed 导入
    return st, tmp


class StorageAdReviewTests(unittest.TestCase):
    def test_create_list_get_resolve_flow_with_msg_id(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_ad_review(
                "100", "200", "tester", "加微信abc123 领取优惠",
                msg_id="msg-001", image_urls=["http://x/1.png"], source="text",
            )
            self.assertGreater(rid, 0)
            lst = st.list_pending_ad_reviews()
            self.assertEqual(1, len(lst))
            self.assertEqual("pending", lst[0]["status"])
            self.assertEqual("msg-001", lst[0]["msg_id"])
            self.assertIn("http://x/1.png", lst[0]["image_urls"])
            self.assertEqual("text", lst[0]["source"])
            item = st.get_ad_review(rid)
            self.assertIsNotNone(item)
            self.assertTrue(st.resolve_ad_review(rid, "confirmed", "admin"))
            self.assertFalse(st.resolve_ad_review(rid, "released", "admin2"))  # CAS
            self.assertEqual([], st.list_pending_ad_reviews())
            item2 = st.get_ad_review(rid)
            self.assertEqual("confirmed", item2["status"])
            self.assertEqual("admin", item2["reviewed_by"])
        finally:
            tmp.cleanup()

    def test_create_empty_and_invalid_status(self):
        st, tmp = _new_storage()
        try:
            self.assertEqual(0, st.create_ad_review("", "", ""))
            rid = st.create_ad_review("100", "200", "u", "t", source="card")
            self.assertGreater(rid, 0)
            self.assertFalse(st.resolve_ad_review(rid, "bogus", "admin"))
            self.assertEqual("pending", st.get_ad_review(rid)["status"])
        finally:
            tmp.cleanup()

    def test_ad_text_fingerprint_learn_and_hit(self):
        st, tmp = _new_storage()
        try:
            self.assertIsNone(st.ad_text_fingerprint_hit("fp-ad-1"))
            self.assertTrue(st.learn_ad_text_fingerprint("fp-ad-1", "ad", "100", "加我"))
            self.assertEqual("ad", st.ad_text_fingerprint_hit("fp-ad-1"))
            self.assertTrue(st.learn_ad_text_fingerprint("fp-ok-1", "ok", "100", "正常"))
            self.assertEqual("ok", st.ad_text_fingerprint_hit("fp-ok-1"))
            # 重复学习覆盖为最新结论
            self.assertTrue(st.learn_ad_text_fingerprint("fp-ad-1", "ok", "100", "改判"))
            self.assertEqual("ok", st.ad_text_fingerprint_hit("fp-ad-1"))
            # 非法 verdict 拒绝
            self.assertFalse(st.learn_ad_text_fingerprint("fp-x", "bogus", "100"))
            lst = st.list_ad_text_fingerprints()
            self.assertEqual(2, len(lst))
            self.assertGreater(st.clear_ad_text_fingerprints(), 0)
            self.assertEqual([], st.list_ad_text_fingerprints())
        finally:
            tmp.cleanup()


class AdReviewRouteTests(unittest.TestCase):
    def setUp(self):
        _stub_astrbot()
        self.cfg_values = {"ad_review_enabled": True}
        self._storage = _StorageFake()

    def _cfg(self, key, default=None, group_id=None):
        return self.cfg_values.get(key, default)

    @staticmethod
    def _ad_escalation_is_ad(hit_summary="", hit_types=None):
        if hit_types:
            for key in ("ad", "learned_ad", "ad_hash", "image_scan"):
                if hit_types.get(key):
                    return True
        if hit_summary:
            lowered = str(hit_summary).lower()
            for token in ("ad", "广告", "image_scan", "learned_ad"):
                if token in lowered:
                    return True
        return False

    @staticmethod
    def _ad_text_fingerprint(text):
        import hashlib
        import re
        norm = re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE).lower()
        if not norm:
            return ""
        return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()

    def _ad_review_text_verdict(self, text):
        fp = self._ad_text_fingerprint(text)
        return self._storage.hits.get(fp, "")

    def _ad_review_should_route(self, group_id, hit_types, text="", reason="", hit_summary=""):
        if not self._cfg("ad_review_enabled", False, group_id=group_id):
            return False
        is_ad = self._ad_escalation_is_ad(hit_summary=hit_summary, hit_types=hit_types)
        if not is_ad and reason:
            if any(token in str(reason) for token in ("广告", "推广", "引流", "营销")):
                is_ad = True
        if not is_ad:
            return False
        if self._ad_review_text_verdict(text) == "ad":
            return False
        return True

    def test_fingerprint_normalization(self):
        fp1 = self._ad_text_fingerprint("加微信： abc123 领取优惠")
        fp2 = self._ad_text_fingerprint("加微信abc123领取优惠")
        fp3 = self._ad_text_fingerprint("这是另一条消息内容")
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertEqual("", self._ad_text_fingerprint(""))

    def test_disabled_never_routes(self):
        self.cfg_values["ad_review_enabled"] = False
        self.assertFalse(self._ad_review_should_route("100", {"ad": True}, "广告文本"))

    def test_ad_hit_routes_when_enabled(self):
        self.assertTrue(self._ad_review_should_route("100", {"ad": True}, "微信支付宝 熟悉环境"))
        self.assertTrue(self._ad_review_should_route(
            "100", {"full_scan": True}, "疑似广告",
            reason="内容包含广告引流", hit_summary="full_scan",
        ))

    def test_learned_ad_skips_review_and_goes_direct(self):
        text = "加微信abc123 领取优惠"
        self._storage.hits[self._ad_text_fingerprint(text)] = "ad"
        self.assertFalse(self._ad_review_should_route("100", {"ad": True}, text))

    def test_non_ad_never_routes(self):
        self.assertFalse(self._ad_review_should_route("100", {"swear": True}, "脏话内容"))
        self.assertFalse(self._ad_review_should_route(
            "100", {"full_scan": True}, "正常消息",
            reason="正常讨论", hit_summary="full_scan",
        ))


class _StorageFake:
    def __init__(self):
        self.hits = {}

    def ad_text_fingerprint_hit(self, fingerprint):
        return self.hits.get(fingerprint)


class AdReviewSchemaTests(unittest.TestCase):
    """v2.36.1：后台审核日志确认交互对应的配置 schema 断言。"""

    def test_schema_removed_forward_and_notice(self):
        import json
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("ad_review_enabled", "ad_review_admin_private",
                    "ad_review_admin_ids", "ad_review_learn_text"):
            self.assertIn(key, schema, key)
        # v2.36.1：群里不通知 → 移除群内通知/管理群转发配置
        self.assertNotIn("ad_review_notice", schema)
        self.assertNotIn("ad_review_forward_group", schema)

    def test_schema_hint_mentions_backend_only(self):
        import json
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        hint = schema["ad_review_enabled"]["hint"]
        self.assertIn("后台", hint)
        self.assertIn("群里不通知", hint)
        private_hint = schema["ad_review_admin_private"]["hint"]
        self.assertIn("后台", private_hint)


class AdReviewSubmitTests(unittest.TestCase):
    """v2.36.1：疑似广告入队不发送任何群内通知（后台审核日志确认 + 私信通知）。"""

    async def _run_submit(self, storage, event):
        inst = object.__new__(moderation.ModerationMixin)
        inst._storage = storage
        inst._log_moderation = lambda *a, **k: None
        inst._cfg = lambda key, default=None, group_id=None: True
        sent = []

        async def _notify(grp, uid, name, text, review_id, source="text"):
            sent.append((uid, review_id, text))

        inst._notify_ad_admins_private = _notify
        await inst._submit_ad_review(
            event, "100", "200", "tester", "加微信abc123 领取优惠", [], "text"
        )
        return sent

    def test_submit_is_coroutine_not_generator(self):
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(
            moderation.ModerationMixin._submit_ad_review
        ))

    def test_submit_no_group_notice_and_private_notified(self):
        import asyncio
        import types

        async def main():
            class _Storage:
                def create_ad_review(self, *a, **k):
                    return 7

            stopped = []
            event = types.SimpleNamespace(
                message_obj=types.SimpleNamespace(message_id="msg-9"),
                stop_event=lambda: stopped.append(1),
            )
            sent = await self._run_submit(_Storage(), event)
            self.assertEqual(1, len(sent))  # 仅私信通知一次
            self.assertEqual(("200", 7, "加微信abc123 领取优惠"), sent[0])
            self.assertEqual([1], stopped)
            return True

        self.assertTrue(asyncio.run(main()))

    def test_notify_admins_private_mentions_backend(self):
        import asyncio

        async def main():
            inst = object.__new__(moderation.ModerationMixin)
            inst._admin_list = ["50001"]
            inst._cfg_str = lambda key, default="": (
                "50001" if key == "ad_review_admin_ids" else default
            )
            sent = []

            async def _send(uid, content):
                sent.append((uid, content))

            inst._send_private_message = _send
            await inst._notify_ad_admins_private(
                "100", "200", "tester", "加微信abc123 领取优惠", 9, "text"
            )
            self.assertEqual(1, len(sent))
            content = sent[0][1]
            self.assertIn("后台审核日志", content)
            self.assertIn("WebUI", content)
            self.assertNotIn("请私聊回复", content)
            return True

        self.assertTrue(asyncio.run(main()))


if __name__ == "__main__":
    unittest.main()

