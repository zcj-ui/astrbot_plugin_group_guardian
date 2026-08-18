# -*- coding: utf-8 -*-
"""v2.35.0：广告审核与其他违规审核开关完全独立。

scan_swear / scan_ad 分别成为脏话/广告的「完整审核开关」——同时控制内置正则
（_swear_matcher / _is_ad_pattern）与对应词库分类；此前词库 swear/ad 恒启用、
关闭开关也关不掉，导致「广告」与「其他违规」无法真正分开控制。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_utilities():
    # 与 test_nested_forward 保持一致：注入完整的 astrbot 模块树，保证两个测试
    # 文件无论加载顺序如何都能拿到相同的 shim（若这里只注入 api，后续
    # test_nested_forward 会因 "astrbot.api 已在 sys.modules" 跳过自己的完整
    # shim 而加载 moderation.py 失败）。
    if "astrbot.api" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None)
        core = types.ModuleType("astrbot.core")
        platform = types.ModuleType("astrbot.core.platform")
        sources = types.ModuleType("astrbot.core.platform.sources")
        aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
        event_module = types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
        event_module.AiocqhttpMessageEvent = object
        api_event = types.ModuleType("astrbot.api.event")
        api_event.AstrMessageEvent = object
        astrbot.api = api
        sys.modules.update({
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.core": core,
            "astrbot.core.platform": platform,
            "astrbot.core.platform.sources": sources,
            "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
        })
    # 使用独立包名加载 utils，绝不触碰 test_nested_forward 等共享的
    # `group_guardian` 包 / `group_guardian.automaton`（fake automaton 若被
    # 复用会导致后续测试拿到 object 占位而 hang 或失败）。
    pkg_name = "gg_audit_switch_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT)]
    automaton = types.ModuleType(pkg_name + ".automaton")
    automaton.KeywordAutomaton = object
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".automaton"] = automaton
    path = ROOT / "utils.py"
    spec = importlib.util.spec_from_file_location(pkg_name + ".utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utils = _load_utilities()
UtilitiesMixin = utils.UtilitiesMixin


class _SwitchHarness(UtilitiesMixin):
    def __init__(self, config=None, schema=None, overrides=None):
        self.config = dict(config or {})
        self._config_schema = dict(schema or {})
        self._storage = None
        self._overrides = dict(overrides or {})
        self._group_cfg_cache = {}

    def _get_group_override(self, group_id, key):
        return self._overrides.get(key)


class AuditSwitchSeparationTests(unittest.TestCase):
    def test_defaults_all_enabled(self):
        h = _SwitchHarness()
        m = h._lexicon_switch_map()
        for cat in ("swear", "ad", "political", "porn",
                    "violent_terror", "reactionary", "weapons",
                    "corruption", "illegal_url", "other"):
            self.assertTrue(m[cat], cat)

    def test_scan_ad_off_disables_ad_lexicon_only(self):
        # 关闭广告审核：ad 词库失效，脏话/其他违规不受影响
        h = _SwitchHarness(config={"scan_ad": False})
        m = h._lexicon_switch_map()
        self.assertFalse(m["ad"])
        self.assertTrue(m["swear"])
        self.assertTrue(m["political"])
        self.assertTrue(m["porn"])

    def test_scan_swear_off_disables_swear_lexicon_only(self):
        # 关闭脏话审核：swear 词库失效，广告/其他违规不受影响
        h = _SwitchHarness(config={"scan_swear": False})
        m = h._lexicon_switch_map()
        self.assertFalse(m["swear"])
        self.assertTrue(m["ad"])
        self.assertTrue(m["political"])

    def test_other_categories_independent_from_scan_switches(self):
        # 词库各分类开关独立于 scan_swear/scan_ad
        h = _SwitchHarness(config={
            "scan_swear": False,
            "scan_ad": False,
            "lexicon_political_enabled": False,
            "lexicon_porn_enabled": False,
        })
        m = h._lexicon_switch_map()
        self.assertFalse(m["swear"])
        self.assertFalse(m["ad"])
        self.assertFalse(m["political"])
        self.assertFalse(m["porn"])
        self.assertTrue(m["violent_terror"])
        self.assertTrue(m["other"])

    def test_other_follows_other_switch(self):
        # supplement/livelihood/tencent_ban 跟随 lexicon_other_enabled
        h = _SwitchHarness(config={"lexicon_other_enabled": False})
        m = h._lexicon_switch_map()
        for cat in ("other", "supplement", "livelihood", "tencent_ban"):
            self.assertFalse(m[cat], cat)
        self.assertTrue(m["swear"])
        self.assertTrue(m["ad"])

    def test_group_override_respected(self):
        # 按群覆盖：全局开广告审核，群 100 关闭 → 该群 ad 词库失效
        h = _SwitchHarness(
            config={"scan_ad": True},
            overrides={"scan_ad": "false"},
        )
        self.assertFalse(h._lexicon_switch_map(group_id="100")["ad"])
        self.assertTrue(h._lexicon_switch_map(group_id="100")["swear"])
        self.assertTrue(h._lexicon_switch_map()["ad"])


if __name__ == "__main__":
    unittest.main()
