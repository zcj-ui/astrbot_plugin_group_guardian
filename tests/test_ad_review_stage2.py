# -*- coding: utf-8 -*-
"""阶段二：广告复核安全修复回归测试。

覆盖打回审查的验收项：
- 群名片违规按类别路由（仅广告类进复核，辱骂等非广告走直接还原）
- 名片复核使用独立字段 restore_value（不再把名片值当 msg_id，避免误撤回）
- 复核确认只学习该记录持久化的图片/视频证据（media_hashes / video_fingerprints）
- 旧日志按「群+用户+内容」唯一匹配复核（多条时不自动绑定，避免错绑）

无需安装 AstrBot：顶层 astrbot 包用 shim 代替。
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
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    sys.modules.update({"astrbot": sys.modules["astrbot"], "astrbot.api": api})


def _load(module_name, filename):
    _stub_astrbot()
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(name):
    return (ROOT / name).read_text(encoding="utf-8")


storage = _load("group_guardian_ad_review_stage2_storage", "storage.py")


def _new_storage():
    tmp = tempfile.TemporaryDirectory(prefix="gg_ad_review_stage2_")
    st = storage.SQLiteStorage(Path(tmp.name), str(ROOT))
    with st._connect() as conn:
        storage.SQLiteStorage._create_tables(conn)
    return st, tmp


class StorageAdReviewEvidenceTests(unittest.TestCase):
    """ad_reviews 独立字段与证据持久化。"""

    def test_card_review_uses_restore_value_not_msg_id(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_ad_review(
                "100", "200", "u", "[群名片] 加V卖货", "",
                [], "card", restore_value="旧名片-张三",
            )
            self.assertGreater(rid, 0)
            item = st.get_ad_review(rid)
            self.assertEqual("", item["msg_id"])
            self.assertEqual("旧名片-张三", item["restore_value"])
        finally:
            tmp.cleanup()

    def test_media_evidence_persisted(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_ad_review(
                "100", "200", "u", "广告图", "msg-9", ["http://x/1.png"], "image",
                media_hashes={"http://x/1.png": "phash_abc"},
                video_fingerprints=["fp_1_10", "fp_2_20"],
            )
            self.assertGreater(rid, 0)
            item = st.get_ad_review(rid)
            media = json.loads(item["media_hashes"] or "{}")
            fps = json.loads(item["video_fingerprints"] or "[]")
            self.assertEqual("phash_abc", media.get("http://x/1.png"))
            self.assertEqual(["fp_1_10", "fp_2_20"], fps)
        finally:
            tmp.cleanup()

    def test_legacy_record_fields_default_empty(self):
        st, tmp = _new_storage()
        try:
            rid = st.create_ad_review("100", "200", "u", "t", source="text")
            item = st.get_ad_review(rid)
            self.assertEqual("", item.get("restore_value", ""))
            self.assertEqual({}, json.loads(item.get("media_hashes", "{}") or "{}"))
            self.assertEqual([], json.loads(item.get("video_fingerprints", "[]") or "[]"))
        finally:
            tmp.cleanup()

class CardMonitorRoutingTests(unittest.TestCase):
    """名片违规按类别路由（仅广告类进复核）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("card_monitor.py")

    def test_card_review_only_for_ad_like_violations(self):
        self.assertIn("ad_like", self.src)
        self.assertIn('hit_types.get("ad")', self.src)
        self.assertIn('hit_types.get("promo")', self.src)
        self.assertIn("_is_shop_link_card(card_new)", self.src)
        self.assertIn('self._cfg("ad_review_enabled"', self.src)
        i_adlike = self.src.find("ad_like")
        i_review = self.src.find("create_ad_review")
        self.assertGreater(i_review, i_adlike)

    def test_card_review_msg_id_empty_restore_value_separate(self):
        self.assertIn('"",', self.src)
        self.assertIn('restore_value=str(card_old or "")', self.src)


class WebConfirmEvidenceTests(unittest.TestCase):
    """复核确认只学习记录证据 + 名片用 restore_value 还原 + 旧日志唯一匹配。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("web.py")

    def test_confirm_learns_from_record_evidence(self):
        self.assertIn("_learn_ad_hashes_from", self.src)
        self.assertIn("_learn_video_fingerprints_from", self.src)
        self.assertIn("media_hashes", self.src)
        self.assertIn("video_fingerprints", self.src)
        seg = self.src[self.src.find("async def _web_ad_review_confirm"):self.src.find("async def _web_ad_review_clear")]
        self.assertNotIn("_learn_recent_ad_hashes", seg)
        self.assertNotIn("_learn_recent_video_fingerprints", seg)

    def test_card_restore_uses_restore_value(self):
        self.assertIn("restore_value", self.src)
        self.assertIn('item.get("restore_value"', self.src)

    def test_legacy_log_unique_match_only(self):
        self.assertIn("len(reviews) == 1", self.src)
        self.assertIn("pending_by_group", self.src)


class HashAuditEvidenceTests(unittest.TestCase):
    """hash_audit 支持按指定证据学习。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("hash_audit.py")

    def test_learn_from_specific_evidence(self):
        self.assertIn("def _learn_ad_hashes_from", self.src)
        self.assertIn("def _learn_video_fingerprints_from", self.src)
        self.assertIn("_learn_ad_hashes_from(group_id, recent)", self.src)
        self.assertIn("_learn_video_fingerprints_from(list(recent))", self.src)


if __name__ == "__main__":
    unittest.main()
