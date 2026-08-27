import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        tree = ast.parse((ROOT / "web.py").read_text(encoding="utf-8"))
        cls.web_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WebMixin"
        )
        cls.dashboard = (
            ROOT / "pages" / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")
        cls.web_source = (ROOT / "web.py").read_text(encoding="utf-8")

    @classmethod
    def _class_literal(cls, name):
        assignment = next(
            node
            for node in cls.web_class.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        )
        return ast.literal_eval(assignment.value)

    @classmethod
    def _static_return_literal(cls, name):
        function = next(
            node
            for node in cls.web_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        return ast.literal_eval(next(
            node.value for node in function.body if isinstance(node, ast.Return)
        ))

    def test_full_message_moderation_is_optional_and_group_overridable(self):
        setting = self.schema["llm_moderation_always"]
        self.assertEqual(setting["type"], "bool")
        self.assertFalse(setting["default"])

        categories = self._class_literal("_CONFIG_CATEGORIES")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        self.assertEqual(categories["llm_moderation_always"], "审核规则")
        self.assertNotIn("llm_moderation_always", excluded)
        self.assertIn("key: 'llm_moderation_always'", self.dashboard)
        self.assertIn("llm_moderation_always: true", self.dashboard)

        concurrency = self.schema["llm_max_concurrency"]
        self.assertEqual(concurrency["type"], "int")
        self.assertEqual(concurrency["default"], 12)
        self.assertIn("llm_max_concurrency", excluded)
        ranges = self._static_return_literal("_config_int_ranges")
        self.assertEqual(ranges["llm_max_concurrency"], (1, 32))

    def test_card_admin_exemption_is_enabled_and_exposed(self):
        setting = self.schema["card_audit_admin_exempt"]
        self.assertEqual(setting["type"], "bool")
        self.assertTrue(setting["default"])

        keys = self._class_literal("_CARD_MONITOR_KEYS")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        self.assertIn("card_audit_admin_exempt", keys)
        self.assertNotIn("card_audit_admin_exempt", excluded)
        self.assertIn("'card_audit_admin_exempt'", self.dashboard)

    def test_base_decode_audit_is_enabled_and_group_overridable(self):
        setting = self.schema["base_decode_enabled"]
        self.assertEqual("bool", setting["type"])
        self.assertTrue(setting["default"])

        categories = self._class_literal("_CONFIG_CATEGORIES")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        self.assertEqual("审核规则", categories["base_decode_enabled"])
        self.assertNotIn("base_decode_enabled", excluded)
        self.assertIn("key: 'base_decode_enabled'", self.dashboard)
        self.assertIn("base_decode_enabled: true", self.dashboard)

    def test_admin_content_exemption_is_opt_in_and_group_overridable(self):
        setting = self.schema["moderation_admin_exempt"]
        self.assertEqual("bool", setting["type"])
        self.assertFalse(setting["default"])

        categories = self._class_literal("_CONFIG_CATEGORIES")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        self.assertEqual("审核规则", categories["moderation_admin_exempt"])
        self.assertNotIn("moderation_admin_exempt", excluded)
        self.assertIn("key: 'moderation_admin_exempt'", self.dashboard)
        self.assertIn("moderation_admin_exempt: true", self.dashboard)

    def test_moderation_review_is_global_and_exposed_in_builtin_dashboard(self):
        expected = {
            "moderation_review_enabled": ("bool", False),
            "moderation_review_interval": ("int", 86400),
            "moderation_review_min_samples": ("int", 3),
            "llm_moderation_review_guidance": ("text", ""),
        }
        categories = self._class_literal("_CONFIG_CATEGORIES")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        for key, (type_name, default) in expected.items():
            with self.subTest(key=key):
                self.assertEqual(type_name, self.schema[key]["type"])
                self.assertEqual(default, self.schema[key]["default"])
                self.assertEqual("审核复盘", categories[key])
                self.assertIn(key, excluded)

        self.assertIn("AI 误判复盘", self.dashboard)
        self.assertIn('id="btnRunReview"', self.dashboard)
        self.assertIn('id="btnSaveReviewConfig"', self.dashboard)
        for route in (
            "/moderation_review/feedback",
            "/moderation_review/feedback/mark",
            "/moderation_review/run",
            "/moderation_review/suggestions",
            "/moderation_review/suggestions/apply",
            "/moderation_review/suggestions/reject",
            "/moderation_review/suggestions/rollback",
            "/moderation_review/audit",
        ):
            self.assertIn(f'("{route}"', self.web_source)
        self.assertIn("self.context.register_web_api(", self.web_source)

    def test_plugin_has_no_standalone_web_listener(self):
        production = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in list(ROOT.glob("*.py"))
            + list((ROOT / "pages").rglob("*.html"))
        )
        for marker in (
            "0.0.0.0",
            "web.run_app",
            "TCPSite(",
            "HTTPServer(",
            "uvicorn.run",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, production)

    def test_dashboard_relies_on_host_injected_bridge_sdk(self):
        # Keep the SDK reference explicit so the page also works when opened
        # through the plugin-page route in AstrBot versions that do not inject
        # it into the document automatically.
        self.assertIn(
            '<script src="/api/plugin/page/bridge-sdk.js"></script>',
            self.dashboard,
        )
        self.assertIn("var bridge = null", self.dashboard)

    def test_dashboard_literal_api_paths_are_registered(self):
        called = set(re.findall(
            r"safe(?:Get|Post|Download)\(\s*['\"]([^'\"]+)",
            self.dashboard,
        ))
        registered = {
            path.lstrip("/")
            for path in re.findall(
                r'\("(/[^"\\]+)",\s*self\._web_', self.web_source
            )
        }
        self.assertFalse({
            path for path in called
            if path.startswith("/") or "?" in path or "#" in path or ".." in path
        })
        self.assertFalse(called - registered)


if __name__ == "__main__":
    unittest.main()
