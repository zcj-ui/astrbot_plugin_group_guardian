import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "group_guardian_admin_config_tests"

astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
api.logger = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                  warning=lambda *a, **k: None, exception=lambda *a, **k: None)
astrbot.api = api
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg

def load(name, filename):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

automaton = types.ModuleType(f"{PACKAGE}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
event_module = types.ModuleType("astrbot.api.event")
event_module.AstrMessageEvent = object
sys.modules[event_module.__name__] = event_module
api.event = event_module
platform = types.ModuleType("astrbot.core.platform")
sources = types.ModuleType("astrbot.core.platform.sources")
aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
aiocqhttp_event = types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
aiocqhttp_event.AiocqhttpMessageEvent = object
for name, module in (("astrbot.core", types.ModuleType("astrbot.core")),
                     ("astrbot.core.platform", platform),
                     ("astrbot.core.platform.sources", sources),
                     ("astrbot.core.platform.sources.aiocqhttp", aiocqhttp),
                     ("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event", aiocqhttp_event)):
    sys.modules[name] = module
sys.modules["astrbot.core"].platform = platform
platform.sources = sources
sources.aiocqhttp = aiocqhttp
aiocqhttp.aiocqhttp_message_event = aiocqhttp_event
storage = load("storage", "storage.py")
utils = load("utils", "utils.py")
load("constants", "constants.py")
web = load("web", "web.py")
onebot = load("onebot", "onebot.py")

class Request:
    body = {}
    async def get_json(self, **kwargs):
        return self.body

class Config(dict):
    def __init__(self, *args, save_ok=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_ok = save_ok
        self.save_calls = 0
    def save_config(self):
        self.save_calls += 1
        if not self.save_ok:
            raise OSError("save failed")

class Harness(web.WebMixin, utils.UtilitiesMixin):
    def __init__(self, store, config=None):
        self._storage = store
        self.config = Config(config or {})
        self._config_schema = {"admin_list": {"type": "list", "default": []},
                               "moderation_require_bot_admin": {"type": "bool", "default": True}}
        self.admin_list = list(store.load_managed_list("admin"))
        self._admin_role_cache = {"stale": (False, 0)}
        self._group_cfg_cache = {}
        self._group_white_set = set()
        self.group_white_list = []
        self.group_black_list = []
        self.user_black_list = []
        self.user_white_list = []
    def _invalidate_group_cfg_cache(self):
        self._group_cfg_cache.clear()
    def _save_config_safe(self):
        try:
            self.config.save_config()
            return True
        except Exception:
            return False

class GlobalAdminConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.SQLiteStorage(Path(self.temp.name), str(ROOT))
        with self.store._connect() as conn:
            storage.SQLiteStorage._create_tables(conn)
        web.jsonify = lambda value: value
        web.quart_request = Request()
    def tearDown(self): self.temp.cleanup()

    def test_atomic_replace_isolated_to_selected_list_and_rejects_unknown_type(self):
        self.store.seed_managed_list("admin", ["old"])
        self.store.seed_managed_list("group_black", ["group"])
        self.store.replace_managed_list("admin", ["new", "other"])
        self.assertEqual(self.store.load_managed_list("admin"), ["new", "other"])
        self.assertEqual(self.store.load_managed_list("group_black"), ["group"])
        with self.assertRaises(ValueError): self.store.replace_managed_list("unknown", [])

    def test_empty_migrated_admin_list_does_not_resurrect_legacy_config(self):
        self.store.set_meta("lists_migrated", "1")
        h = Harness(self.store, {"admin_list": ["legacy"]})
        h._migrate_and_load_managed_lists()
        self.assertEqual(h.admin_list, [])

    async def test_settings_update_persists_canonical_admin_ids_and_config_reads_same_list(self):
        h = Harness(self.store, {"admin_list": ["stale"]})
        web.quart_request.body = {"admin_list": [" 123 ", 123, "official:abc", ""]}
        response = await h._web_update_config()
        self.assertEqual(response["status"], "success")
        self.assertEqual(self.store.load_managed_list("admin"), ["123", "official:abc"])
        self.assertEqual(h.admin_list, ["123", "official:abc"])
        self.assertEqual(h.config.save_calls, 0)
        config = await h._web_get_config()
        self.assertEqual(config["admin_list"], ["123", "official:abc"])
        self.assertEqual(config["_admin_list"], ["123", "official:abc"])
        fresh = Harness(self.store, {"admin_list": ["stale"]})
        fresh._migrate_and_load_managed_lists()
        self.assertEqual(fresh.admin_list, ["123", "official:abc"])

    async def test_mixed_setting_and_admin_update_persists_both(self):
        h = Harness(self.store, {"moderation_require_bot_admin": True})
        web.quart_request.body = {
            "admin_list": ["next"],
            "moderation_require_bot_admin": False,
        }
        response = await h._web_update_config()
        self.assertEqual(response["status"], "success")
        self.assertIn("moderation_require_bot_admin", response["updated"])
        self.assertIn("admin_list", response["updated"])
        self.assertEqual(h.config["moderation_require_bot_admin"], False)
        self.assertEqual(self.store.load_managed_list("admin"), ["next"])

    def test_admin_normalizer_accepts_platform_ids_and_rejects_malformed_values(self):
        self.assertEqual(
            utils.UtilitiesMixin._normalize_admin_list(" a，b,a "), ["a", "b"]
        )
        self.assertEqual(utils.UtilitiesMixin._normalize_admin_list([]), [])
        for invalid in (None, True, 1.5, {}, [None], [["nested"]], ["null"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                utils.UtilitiesMixin._normalize_admin_list(invalid)

    async def test_invalid_ids_fail_without_writing_normal_config_or_admins(self):
        self.store.seed_managed_list("admin", ["prior"])
        h = Harness(self.store, {"moderation_require_bot_admin": True})
        web.quart_request.body = {"admin_list": [True], "moderation_require_bot_admin": False}
        self.assertEqual((await h._web_update_config())["status"], "error")
        self.assertEqual(h.config["moderation_require_bot_admin"], True)
        self.assertEqual(self.store.load_managed_list("admin"), ["prior"])
        self.assertEqual(h.admin_list, ["prior"])

    async def test_storage_failure_keeps_runtime_and_returns_partial_save_error(self):
        h = Harness(self.store, {"moderation_require_bot_admin": True})
        self.store.replace_managed_list = lambda *_args: (_ for _ in ()).throw(OSError("db down"))
        web.quart_request.body = {"admin_list": ["valid"], "moderation_require_bot_admin": False}
        response = await h._web_update_config()
        self.assertEqual(response["status"], "error")
        self.assertIn("moderation_require_bot_admin", response["updated"])
        self.assertEqual(h.admin_list, [])
        self.assertEqual(h._admin_role_cache, {"stale": (False, 0)})
        self.assertIn("其他设置已保存", response["message"])

    async def test_config_failure_prevents_admin_mutation(self):
        self.store.seed_managed_list("admin", ["prior"])
        h = Harness(self.store, {"moderation_require_bot_admin": True})
        h.config.save_ok = False
        web.quart_request.body = {"admin_list": ["next"], "moderation_require_bot_admin": False}
        response = await h._web_update_config()
        self.assertEqual(response["status"], "error")
        self.assertEqual(self.store.load_managed_list("admin"), ["prior"])
        self.assertEqual(h.admin_list, ["prior"])
        self.assertEqual(h.config["moderation_require_bot_admin"], True)

    async def test_missing_admin_field_leaves_canonical_list_untouched(self):
        self.store.seed_managed_list("admin", ["current"])
        h = Harness(self.store)
        web.quart_request.body = {"moderation_require_bot_admin": False}
        response = await h._web_update_config()
        self.assertEqual(response["status"], "success")
        self.assertEqual(self.store.load_managed_list("admin"), ["current"])
        self.assertEqual(h.admin_list, ["current"])

    def test_first_migration_imports_legacy_admin_ids(self):
        h = Harness(self.store, {"admin_list": ["legacy-id"]})
        h._migrate_and_load_managed_lists()
        self.assertEqual(h.admin_list, ["legacy-id"])
        self.assertEqual(self.store.load_managed_list("admin"), ["legacy-id"])

    async def test_remove_final_admin_stays_empty_after_reload(self):
        self.store.set_meta("lists_migrated", "1")
        self.store.seed_managed_list("admin", ["123"])
        h = Harness(self.store, {"admin_list": ["123"]})
        web.quart_request.body = {"user_id": 123}
        response = await h._web_admin_remove()
        self.assertEqual(response["status"], "success")
        self.assertEqual(h.admin_list, [])
        fresh = Harness(self.store, {"admin_list": ["123"]})
        fresh._migrate_and_load_managed_lists()
        self.assertEqual(fresh.admin_list, [])

    async def test_dedicated_admin_api_preserves_integer_user_ids(self):
        h = Harness(self.store)
        web.quart_request.body = {"user_id": 456}
        self.assertEqual((await h._web_admin_add())["status"], "success")
        self.assertEqual(h.admin_list, ["456"])

    async def test_storage_trigger_failure_rolls_back_replacement(self):
        self.store.seed_managed_list("admin", ["prior"])
        with self.store._connect() as conn:
            conn.execute("""CREATE TRIGGER reject_admin BEFORE INSERT ON managed_lists
                WHEN NEW.list_type='admin' AND NEW.value='reject'
                BEGIN SELECT RAISE(ABORT, 'rejected'); END""")
        with self.assertRaises(Exception):
            self.store.replace_managed_list("admin", ["next", "reject"])
        self.assertEqual(self.store.load_managed_list("admin"), ["prior"])
        self.store.seed_managed_list("group_black", ["unaffected"])
        self.assertEqual(self.store.load_managed_list("group_black"), ["unaffected"])

    async def test_permissions_use_global_ids_across_groups_but_preserve_block_precedence(self):
        class Event:
            def __init__(self, uid, gid): self.uid, self.gid = uid, gid
            def get_sender_id(self): return self.uid
            def get_group_id(self): return self.gid

        class Permissions(onebot.OneBotMixin, utils.UtilitiesMixin):
            def __init__(self, store, admins, astrbot_admins=()):
                self._storage = store
                self.admin_list = list(admins)
                self.context = types.SimpleNamespace(astrbot_config={"admin_id": list(astrbot_admins)})
                self._admin_role_cache = {}

        h = Permissions(self.store, ["123"], ["core"])
        self.assertTrue(await h._is_admin(Event("123", "group-a")))
        self.assertTrue(await h._is_admin(Event("core", "group-b")))
        self.assertTrue(await h._is_plugin_admin(Event("123", "group-b")))
        self.assertTrue(await h._is_plugin_admin(Event("core", "group-a")))
        self.assertFalse(await h._is_plugin_admin(Event("member", "group-a")))
        self.store.add_group_admin_block("group-a", "123")
        h._admin_role_cache.clear()
        self.assertFalse(await h._is_admin(Event("123", "group-a")))
        self.assertFalse(await h._is_plugin_admin(Event("123", "group-a")))

if __name__ == "__main__": unittest.main()
