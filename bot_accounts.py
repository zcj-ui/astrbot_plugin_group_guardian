"""Account discovery, addressed OneBot calls and first-use profile migration."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


def account_id(value):
    value = str(value or '')
    return value if re.fullmatch(r'[1-9][0-9]{0,19}', value) else ''


def connected_accounts(context):
    accounts = {}
    for platform in context.platform_manager.get_insts():
        if platform.meta().name != 'aiocqhttp':
            continue
        bot = platform.get_client()
        # CQHttp indexes its reverse API sockets by the OneBot X-Self-ID.
        for key in getattr(bot, '_wsr_api_clients', {}):
            qq = account_id(key)
            if qq:
                accounts[qq] = bot
    return accounts


class AccountClient:
    def __init__(self, bot, qq):
        self.bot = bot
        self.qq = qq

    async def call_action(self, action, **params):
        if self.qq not in self.bot._wsr_api_clients:
            raise RuntimeError('所选 Bot 已离线')
        params['self_id'] = self.qq
        return await self.bot.api.call_action(action, **params)


class AccountConfig(dict):
    def __init__(self, path, values):
        super().__init__(values)
        self.path = path

    def save_config(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)


def _snapshot(source, target, *, settings_only=False):
    tmp = target.with_suffix('.tmp')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
            if settings_only:
                keep = {'meta', 'lexicon_categories', 'lexicon_keywords', 'moderation_rules',
                        'join_audit_rules', 'group_admin_grant', 'managed_lists', 'group_super_admins',
                        'group_admin_block', 'group_configs', 'card_protected_members'}
                tables = dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                with dst:
                    for (name,) in tables:
                        if not name.startswith('sqlite_') and name not in keep:
                            dst.execute('DELETE FROM "' + name.replace('"', '""') + '"')
    tmp.replace(target)


def prepare_account(root: Path, qq: str, defaults):
    if not account_id(qq):
        raise ValueError('无效的 Bot QQ 号')
    directory = root / 'bots' / qq
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / 'config.json'
    old_db = root / 'group_guardian.db'
    db = directory / old_db.name
    if old_db.is_file() and not db.exists():
        backup = root / 'before-bot-profiles'
        backup.mkdir(exist_ok=True)
        if not (backup / 'config.json').exists():
            AccountConfig(backup / 'config.json', defaults).save_config()
        if not (backup / db.name).exists():
            _snapshot(old_db, backup / db.name)
        _snapshot(backup / db.name, db, settings_only=True)
    if config_path.is_file():
        config = AccountConfig(config_path, json.loads(config_path.read_text(encoding='utf-8')))
    else:
        config = AccountConfig(config_path, json.loads(json.dumps(defaults)))
        config.save_config()
    return directory, config
