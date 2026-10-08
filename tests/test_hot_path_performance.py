"""消息热路径性能回归测试：查询合并、限时等待、旧值复用、失败退避、权限缓存。"""

import ast
import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    for name in ("astrbot.core", "astrbot.core.platform", "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.aiocqhttp"):
        sys.modules.setdefault(name, types.ModuleType(name))
    aio_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"))
    aio_event.AiocqhttpMessageEvent = object


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
package = types.ModuleType("group_guardian_hot_path_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
single_flight = _load_module(f"{package.__name__}.single_flight", "single_flight.py")
utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")


class _Client:
    """按 action 计数的 OneBot 客户端桩，可配置延迟与失败。"""

    def __init__(self, role="admin", delay=0.0, fail=False):
        self.role = role
        self.delay = delay
        self.fail = fail
        self.calls = {}

    async def call_action(self, action, **kwargs):
        self.calls[action] = self.calls.get(action, 0) + 1
        role = self.role  # 按发起时刻的状态应答，模拟查询期间角色才发生变化
        if self.delay:
            await asyncio.sleep(self.delay)
        if action == "get_login_info":
            return {"user_id": 999}
        if action == "get_group_member_info":
            if self.fail:
                raise RuntimeError("protocol down")
            return {"role": role, "user_id": kwargs.get("user_id")}
        return None


class _GatedClient(_Client):
    """查询发出后挂起，直到测试放行：用事件而非 sleep 控制时序，避免计时粒度导致的偶发失败。"""

    def __init__(self, role):
        super().__init__(role=role)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call_action(self, action, **kwargs):
        self.calls[action] = self.calls.get(action, 0) + 1
        role = self.role
        if action == "get_group_member_info":
            self.started.set()
            await self.release.wait()
            return {"role": role, "user_id": kwargs.get("user_id")}
        if action == "get_login_info":
            return {"user_id": 999}
        return None


class _Storage:
    def __init__(self):
        self.calls = 0

    def is_group_admin_blocked(self, group_id, user_id):
        self.calls += 1
        return False

    def is_group_super_admin(self, group_id, user_id):
        self.calls += 1
        return False

    def get_group_admin_grant(self, group_id):
        self.calls += 1
        return None


class _Host(onebot.OneBotMixin, utilities.UtilitiesMixin):
    def __init__(self, client):
        self._client = client
        self._storage = _Storage()
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 30.0
        self.context = types.SimpleNamespace(astrbot_config={"admin_id": []})

    def _cfg(self, key, default=None, group_id=None):
        return default

    def _get_admin_list(self):
        return []


class SingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callers_share_one_call(self):
        owner = types.SimpleNamespace()
        calls = []

        async def work():
            calls.append(1)
            await asyncio.sleep(0.02)
            return "v"

        tasks = [single_flight.shared_task(owner, "_inflight", "k", work) for _ in range(10)]
        results = [await single_flight.wait_shared(t) for t in tasks]
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [(True, "v")] * 10)
        self.assertEqual(owner._inflight, {})

    async def test_bounded_wait_leaves_task_running(self):
        owner = types.SimpleNamespace()
        done = asyncio.Event()

        async def slow():
            await asyncio.sleep(0.1)
            done.set()
            return "late"

        task = single_flight.shared_task(owner, "_inflight", "k", slow)
        started = time.perf_counter()
        self.assertEqual(await single_flight.wait_shared(task, 0.01), (False, None))
        self.assertLess(time.perf_counter() - started, 0.08)
        await asyncio.wait_for(done.wait(), 1)
        self.assertEqual(task.result(), "late")

    async def test_task_error_is_reported_as_missing_result(self):
        owner = types.SimpleNamespace()

        async def boom():
            raise RuntimeError("x")

        task = single_flight.shared_task(owner, "_inflight", "k", boom)
        self.assertEqual(await single_flight.wait_shared(task), (False, None))


class BotRoleHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_messages_issue_one_lookup(self):
        client = _Client(role="admin", delay=0.02)
        host = _Host(client)
        results = await asyncio.gather(*[host._bot_can_moderate(None, "123") for _ in range(20)])
        self.assertTrue(all(results))
        self.assertEqual(client.calls.get("get_group_member_info"), 1)

    async def test_slow_protocol_does_not_block_message(self):
        client = _Client(role="member", delay=0.5)
        host = _Host(client)
        host.BOT_ROLE_HOT_WAIT = 0.02
        started = time.perf_counter()
        self.assertTrue(await host._bot_can_moderate(None, "123"))  # 未及时返回：放行
        self.assertLess(time.perf_counter() - started, 0.3)
        await asyncio.gather(*list(host._bot_role_inflight.values()))
        self.assertFalse(await host._bot_can_moderate(None, "123"))  # 后台结果已入缓存

    async def test_expired_cache_returns_stale_role_and_refreshes_once(self):
        client = _Client(role="admin", delay=0.05)
        host = _Host(client)
        host._bot_role_cache = {"123": ("member", time.time() - 1000, 300.0)}
        started = time.perf_counter()
        results = await asyncio.gather(*[host._bot_can_moderate(None, "123") for _ in range(5)])
        self.assertLess(time.perf_counter() - started, 0.04)
        self.assertEqual(results, [False] * 5)  # 先沿用旧值，不等协议端
        await asyncio.gather(*list(host._bot_role_inflight.values()))
        self.assertEqual(client.calls.get("get_group_member_info"), 1)
        self.assertTrue(await host._bot_can_moderate(None, "123"))

    async def test_invalidation_during_lookup_discards_stale_result(self):
        # 查询发出后机器人被设为管理员：旧查询返回的 member 不得写入缓存
        client = _GatedClient(role="member")
        host = _Host(client)
        task = asyncio.ensure_future(host._get_bot_group_role(None, "123"))
        await asyncio.wait_for(client.started.wait(), 1)
        host._invalidate_bot_role("123")
        client.role = "admin"
        client.release.set()
        self.assertEqual(await task, "member")
        self.assertNotIn("123", host._bot_role_cache)
        self.assertEqual(await host._get_bot_group_role(None, "123"), "admin")

    async def test_web_lookup_waits_for_result(self):
        client = _Client(role="owner", delay=0.02)
        host = _Host(client)
        self.assertEqual(await host._get_bot_group_role(None, "123"), "owner")


class MemberRoleHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_lookups_are_merged(self):
        client = _Client(role="admin", delay=0.02)
        host = _Host(client)
        roles = await asyncio.gather(*[host._get_member_role(None, "1", "2") for _ in range(10)])
        self.assertEqual(roles, ["admin"] * 10)
        self.assertEqual(client.calls.get("get_group_member_info"), 1)

    async def test_expired_role_is_served_while_refreshing(self):
        client = _Client(role="member", delay=0.05)
        host = _Host(client)
        host._admin_role_cache["1:2"] = ("admin", time.time() - 60)
        started = time.perf_counter()
        self.assertEqual(await host._get_member_role(None, "1", "2"), "admin")
        self.assertLess(time.perf_counter() - started, 0.04)
        await asyncio.gather(*list(host._member_role_inflight.values()))
        self.assertEqual(await host._get_member_role(None, "1", "2"), "member")

    async def test_failure_backoff_stops_request_storm(self):
        client = _Client(fail=True)
        host = _Host(client)
        for _ in range(10):
            self.assertEqual(await host._get_member_role(None, "1", "2"), "")
        self.assertEqual(client.calls.get("get_group_member_info"), 1)

    async def test_too_old_cache_is_not_served(self):
        client = _Client(role="member")
        host = _Host(client)
        host._admin_role_cache["1:2"] = ("admin", time.time() - host.MEMBER_ROLE_STALE_MAX - 1)
        self.assertEqual(await host._get_member_role(None, "1", "2"), "member")

    async def test_demotion_during_lookup_is_not_cached(self):
        client = _GatedClient(role="admin")
        host = _Host(client)
        task = asyncio.ensure_future(host._get_member_role(None, "1", "2"))
        await asyncio.wait_for(client.started.wait(), 1)
        host._invalidate_member_roles("1")
        client.role = "member"
        client.release.set()
        self.assertEqual(await task, "admin")
        self.assertNotIn("1:2", host._admin_role_cache)
        self.assertEqual(await host._get_member_role(None, "1", "2"), "member")

    def test_invalidate_member_roles_is_scoped_to_group(self):
        host = _Host(_Client())
        now = time.time()
        host._admin_role_cache.update({
            "1:2": ("admin", now), "1:3": ("member", now), "10:2": ("admin", now),
            "blocked|1:2": (False, now),
        })
        host._invalidate_member_roles("1")
        self.assertEqual(set(host._admin_role_cache), {"10:2", "blocked|1:2"})


class PermissionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_check_reuses_permission_lookups(self):
        host = _Host(_Client(role="member"))
        event = types.SimpleNamespace(get_sender_id=lambda: "2", group_id="1", bot=None)
        for _ in range(5):
            self.assertFalse(await host._is_admin(event))
        self.assertEqual(host._storage.calls, 2)  # 黑名单 + 群超管各查一次

    async def test_clearing_role_cache_invalidates_permissions(self):
        host = _Host(_Client(role="member"))
        host._is_group_admin_blocked("1", "2")
        host._admin_role_cache.clear()  # 所有权限写入路径都会 clear()
        host._is_group_admin_blocked("1", "2")
        self.assertEqual(host._storage.calls, 2)


class RebuildOffLoopTests(unittest.TestCase):
    def test_full_rebuild_compiles_in_thread(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_background_full_rebuild"
        )
        called = {
            node.func.attr for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("_run_in_thread", called)
        self.assertNotIn("_compile_lexicon", called)
        self.assertNotIn("load_lexicon", called)


if __name__ == "__main__":
    unittest.main()
