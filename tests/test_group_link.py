"""v2.29.0：QQ 群链接（消息撤回 + 名片还原）测试。

覆盖：
- advanced_audit.py：_find_qq_group_link 检测（含去链化）、_detect_link_violation
  在 invite_link_recall_enabled 关闭时仍无条件命中 QQ 群链接
- card_monitor.py：_is_shop_link_card 命中 qm.qq.com（无协议前缀）、
  _PROMO_SUSPECT_RE 命中「群链接/拉群」
"""

import importlib.util
import sys
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
    spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


advanced_audit = _load("group_guardian_qq_link_aa", "advanced_audit.py")
card_monitor = _load("group_guardian_qq_link_cm", "card_monitor.py")


class _LinkHarness(advanced_audit.AdvancedAuditMixin):
    def _invite_link_hit(self, group_id):
        return False  # 关闭外链邀请开关，验证 QQ 群链接仍无条件命中

    def _url_safety_hit(self, group_id):
        return False


class QqGroupLinkDetectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = _LinkHarness()

    def test_find_qq_group_link_full_url(self):
        self.assertEqual(
            "https://qm.qq.com/q/abc", self.h._find_qq_group_link("看这里 https://qm.qq.com/q/abc")
        )

    def test_find_qq_group_link_no_protocol(self):
        # 去链化（无 https:// 前缀）也应命中
        self.assertEqual("qm.qq.com", self.h._find_qq_group_link("加群 qm.qq.com/q/abc"))

    def test_find_qq_group_link_other_hosts(self):
        self.assertEqual("jq.qq.com", self.h._find_qq_group_link("jq.qq.com/abc"))
        self.assertEqual("qun.qq.com", self.h._find_qq_group_link("qun.qq.com/abc"))
        self.assertEqual("pd.qq.com", self.h._find_qq_group_link("pd.qq.com/abc"))

    def test_find_qq_group_link_normal_text(self):
        self.assertEqual("", self.h._find_qq_group_link("今天天气不错"))

    async def test_detect_link_violation_unconditional_for_qq_group_link(self):
        # invite_link_recall_enabled 关闭时，QQ 群链接仍无条件命中
        violation = await self.h._detect_link_violation(
            "拉人 qm.qq.com/q/xyz", "100"
        )
        self.assertIsNotNone(violation)
        self.assertEqual("外链邀请", violation[0])
        self.assertIn("qm.qq.com", violation[1])

    async def test_detect_link_violation_normal_text(self):
        violation = await self.h._detect_link_violation("正常聊天内容", "100")
        self.assertIsNone(violation)


class CardQqGroupLinkTests(unittest.TestCase):
    def test_shop_link_card_qq_group_url(self):
        self.assertTrue(
            card_monitor.CardMonitorMixin._is_shop_link_card("qm.qq.com/q/abc")
        )
        self.assertTrue(
            card_monitor.CardMonitorMixin._is_shop_link_card(
                "https://qm.qq.com/q/abc"
            )
        )

    def test_promo_suspect_group_link_text(self):
        self.assertIsNotNone(card_monitor._PROMO_SUSPECT_RE.search("进群链接"))
        self.assertIsNotNone(card_monitor._PROMO_SUSPECT_RE.search("拉群私聊"))

    def test_shop_link_card_normal(self):
        self.assertFalse(
            card_monitor.CardMonitorMixin._is_shop_link_card("阿伟")
        )


if __name__ == "__main__":
    unittest.main()
