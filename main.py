# -*- coding: utf-8 -*-
import asyncio
from collections import deque
from typing import Dict, Tuple

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .commands import CommandsMixin
from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .llm_tools import LlmToolsMixin
from .moderation import ModerationMixin
from .onebot import OneBotMixin
from .patterns import AD_PATTERNS, SWEAR_PATTERNS
from .storage import SQLiteStorage
from .utils import UtilitiesMixin
from .web import WebMixin


class Main(CommandsMixin, ModerationMixin, LlmToolsMixin, WebMixin, OneBotMixin, UtilitiesMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._config_schema = self._load_config_schema()
        self._sync_astrbot_admins()
        self._client = None
        self._data_dir = StarTools.get_data_dir()
        self._storage = SQLiteStorage(self._data_dir, self._get_plugin_dir())
        self._storage.initialize()
        _gwl = self.config.get("group_white_list", [])
        self.group_white_list = [str(g).strip() for g in (_gwl if isinstance(_gwl, list) else [_gwl]) if g]
        self._group_white_set = set(self.group_white_list)
        _gbl = self.config.get("group_black_list", [])
        self.group_black_list = [str(g).strip() for g in (_gbl if isinstance(_gbl, list) else [_gbl]) if g]
        self._group_black_set = set(self.group_black_list)
        _ubl = self.config.get("user_black_list", [])
        self.user_black_list = [str(u).strip() for u in (_ubl if isinstance(_ubl, list) else [_ubl]) if u]
        self._user_black_set = set(self.user_black_list)
        self.auto_moderate_enabled = self.config.get("auto_moderate_enabled", True)
        self._compiled_swear = self._build_combined_regex(SWEAR_PATTERNS)
        self._compiled_ad = self._build_combined_regex(AD_PATTERNS)
        self._lexicon = self._load_lexicon()
        self._compiled_lexicon = self._compile_lexicon()
        self._moderation_logs = deque(self._load_logs(), maxlen=500)
        self._next_log_id = max(self._init_next_log_id(), self._storage.max_log_id() + 1)
        self._last_log_save = 0.0
        self._log_save_task = None
        self._admin_role_cache: Dict[str, Tuple[bool, float]] = {}
        self._admin_role_cache_ttl = 300.0
        self._stats_cache = {"today_start": 0, "blocked": 0, "passed": 0, "total": 0, "group_stats": {}, "user_stats": {}}
        self._llm_semaphore = asyncio.Semaphore(5)
        self._register_web_apis()

    async def terminate(self):
        logger.info("[GroupMgr] 插件卸载，SQLite 存储已自动持久化")


_DECORATED_METHOD_MIXINS = (CommandsMixin, ModerationMixin, LlmToolsMixin)
for _mixin in _DECORATED_METHOD_MIXINS:
    for _name, _value in _mixin.__dict__.items():
        if callable(_value) and (
            hasattr(_value, "__decorated__")
            or hasattr(_value, "__decorated_event__")
            or hasattr(_value, "__decorated_platform__")
        ):
            setattr(Main, _name, _value)


Main = register(
    PLUGIN_NAME,
    "zhaisir",
    "QQ群智能守护者 - AI审核+群管工具集",
    PLUGIN_VERSION,
    "https://github.com/zcj-ui/astrbot_plugin_group_guardian",
)(Main)
