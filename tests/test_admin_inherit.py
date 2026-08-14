"""v2.18.0 管理员继承开关回归测试。

覆盖 onebot.py 的：
- _get_astrbot_admin_ids：读取 AstrBot 全局 admin_id（context.astrbot_config）
- _get_all_admin_ids：inherit_astrbot_admins=true（默认）合并 AstrBot 全局管理员；
  false 时仅认插件管理员名单（消除隐式交叉污染）
- 无 context / 配置读取失败时的降级行为

以及 web.py 静态结构检查（/admin/lists 双名单接口）。
"""

import importlib.util
import sys
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
    core = types.ModuleType("astrbot.core")
    platform = types.ModuleType("astrbot.core.platform")
    sources = types.ModuleType("astrbot.core.platform.sources")
    aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    event_module = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    event_module.AiocqhttpMessageEvent = object
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.platform": platform,
        "astrbot.core.platform.sources": sources,
        "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
    })


def _load_module(filename, alias):
    path = _ROOT / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[alias] = module
    return module


def _load_onebot():
    _stub_astrbot()
    if "platforms" not in sys.modules:
        _load_module("platforms.py", "platforms")
    if "gg_onebot" not in sys.modules:
        _load_module("onebot.py", "gg_onebot")
    return sys.modules["gg_onebot"]


onebot_mod = None


class _Harness(object):
    """OneBotMixin 测试 harness（基类在 setUpClass 中动态组合）。"""

    def __init__(self, plugin_admins, astrbot_admins, inherit):
        self.admin_list = list(plugin_admins)
        self.context = types.SimpleNamespace(
            astrbot_config={"admin_id": list(astrbot_admins)}
        )
        self._inherit = inherit

    def _get_admin_list(self):
        return list(self.admin_list)

    def _cfg(self, key, default=None, group_id=None):
        if key == "inherit_astrbot_admins":
            return self._inherit
        return default


class AdminInheritTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global onebot_mod
        onebot_mod = _load_onebot()
        cls.Harness = type("Harness", (_Harness, onebot_mod.OneBotMixin), {})

    def make(self, plugin=("10001",), astrbot=("90001",), inherit=True):
        return self.Harness(list(plugin), list(astrbot), inherit)

    def test_inherit_default_merges_astrbot(self):
        # 默认 inherit=true：插件管理员 + AstrBot 全局管理员合并为有效管理员
        h = self.make()
        self.assertEqual(h._get_all_admin_ids(), {"10001", "90001"})

    def test_no_inherit_excludes_astrbot(self):
        # inherit=false：仅认插件管理员名单
        h = self.make(inherit=False)
        self.assertEqual(h._get_all_admin_ids(), {"10001"})

    def test_astrbot_admin_ids_getter(self):
        h = self.make(astrbot=("90001", "90002"))
        self.assertEqual(h._get_astrbot_admin_ids(), {"90001", "90002"})

    def test_no_context_returns_empty(self):
        h = self.make()
        h.context = types.SimpleNamespace(astrbot_config=None)
        self.assertEqual(h._get_astrbot_admin_ids(), set())
        # 无 AstrBot 配置时仍保留插件管理员
        self.assertEqual(h._get_all_admin_ids(), {"10001"})

    def test_mixed_duplicates_deduped(self):
        h = self.make(plugin=("10001", "90001"), astrbot=("90001",))
        self.assertEqual(h._get_all_admin_ids(), {"10001", "90001"})


class WebAdminListsStructureTests(unittest.TestCase):
    """web.py /admin/lists 双名单接口静态结构检查。"""

    def test_web_has_admin_lists_handler_and_route(self):
        src = (Path(__file__).resolve().parents[1] / "web.py").read_text(encoding="utf-8")
        self.assertIn("def _web_admin_lists", src)
        self.assertIn('"/admin/lists"', src)
        self.assertIn("plugin_admins", src)
        self.assertIn("astrbot_admins", src)
        self.assertIn("effective_admins", src)

    def test_schema_has_inherit_key(self):
        import json

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("inherit_astrbot_admins", schema)
        self.assertTrue(schema["inherit_astrbot_admins"]["default"])


if __name__ == "__main__":
    unittest.main()
