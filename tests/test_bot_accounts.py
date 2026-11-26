"""Exercise the registered dashboard and event paths with two QQ accounts."""
import asyncio
import importlib
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
NS = types.SimpleNamespace


def load_plugin():
    def module(name, **attrs):
        mod = types.ModuleType(name)
        vars(mod).update(attrs)
        return mod

    def decorator(*args, **kwargs):
        return lambda fn: fn

    class Star:
        def __init__(self, context):
            self.context = context

    filters = NS(command=decorator, llm_tool=decorator, on_llm_request=decorator,
                 platform_adapter_type=decorator, event_message_type=decorator,
                 PlatformAdapterType=NS(AIOCQHTTP=1), EventMessageType=NS(ALL=1, PRIVATE_MESSAGE=2))
    event_module = 'astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event'
    stubs = {m.__name__: m for m in [
        module('guardian_accounts_test', __path__=[str(ROOT)]),
        module('astrbot.api', logger=logging.getLogger('guardian-test')),
        module('astrbot.api.event', AstrMessageEvent=object, filter=filters),
        module('astrbot.api.provider', ProviderRequest=object),
        module('astrbot.api.star', Context=object, Star=Star, StarTools=NS(), register=decorator),
        module('astrbot.core.agent.message', TextPart=object),
        module('astrbot.core.config.astrbot_config', AstrBotConfig=dict),
        module(event_module, AiocqhttpMessageEvent=type('QQEvent', (), {})),
    ]}
    with patch.dict(sys.modules, stubs):
        app = importlib.import_module('guardian_accounts_test.main')
        web = sys.modules['guardian_accounts_test.web']
    return app, web


class BotAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app, self.web = load_plugin()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app.StarTools.get_data_dir = lambda: self.root
        self.sock = NS(_wsr_api_clients={'111': object(), '222': object()},
                       api=NS(call_action=AsyncMock(return_value={'nickname': 'Bot'})))
        adapter = NS(meta=lambda: NS(name='aiocqhttp'), get_client=lambda: self.sock)
        self.routes = {}

        def register(path, handler, methods, description):
            self.routes[(path.split('/', 2)[2], methods[0])] = handler

        self.context = NS(platform_manager=NS(get_insts=lambda: [adapter]), register_web_api=register)
        self.defaults = {'enabled': True, 'disclaimer_agreed': True}
        self.host = self.app.Main(self.context, self.defaults)
        self.web.jsonify = lambda obj: obj
        self.addAsyncCleanup(self.close_host)

    async def close_host(self):
        await self.host.terminate()

    def event(self, qq, gid='999'):
        event = self.app.AiocqhttpMessageEvent()
        event.get_platform_name = lambda: 'aiocqhttp'
        event.get_self_id = lambda: qq
        event.group_id = gid
        return event

    async def request(self, path, method, qq='', **body):
        self.web.quart_request = NS(method=method, args={'_bot_id': qq},
                                    get_json=AsyncMock(return_value=dict(body, _bot_id=qq)))
        return await self.routes[path, method]()

    async def test_two_accounts_same_group_and_restart(self):
        self.assertEqual(len((await self.request('accounts', 'GET'))['data']), 2)
        await self.request('whitelist/add', 'POST', '111', group_id='999')
        result = await self.request('config', 'POST', '222', enabled=False)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual((await self.request('config', 'GET', '111'))['_white_list'], ['999'])
        self.assertEqual((await self.request('config', 'GET', '222'))['_white_list'], [])
        observed = []

        async def moderation(runtime, event):
            observed.append((runtime.qq, event.group_id))
            yield runtime.qq

        with patch.object(self.app.ModerationMixin, '_handle_message', moderation):
            async def hear(qq, gid):
                return [v async for v in self.host._handle_message(self.event(qq, gid))]
            self.assertEqual(await asyncio.gather(hear('111','999'), hear('222','999'), hear('111','888')),
                             [['111'], [], []])
            self.assertEqual(observed, [('111', '999')])
            await self.host.terminate()
            self.host = self.app.Main(self.context, self.defaults)
            self.assertEqual(await hear('222', '999'), [])
            self.assertEqual(await hear('111', '999'), ['111'])

    async def test_client_never_uses_other_socket(self):
        account = await self.host._account('222')
        client = await account._get_client()
        await client.call_action('get_group_list')
        self.sock.api.call_action.assert_awaited_once_with('get_group_list', self_id='222')
        del self.sock._wsr_api_clients['222']
        with self.assertRaises(RuntimeError):
            await client.call_action('get_group_list')
        self.assertIsNone(await account._get_client())
        self.assertEqual((await self.request('remote/execute', 'POST', '222'))['status'], 'error')
        self.assertEqual((await self.request('config', 'POST', '', enabled=False))['status'], 'error')
        self.assertEqual((await self.request('config', 'POST', '333', enabled=False))['status'], 'error')
        self.sock.api.call_action.assert_awaited_once()

    async def test_same_group_admin_cache_uses_each_account(self):
        def respond(action, **params):
            if action == 'get_login_info':
                return {'user_id': params['self_id']}
            self.assertEqual(action, 'get_group_member_info')
            self.assertEqual(str(params['user_id']), params['self_id'])
            return {'role': 'admin' if params['self_id'] == '111' else 'member'}

        self.sock.api.call_action.side_effect = respond
        a, b = await asyncio.gather(self.host._account('111'), self.host._account('222'))
        for _ in range(2):
            roles = await asyncio.gather(a._get_bot_group_role(None, '999'), b._get_bot_group_role(None, '999'))
            self.assertEqual(roles, ['admin', 'member'])
        self.assertEqual(self.sock.api.call_action.await_count, 4)

    async def test_legacy_settings_copied_without_replaying_tasks(self):
        source = self.root / 'group_guardian.db'
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('CREATE TABLE group_configs(group_id TEXT, key TEXT, value TEXT)')
            db.execute("INSERT INTO group_configs VALUES ('999','anti_flood_enabled','false')")
            db.execute('CREATE TABLE scheduled_unbans(id INTEGER)')
            db.execute('INSERT INTO scheduled_unbans VALUES (1)')
        for qq in ('111', '222'):
            directory, config = self.app.prepare_account(self.root, qq, self.defaults)
            with closing(sqlite3.connect(directory / source.name)) as db:
                self.assertEqual(db.execute('SELECT value FROM group_configs').fetchone()[0], 'false')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM scheduled_unbans').fetchone()[0], 0)
        for old in (source, self.root / 'before-bot-profiles' / source.name):
            with closing(sqlite3.connect(old)) as db:
                self.assertEqual(db.execute('SELECT id FROM scheduled_unbans').fetchone()[0], 1)
