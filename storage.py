# -*- coding: utf-8 -*-
import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from astrbot.api import logger

try:
    from .lexicon_migration import ensure_swear_expansion
    from .moderation_review_storage import ModerationReviewStorageMixin
    from .storage_group import GroupStorageMixin
except ImportError:  # 允许直接运行 storage.py 的离线迁移/测试脚本
    from lexicon_migration import ensure_swear_expansion
    from moderation_review_storage import ModerationReviewStorageMixin
    from storage_group import GroupStorageMixin


class SQLiteStorage(ModerationReviewStorageMixin, GroupStorageMixin):
    # 持久化层统一使用 SQLite。_connect() 是 contextmanager，进入时创建连接并开启 WAL，退出时自动关闭。
    # 审核日志按 message_id + group_id + user_id + time 组合键去重。
    # seed_lexicon_db 是发布时打包进插件的内置词库，只在首次初始化时复制到 data 目录。
    def __init__(self, data_dir: Path, plugin_dir: str):
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.db_path = self.data_dir / "group_guardian.db"
        self.seed_lexicon_db_path = self.plugin_dir / "lexicon.db"
        self.legacy_logs_path = self.data_dir / "moderation_logs.json"
        # v2.16.0 统计查询带 TTL 的内存缓存：key -> (monotonic_ts, result)
        self._query_cache: dict = {}

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._create_tables(conn)
        self._ensure_seed_lexicon()
        self._ensure_seed_rules()
        # 词库扩展使用 meta 版本键幂等执行：新安装会导入 seed，已有运行库
        # 也会补齐新增辱骂变体，不再依赖 moderation_rules 为空这一旧条件。
        try:
            with self._connect() as conn:
                stats = ensure_swear_expansion(conn)
            if stats.get("inserted"):
                logger.info(
                    f"[GroupMgr] 已应用辱骂词库扩展: 新增 {stats['inserted']} 条，"
                    f"当前共 {stats['total_swear_rules']} 条"
                )
        except Exception as e:
            # 词库扩展失败不应阻止插件启动；原有 seed 规则仍可继续工作。
            logger.warning(f"[GroupMgr] 应用辱骂词库扩展失败: {e}")

    def db_mtime(self) -> float:
        try:
            return self.db_path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _positive_ints(values: Iterable[int]) -> List[int]:
        ids: List[int] = []
        for value in values or []:
            try:
                item = int(value)
            except (TypeError, ValueError):
                continue
            if item > 0:
                ids.append(item)
        return ids

    @staticmethod
    def _non_empty_strings(values: Iterable[object]) -> List[str]:
        items: List[str] = []
        seen = set()
        for value in values or []:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    # v2.16.0 统计查询 TTL 缓存：报表类 30s、群活跃度 10s、违规积分计数 5s（写日志时主动失效）
    _QUERY_CACHE_TTL_STATS = 30.0
    _QUERY_CACHE_TTL_ACTIVITY = 10.0
    _QUERY_CACHE_TTL_VIOLATION = 5.0
    _QUERY_CACHE_MAX_ENTRIES = 256

    def _query_cached(self, key: str, ttl: float, fn, *args, **kwargs):
        """带 TTL 的统计查询缓存：命中直接返回，未命中执行 fn 后缓存。失败不缓存（下次重试）。"""
        now = time.monotonic()
        entry = self._query_cache.get(key)
        if entry is not None and now - entry[0] < ttl:
            return entry[1]
        result = fn(*args, **kwargs)
        self._query_cache[key] = (now, result)
        if len(self._query_cache) > self._QUERY_CACHE_MAX_ENTRIES:
            expired = [k for k, v in self._query_cache.items() if now - v[0] > ttl]
            for k in expired:
                self._query_cache.pop(k, None)
        return result

    def invalidate_query_cache(self, prefix: str = "") -> None:
        """清除全部或指定前缀的统计缓存（写日志/数据变更时调用）。"""
        if not self._query_cache:
            return
        if prefix:
            keys = [k for k in self._query_cache if k.startswith(prefix)]
            for k in keys:
                self._query_cache.pop(k, None)
        else:
            self._query_cache.clear()

    async def run_in_thread(self, func, *args, **kwargs):
        """在事件循环外的线程中执行同步 DB 操作，避免阻塞插件事件循环。

        v2.21.0：改用 ``loop.run_in_executor`` 实现（Python 3.8 兼容，
        ``asyncio.to_thread`` 为 3.9+，在 3.8 环境会 AttributeError）。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    @contextmanager
    def _connect(self):
        # 使用 contextmanager 确保连接在退出 with 块时总是通过 finally 关闭，防止泄漏。
        # timeout=5：数据库被外部锁定时最多等 5 秒抛异常，避免永久阻塞事件循环（扫描#38 S5）
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # synchronous 是连接级设置、不随 WAL 持久化到库文件；_create_tables 里那次设置
        # 只对建表连接生效，之后每个 _connect() 都会回落到默认 FULL。这里每连接显式设为
        # NORMAL，配合已持久化的 WAL 模式，减少高频审核日志写入的 fsync 开销。
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
        # 表名/列名只允许字母数字下划线，防止将来动态传入时被注入
        import re as _re
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
            raise ValueError(f"非法表名或列名: {table}.{column}")
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _create_tables(conn) -> None:
        # WAL 模式提升并发读性能，NORMAL 同步策略在 crash 后仍可恢复。
        # 三组表：meta（键值对存储）、moderation_logs（审核日志，带时间/群号/用户/操作索引）、lexicon（分类+关键词二级表，级联删除）。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_logs ("
            "id INTEGER PRIMARY KEY, "
            "time TEXT, "
            "ts INTEGER, "
            "group_id TEXT, "
            "user_id TEXT, "
            "user_name TEXT, "
            "msg_text TEXT, "
            "msg_preview TEXT, "
            "action TEXT, "
            "reason TEXT, "
            "image_urls TEXT"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON moderation_logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_group ON moderation_logs(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON moderation_logs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_action ON moderation_logs(action)")
        ModerationReviewStorageMixin._create_moderation_review_tables(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lexicon_categories ("
            "name TEXT PRIMARY KEY, "
            "description TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lexicon_keywords ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT NOT NULL, "
            "keyword TEXT NOT NULL, "
            "UNIQUE(category, keyword), "
            "FOREIGN KEY(category) REFERENCES lexicon_categories(name) ON DELETE CASCADE"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lexicon_category ON lexicon_keywords(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lexicon_keyword ON lexicon_keywords(keyword)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT NOT NULL, "
            "pattern TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "description TEXT, "
            "UNIQUE(category, pattern)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_category ON moderation_rules(category)")
        # ===== v2.4.0 新增表 =====
        # F1 入群审核规则（按群，group_id='default' 为全局兜底）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS join_audit_rules ("
            "group_id TEXT PRIMARY KEY, "
            "accept_keywords TEXT, "
            "reject_keywords TEXT, "
            "default_action TEXT, "
            "reject_reason TEXT, "
            "enabled INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        # F2 刷屏申诉会话状态机
        conn.execute(
            "CREATE TABLE IF NOT EXISTS appeals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "reason TEXT, "
            "penalty TEXT, "
            "mute_duration INTEGER, "
            "status TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "expire_at INTEGER NOT NULL, "
            "decided_at INTEGER, "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "prompt_sent INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        SQLiteStorage._ensure_column(conn, "appeals", "attempts", "INTEGER NOT NULL DEFAULT 0")
        SQLiteStorage._ensure_column(conn, "appeals", "prompt_sent", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appeals_user_status ON appeals(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appeals_expire ON appeals(expire_at)")
        # F3 定时解禁计划
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_unbans ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "unban_at INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "retry_count INTEGER NOT NULL DEFAULT 0, "
            "next_retry_at INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT DEFAULT '', "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "retry_count", "INTEGER NOT NULL DEFAULT 0"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "next_retry_at", "INTEGER NOT NULL DEFAULT 0"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "last_error", "TEXT DEFAULT ''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unban_at ON scheduled_unbans(unban_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_unban_retry ON scheduled_unbans(next_retry_at, unban_at)"
        )
        # F5 群管理员动态授权（按群）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_admin_grant ("
            "group_id TEXT PRIMARY KEY, "
            "grant_owner INTEGER NOT NULL DEFAULT 1, "
            "grant_admin INTEGER NOT NULL DEFAULT 1, "
            "enabled INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        # 配置迁移：单群管理类名单（群白/群黑/用户黑/用户白/管理员）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS managed_lists ("
            "list_type TEXT NOT NULL, "
            "value TEXT NOT NULL, "
            "UNIQUE(list_type, value)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_managed_lists_type ON managed_lists(list_type)")
        # F5 增强：群超管（某群专属的插件管理员，仅在该群生效，WebUI 单独设置）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_super_admins ("
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_super_admins_group ON group_super_admins(group_id)")
        # F5 增强：群级 bot 权限黑名单（群主可移除本群某群管的 bot 管理权限，优先级最高）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_admin_block ("
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_block_group ON group_admin_block(group_id)")
        # 多群独立配置：每个群对任意配置项的覆盖值（value 存字符串，读取时按类型解析）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_configs ("
            "group_id TEXT NOT NULL, "
            "key TEXT NOT NULL, "
            "value TEXT, "
            "UNIQUE(group_id, key)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_group_configs_group ON group_configs(group_id)")

        # 名片监控：群成员名片变更 / 管理员任免的通知事件日志
        conn.execute(
            "CREATE TABLE IF NOT EXISTS card_change_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "time TEXT NOT NULL, "
            "kind TEXT NOT NULL, "           # 'card' 名片变更 / 'admin' 管理员任免
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "user_name TEXT DEFAULT '', "
            "old_value TEXT DEFAULT '', "     # card: 旧名片；admin: ''
            "new_value TEXT DEFAULT '', "     # card: 新名片；admin: set/unset
            "action TEXT DEFAULT ''"          # 记录/还原/违规还原 等处理结果
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_logs_ts ON card_change_logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_logs_group ON card_change_logs(group_id)")

        # 名片保护名单：被保护成员的名片被改后自动还原为 protected_card
        conn.execute(
            "CREATE TABLE IF NOT EXISTS card_protected_members ("
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "protected_card TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_protected_group ON card_protected_members(group_id)")

        # 自适应上下文学习：AI 从群聊上下文挖掘的候选违禁词（按群独立、需审批后生效）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS learned_keywords ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id TEXT NOT NULL, "
            "keyword TEXT NOT NULL, "
            "category TEXT NOT NULL DEFAULT 'ad', "   # ad / swear
            "status TEXT NOT NULL DEFAULT 'pending', " # pending / approved / rejected
            "reason TEXT DEFAULT '', "                 # LLM 给出的判定理由
            "sample TEXT DEFAULT '', "                 # 触发样例文本
            "confidence REAL DEFAULT 0, "              # LLM 置信度 0-1
            "occurrences INTEGER DEFAULT 1, "          # 跨轮累计出现次数
            "source TEXT DEFAULT 'llm', "              # llm / manual
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "UNIQUE(group_id, keyword)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_learned_group_status ON learned_keywords(group_id, status)")
        # 群活跃度统计（v2.13.0）：记录每群每条发言，供日活/周活/月活报表
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_activity ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "user_name TEXT DEFAULT ''"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_group_ts ON group_activity(group_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_ts ON group_activity(ts)")
        # WebUI 远程操作审计日志（v2.15.0）：记录操作者身份、目标群、操作与结果
        conn.execute(
            "CREATE TABLE IF NOT EXISTS web_audit_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "time TEXT, "
            "operator_name TEXT DEFAULT '', "
            "operator_qq TEXT DEFAULT '', "
            "group_id TEXT DEFAULT '', "
            "action TEXT DEFAULT '', "
            "target_user TEXT DEFAULT '', "
            "params TEXT DEFAULT '', "
            "result TEXT DEFAULT '', "
            "message TEXT DEFAULT ''"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_audit_ts ON web_audit_logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_audit_operator ON web_audit_logs(operator_qq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_audit_group ON web_audit_logs(group_id)")
        # v2.16.0 高频查询组合索引：
        # 违规积分 COUNT（WHERE group_id=? AND user_id=? AND ts>=?）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_group_user_ts ON moderation_logs(group_id, user_id, ts)")
        # 群活跃用户排行（WHERE group_id=? AND ts>=? GROUP BY user_id）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_group_ts_user ON group_activity(group_id, ts, user_id)")
        # 按群倒序查 Web 审计日志（WHERE group_id=? ORDER BY ts DESC）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_audit_group_ts ON web_audit_logs(group_id, ts)")
        # v2.19.0 WebUI 远程操作安全增强：
        # 1) 审计日志补充「操作人IP / 修改前值 / 修改后值」（旧库自动补列，幂等）
        SQLiteStorage._ensure_column(conn, "web_audit_logs", "operator_ip", "TEXT DEFAULT ''")
        SQLiteStorage._ensure_column(conn, "web_audit_logs", "before_value", "TEXT DEFAULT ''")
        SQLiteStorage._ensure_column(conn, "web_audit_logs", "after_value", "TEXT DEFAULT ''")
        # 2) 双管理员审批：高敏感远程操作先落 pending，由第二名管理员确认后执行
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_web_operations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "expire_at INTEGER NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "   # pending / approved / rejected / expired / failed
            "operator_name TEXT DEFAULT '', "           # 发起人（第一名管理员）
            "operator_qq TEXT DEFAULT '', "
            "operator_ip TEXT DEFAULT '', "
            "group_id TEXT DEFAULT '', "
            "action TEXT DEFAULT '', "
            "params TEXT DEFAULT '', "
            "approver_name TEXT DEFAULT '', "           # 确认人（第二名管理员）
            "approver_qq TEXT DEFAULT '', "
            "approver_ip TEXT DEFAULT '', "
            "executed INTEGER NOT NULL DEFAULT 0, "      # 确认后是否已执行
            "result TEXT DEFAULT ''"                    # 执行结果 / 失败原因
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_web_status ON pending_web_operations(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_web_ts ON pending_web_operations(ts)")
        # 旧库补充 result 列（执行失败原因，幂等）
        SQLiteStorage._ensure_column(conn, "pending_web_operations", "result", "TEXT DEFAULT ''")
        # v2.23.0 不确定视频广告管理员复核队列：
        # 视频广告检测判定为「疑似广告」时先落待复核，由管理员确认违规或放行。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_ad_reviews ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "user_name TEXT DEFAULT '', "
            "msg_text TEXT DEFAULT '', "
            "msg_preview TEXT DEFAULT '', "
            "fingerprint TEXT DEFAULT '', "
            "source TEXT DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'pending', "   # pending / confirmed / cleared
            "reviewed_by TEXT DEFAULT '', "
            "reviewed_at INTEGER DEFAULT 0"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_reviews_status ON video_ad_reviews(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_reviews_ts ON video_ad_reviews(ts)")
        # v2.32.0 不确定内容（文本/图片 LLM 无法确认）管理员复核队列：
        # 私信该群全部管理员重新审核，管理员私聊/管理群回复确认或放行。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS uncertain_reviews ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "user_name TEXT DEFAULT '', "
            "msg_text TEXT DEFAULT '', "
            "msg_preview TEXT DEFAULT '', "
            "source TEXT DEFAULT '', "          # text / image
            "status TEXT NOT NULL DEFAULT 'pending', "   # pending / confirmed / cleared
            "reviewed_by TEXT DEFAULT '', "
            "reviewed_at INTEGER DEFAULT 0"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uncertain_reviews_status ON uncertain_reviews(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uncertain_reviews_ts ON uncertain_reviews(ts)")
        # v2.36.0 通用疑似广告人工复核队列（adguard 合并）：
        # 所有疑似广告（文本/图片/视频/名片）先不处罚，私聊插件管理员确认后再
        # 撤回+禁言+学习；msg_id 用于确认后撤回原消息，image_urls 存图片证据。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ad_reviews ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "user_name TEXT DEFAULT '', "
            "msg_text TEXT DEFAULT '', "
            "msg_id TEXT DEFAULT '', "
            "image_urls TEXT DEFAULT '', "
            "source TEXT DEFAULT '', "          # text / image / video / card
            "status TEXT NOT NULL DEFAULT 'pending', "   # pending / confirmed / released
            "reviewed_by TEXT DEFAULT '', "
            "reviewed_at INTEGER DEFAULT 0"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_reviews_status ON ad_reviews(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_reviews_ts ON ad_reviews(ts)")
        # v2.36.0 广告文本指纹学习库：管理员确认广告后学习文本指纹，
        # 下次相似（归一化相同）内容直接撤回+禁言，无需再次人工确认。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ad_text_fingerprints ("
            "fingerprint TEXT PRIMARY KEY, "
            "verdict TEXT NOT NULL, "           # ad / ok（放行）
            "group_id TEXT DEFAULT '', "
            "text_preview TEXT DEFAULT '', "
            "ts INTEGER NOT NULL"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_fp_verdict ON ad_text_fingerprints(verdict)")
        conn.commit()

    def _ensure_seed_lexicon(self) -> None:
        # 先 count 再读文件：已有词条则跳过，避免每次启动都重复打开 seed DB。
        if self.count_lexicon_keywords() > 0:
            return
        if self.seed_lexicon_db_path.exists():
            imported = self.import_lexicon_db(self.seed_lexicon_db_path)
            if imported:
                logger.info(f"[GroupMgr] 已从 lexicon.db 导入词库: {imported} 条")

    def _ensure_seed_rules(self) -> None:
        # 从内置 lexicon.db 读取 moderation_rules 表导入到运行库，已有则跳过。
        if self.count_moderation_rules() > 0:
            return
        if not self.seed_lexicon_db_path.exists():
            return
        try:
            with closing(sqlite3.connect(str(self.seed_lexicon_db_path))) as seed:
                seed.row_factory = sqlite3.Row
                rows = seed.execute(
                    "SELECT category, pattern FROM moderation_rules ORDER BY id"
                ).fetchall()
            if not rows:
                return
            rules: Dict[str, List[str]] = {}
            for r in rows:
                cat = r["category"]
                if cat not in rules:
                    rules[cat] = []
                rules[cat].append(r["pattern"])
            self.seed_moderation_rules(rules)
        except Exception as e:
            logger.warning(f"[GroupMgr] 从 lexicon.db 导入正则规则失败: {e}")

    def seed_moderation_rules(self, rules: Dict[str, List[str]]) -> None:
        # 将正则规则写入 moderation_rules 表，已有则不重复导入。
        if self.count_moderation_rules() > 0:
            return
        with self._connect() as conn:
            for category, patterns in rules.items():
                for pattern in patterns:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO moderation_rules(category, pattern) VALUES(?, ?)",
                            (category, pattern),
                        )
                    except Exception:
                        logger.debug(f"[GroupMgr] 跳过无效规则 [{category}]: {pattern[:50]}")
            conn.commit()
        logger.info(f"[GroupMgr] 已导入 {len(rules)} 类正则规则到数据库")

    def load_moderation_rules(self, category: str = "") -> List[str]:
        # 从 moderation_rules 表按分类加载已启用的正则 pattern。
        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT pattern FROM moderation_rules WHERE category=? AND enabled=1 ORDER BY id",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT pattern FROM moderation_rules WHERE enabled=1 ORDER BY id"
                ).fetchall()
        return [r["pattern"] for r in rows]

    def list_moderation_rules(
        self,
        category: str = "",
        enabled: Optional[int] = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        sql = "SELECT id, category, pattern, enabled, description FROM moderation_rules WHERE 1=1"
        params: List[object] = []
        if category:
            sql += " AND category=?"
            params.append(category)
        if enabled in (0, 1):
            sql += " AND enabled=?"
            params.append(enabled)
        if query:
            sql += " AND (pattern LIKE ? OR description LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "category": r["category"],
                "pattern": r["pattern"],
                "enabled": bool(r["enabled"]),
                "description": r["description"] or "",
            }
            for r in rows
        ]

    def count_moderation_rules_filtered(
        self, category: str = "", enabled: Optional[int] = None, query: str = ""
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM moderation_rules WHERE 1=1"
        params: List[object] = []
        if category:
            sql += " AND category=?"
            params.append(category)
        if enabled in (0, 1):
            sql += " AND enabled=?"
            params.append(enabled)
        if query:
            sql += " AND (pattern LIKE ? OR description LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def save_moderation_rule(
        self,
        category: str,
        pattern: str,
        description: str = "",
        enabled: bool = True,
        rule_id: int = 0,
    ) -> int:
        with self._connect() as conn:
            if rule_id > 0:
                cur = conn.execute(
                    "UPDATE moderation_rules SET category=?, pattern=?, description=?, enabled=? WHERE id=?",
                    (category, pattern, description, 1 if enabled else 0, rule_id),
                )
                conn.commit()
                return rule_id if cur.rowcount else 0
            cur = conn.execute(
                "INSERT INTO moderation_rules(category, pattern, enabled, description) VALUES(?, ?, ?, ?)",
                (category, pattern, 1 if enabled else 0, description),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def delete_moderation_rule(self, rule_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM moderation_rules WHERE id=?", (rule_id,))
            conn.commit()
        return bool(cur.rowcount)

    def delete_moderation_rules(self, rule_ids: Iterable[int]) -> int:
        ids = self._positive_ints(rule_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM moderation_rules WHERE id IN ({placeholders})", ids)
            conn.commit()
        return int(cur.rowcount or 0)

    def get_moderation_rule(self, rule_id: int) -> Optional[dict]:
        # 按 id 查询单条规则，返回 dict（含 category），用于删除前校验分类归属。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, category, pattern, enabled, description FROM moderation_rules WHERE id=?",
                (int(rule_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "category": row["category"],
            "pattern": row["pattern"],
            "enabled": bool(row["enabled"]),
            "description": row["description"] or "",
        }

    def toggle_moderation_rule(self, rule_id: int, enabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE moderation_rules SET enabled=? WHERE id=?",
                (1 if enabled else 0, rule_id),
            )
            conn.commit()
        return bool(cur.rowcount)

    def toggle_moderation_rules(self, rule_ids: Iterable[int], enabled: bool) -> int:
        ids = self._positive_ints(rule_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        params = [1 if enabled else 0, *ids]
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE moderation_rules SET enabled=? WHERE id IN ({placeholders})",
                params,
            )
            conn.commit()
        return int(cur.rowcount or 0)

    def count_moderation_rules(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_rules").fetchone()
        return row["c"] or 0

    def get_meta(self, key: str, default: str = "") -> str:
        # 从 meta 表读取键值对，不存在则返回 default。
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        # 向 meta 表写入键值对，已存在则覆盖（INSERT OR REPLACE）。
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
            conn.commit()

    def count_logs(self) -> int:
        # 返回审核日志表总条数。
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_logs").fetchone()
        return int(row["c"] or 0)

    def count_lexicon_keywords(self) -> int:
        # 返回词库关键词总条数，用于判断是否已导入 seed lexicon。
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM lexicon_keywords").fetchone()
        return int(row["c"] or 0)

    def count_legacy_logs(self) -> int:
        # 统计旧的 moderation_logs.json 中的日志条数（迁移前）。
        if not self.legacy_logs_path.exists():
            return 0
        try:
            with open(self.legacy_logs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0

    def migration_status(self) -> dict:
        # 返回完整的迁移状态信息，供 WebUI 迁移面板展示。
        return {
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            "db_log_count": self.count_logs(),
            "db_lexicon_keyword_count": self.count_lexicon_keywords(),
            "legacy_logs_path": str(self.legacy_logs_path),
            "legacy_logs_exists": self.legacy_logs_path.exists(),
            "legacy_log_count": self.count_legacy_logs(),
            "seed_lexicon_db_path": str(self.seed_lexicon_db_path),
            "seed_lexicon_db_exists": self.seed_lexicon_db_path.exists(),
        }

    def import_lexicon_db(self, path: Path) -> int:
        # 打开 seed DB（发包自带的 lexicon.db），读取所有分类和关键词，再写入当前数据 SQLite。
        path = Path(path)
        if not path.exists():
            return 0
        imported = 0
        src = sqlite3.connect(str(path))
        src.row_factory = sqlite3.Row
        try:
            categories = src.execute("SELECT name, description FROM lexicon_categories").fetchall()
            keywords = src.execute("SELECT category, keyword FROM lexicon_keywords").fetchall()
        finally:
            src.close()
        with self._connect() as conn:
            for row in categories:
                conn.execute(
                    "INSERT OR IGNORE INTO lexicon_categories(name, description) VALUES(?, ?)",
                    (row["name"], row["description"] or ""),
                )
            for row in keywords:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO lexicon_keywords(category, keyword) VALUES(?, ?)",
                    (row["category"], row["keyword"]),
                )
                imported += cur.rowcount if cur.rowcount else 0
            conn.commit()
        return imported

    def load_lexicon(self) -> Dict[str, Dict]:
        with self._connect() as conn:
            cats = conn.execute(
                "SELECT name, description FROM lexicon_categories ORDER BY name"
            ).fetchall()
            keyword_rows = conn.execute(
                "SELECT category, keyword FROM lexicon_keywords ORDER BY category, id"
            ).fetchall()
            result = {
                cat["name"]: {
                    "description": cat["description"] or "",
                    "keywords": [],
                }
                for cat in cats
            }
            for row in keyword_rows:
                category = row["category"]
                if category in result:
                    result[category]["keywords"].append(row["keyword"])
        return result

    def list_lexicon_categories(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.name, c.description, COUNT(k.id) AS keyword_count "
                "FROM lexicon_categories c "
                "LEFT JOIN lexicon_keywords k ON k.category = c.name "
                "GROUP BY c.name, c.description ORDER BY c.name"
            ).fetchall()
        return [
            {
                "name": r["name"],
                "description": r["description"] or "",
                "keyword_count": int(r["keyword_count"] or 0),
            }
            for r in rows
        ]

    def load_lexicon_category(self, category: str) -> Optional[dict]:
        with self._connect() as conn:
            cat = conn.execute(
                "SELECT name, description FROM lexicon_categories WHERE name=?",
                (category,),
            ).fetchone()
            if not cat:
                return None
            rows = conn.execute(
                "SELECT keyword FROM lexicon_keywords WHERE category=? ORDER BY id",
                (category,),
            ).fetchall()
        return {
            "name": cat["name"],
            "description": cat["description"] or "",
            "keywords": [r["keyword"] for r in rows],
        }

    def list_lexicon_keywords(
        self, category: str, query: str = "", limit: int = 200, offset: int = 0
    ) -> List[dict]:
        sql = "SELECT id, keyword FROM lexicon_keywords WHERE category=?"
        params: List[object] = [category]
        if query:
            sql += " AND keyword LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r["id"], "keyword": r["keyword"]} for r in rows]

    def count_lexicon_keywords_filtered(self, category: str, query: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM lexicon_keywords WHERE category=?"
        params: List[object] = [category]
        if query:
            sql += " AND keyword LIKE ?"
            params.append(f"%{query}%")
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def add_lexicon_keyword(self, category: str, keyword: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO lexicon_keywords(category, keyword) VALUES(?, ?)",
                (category, keyword),
            )
            conn.commit()
        return bool(cur.rowcount)

    def add_lexicon_keywords(self, category: str, keywords: Iterable[str]) -> int:
        values = [(category, str(k).strip()) for k in keywords if str(k).strip()]
        if not values:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO lexicon_keywords(category, keyword) VALUES(?, ?)",
                values,
            )
            conn.commit()
        return int(cur.rowcount or 0)

    def list_existing_lexicon_keywords(self, category: str, keywords: Iterable[str]) -> List[str]:
        items = [str(k).strip() for k in keywords if str(k).strip()]
        if not items:
            return []
        with self._connect() as conn:
            if len(items) <= 900:
                placeholders = ",".join("?" for _ in items)
                rows = conn.execute(
                    f"SELECT keyword FROM lexicon_keywords WHERE category=? AND keyword IN ({placeholders})",
                    [category, *items],
                ).fetchall()
                return [str(r["keyword"]) for r in rows]
            existing: List[str] = []
            for i in range(0, len(items), 900):
                part = items[i:i + 900]
                placeholders = ",".join("?" for _ in part)
                rows = conn.execute(
                    f"SELECT keyword FROM lexicon_keywords WHERE category=? AND keyword IN ({placeholders})",
                    [category, *part],
                ).fetchall()
                existing.extend(str(r["keyword"]) for r in rows)
        return existing

    def update_lexicon_keyword(self, keyword_id: int, category: str, keyword: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lexicon_keywords SET category=?, keyword=? WHERE id=?",
                (category, keyword, keyword_id),
            )
            conn.commit()
        return bool(cur.rowcount)

    def delete_lexicon_keyword(self, keyword_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM lexicon_keywords WHERE id=?", (keyword_id,))
            conn.commit()
        return bool(cur.rowcount)

    def delete_lexicon_keywords(self, keyword_ids: Iterable[int]) -> int:
        ids = self._positive_ints(keyword_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM lexicon_keywords WHERE id IN ({placeholders})", ids)
            conn.commit()
        return int(cur.rowcount or 0)

    @staticmethod
    def _log_to_row(log: dict) -> tuple:
        return (
            int(log.get("id", 0)),
            log.get("time", ""),
            int(log.get("ts", 0) or 0),
            str(log.get("group_id", "")),
            str(log.get("user_id", "")),
            str(log.get("user_name", "")),
            str(log.get("msg_text", "")),
            str(log.get("msg_preview", "")),
            str(log.get("action", "")),
            str(log.get("reason", "")),
            json.dumps(log.get("image_urls", []) or [], ensure_ascii=False),
        )

    @staticmethod
    def _row_to_log(row) -> dict:
        try:
            image_urls = json.loads(row["image_urls"] or "[]")
            if not isinstance(image_urls, list):
                image_urls = []
        except Exception:
            image_urls = []
        return {
            "id": row["id"],
            "time": row["time"] or "",
            "ts": row["ts"] or 0,
            "group_id": row["group_id"] or "",
            "user_id": row["user_id"] or "",
            "user_name": row["user_name"] or "",
            "msg_text": row["msg_text"] or "",
            "msg_preview": row["msg_preview"] or "",
            "action": row["action"] or "",
            "reason": row["reason"] or "",
            "image_urls": image_urls,
        }

    def add_log(self, log: dict) -> None:
        # INSERT OR REPLACE 按 id 主键持久化一条审核日志到 SQLite。
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO moderation_logs("
                "id, time, ts, group_id, user_id, user_name, msg_text, msg_preview, action, reason, image_urls"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._log_to_row(log),
            )
            conn.commit()
        # v2.16.0：写日志后失效违规积分计数缓存，保证积分升级判断相对实时
        self.invalidate_query_cache("violation:")

    def import_logs(self, logs: Iterable[dict]) -> int:
        # 批量导入 dict 格式的日志到 SQLite（INSERT OR IGNORE 按 id 去重）。
        imported = 0
        with self._connect() as conn:
            for log in logs:
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO moderation_logs("
                        "id, time, ts, group_id, user_id, user_name, msg_text, msg_preview, action, reason, image_urls"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._log_to_row(log),
                    )
                    imported += cur.rowcount if cur.rowcount else 0
                except Exception:
                    logger.debug("[GroupMgr] 跳过一条无法导入的旧日志", exc_info=True)
            conn.commit()
        return imported

    def import_legacy_logs(self, delete_file: bool = False) -> int:
        # 读取旧的 moderation_logs.json，批量 INSERT 到 SQLite，然后备份并删除原文件。
        if not self.legacy_logs_path.exists():
            return 0
        with open(self.legacy_logs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return 0
        imported = self.import_logs(data)
        self.set_meta("logs_migrated_at", str(int(time.time())))
        if delete_file:
            backup = self.legacy_logs_path.with_suffix(self.legacy_logs_path.suffix + ".bak")
            try:
                shutil.copy2(self.legacy_logs_path, backup)
            except Exception:
                logger.warning("[GroupMgr] 旧日志备份失败，将继续删除原文件", exc_info=True)
            os.remove(self.legacy_logs_path)
        return imported

    def list_logs(self, limit: int = 200, offset: int = 0,
                  group_id: str = "", user_id: str = "", action: str = "") -> List[dict]:
        sql = "SELECT * FROM moderation_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if action:
            sql += " AND action LIKE ?"
            params.append(f"%{action}%")
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_log(r) for r in rows]

    def count_logs_filtered(self, group_id: str = "", user_id: str = "", action: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM moderation_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if action:
            sql += " AND action LIKE ?"
            params.append(f"%{action}%")
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def list_logs_asc(self, limit: int = 500) -> List[dict]:
        # 按 id 降序查询后反转返回（即实际升序），用于内存缓存按时间顺序回放。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_log(r) for r in reversed(rows)]

    def get_log(self, log_id: int) -> Optional[dict]:
        # 根据 id 查询单条审核日志。
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM moderation_logs WHERE id=?", (log_id,)).fetchone()
        return self._row_to_log(row) if row else None

    def delete_logs(self, ids: Iterable[int]) -> int:
        # 按 id 列表批量删除审核日志，返回实际删除条数。
        ids = self._positive_ints(ids)
        if not ids:
            return 0
        total = 0
        with self._connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(f"DELETE FROM moderation_logs WHERE id IN ({placeholders})", chunk)
                total += int(cur.rowcount or 0)
            conn.commit()
        return total

    def delete_logs_by_users(self, user_ids: Iterable[object]) -> int:
        # 按用户 ID 批量删除审核日志，避免 WebUI 为了拿日志 id 拉取全量导出。
        users = self._non_empty_strings(user_ids)
        if not users:
            return 0
        total = 0
        with self._connect() as conn:
            for start in range(0, len(users), 500):
                chunk = users[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(f"DELETE FROM moderation_logs WHERE user_id IN ({placeholders})", chunk)
                total += int(cur.rowcount or 0)
            conn.commit()
        return total

    def delete_all_logs(self) -> int:
        # 清空审核日志表，返回删除的总条数。
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_logs").fetchone()
            count = int(row["c"] or 0)
            conn.execute("DELETE FROM moderation_logs")
            conn.commit()
        return count

    def max_log_id(self) -> int:
        # 查询当前最大 id，用于计算下一个自增 id。
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) AS m FROM moderation_logs").fetchone()
        return int(row["m"] or -1)

    def migrate_legacy(self, delete_logs: bool = False) -> dict:
        # 将旧的 JSON 格式日志导入 SQLite，返回导入数和最终状态。
        imported_logs = self.import_legacy_logs(delete_file=delete_logs)
        return {
            "imported_logs": imported_logs,
            "deleted_legacy_logs": delete_logs and not self.legacy_logs_path.exists(),
            "status": self.migration_status(),
        }

    def get_daily_trend(self, days: int = 30) -> List[dict]:
        # 按天聚合审核日志，返回最近 days 天每日的拦截/放行/总审核数。
        # v2.16.0：统计报表走 30s TTL 缓存，避免 WebUI 图表反复全表聚合。
        return self._query_cached(
            f"daily_trend:{days}", self._QUERY_CACHE_TTL_STATS,
            self._query_daily_trend, days,
        )

    def _query_daily_trend(self, days: int) -> List[dict]:
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT DATE(time) as day, "
                "SUM(CASE WHEN action LIKE '%撤回%' THEN 1 ELSE 0 END) as blocked, "
                "SUM(CASE WHEN action LIKE '%放行%' THEN 1 ELSE 0 END) as passed, "
                "COUNT(*) as total "
                "FROM moderation_logs WHERE ts >= ? "
                "GROUP BY DATE(time) ORDER BY day ASC",
                (since,),
            ).fetchall()
        return [{"date": r["day"], "blocked": r["blocked"] or 0, "passed": r["passed"] or 0, "total": r["total"] or 0} for r in rows]

    def get_violation_distribution(self, days: int = 30) -> List[dict]:
        # 按违规原因分组统计最近 days 天的分布情况，返回各类型及其出现次数。
        return self._query_cached(
            f"violation_dist:{days}", self._QUERY_CACHE_TTL_STATS,
            self._query_violation_distribution, days,
        )

    def _query_violation_distribution(self, days: int) -> List[dict]:
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT reason, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' AND reason != '' "
                "GROUP BY reason ORDER BY count DESC",
                (since,),
            ).fetchall()
        return [{"reason": r["reason"], "count": r["count"] or 0} for r in rows]

    def get_group_activity_ranking(self, days: int = 30, top_n: int = 10) -> List[dict]:
        # 按群号聚合最近 days 天的拦截量并排序，返回 Top N 群拦截排行。
        return self._query_cached(
            f"group_rank:{days}:{top_n}", self._QUERY_CACHE_TTL_STATS,
            self._query_group_activity_ranking, days, top_n,
        )

    def _query_group_activity_ranking(self, days: int, top_n: int) -> List[dict]:
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT group_id, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' AND group_id != '' "
                "GROUP BY group_id ORDER BY count DESC LIMIT ?",
                (since, top_n),
            ).fetchall()
        return [{"group_id": r["group_id"], "count": r["count"] or 0} for r in rows]

    def get_hourly_distribution(self, days: int = 7) -> List[dict]:
        # 按小时聚合最近 days 天的拦截量，返回 0-23 各时段分布，用于分析活跃高峰。
        return self._query_cached(
            f"hourly:{days}", self._QUERY_CACHE_TTL_STATS,
            self._query_hourly_distribution, days,
        )

    def _query_hourly_distribution(self, days: int) -> List[dict]:
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT CAST(STRFTIME('%H', time) AS INTEGER) as hour, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' "
                "GROUP BY hour ORDER BY hour ASC",
                (since,),
            ).fetchall()
        return [{"hour": r["hour"], "count": r["count"] or 0} for r in rows]

    # ============================================================
    # v2.13.0 新增：群活跃度统计（日活/周活/月活）
    # ============================================================
    def record_group_activity(self, group_id: str, user_id: str, user_name: str, ts: int = None) -> None:
        """记录一条群发言（群活跃度统计的数据源）。失败静默。"""
        if not group_id or not user_id:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO group_activity(ts, group_id, user_id, user_name) VALUES(?,?,?,?)",
                    (int(ts or time.time()), str(group_id), str(user_id), str(user_name or "")),
                )
        except Exception as e:
            logger.debug(f"[GroupMgr] 记录群活跃度失败: {e}")

    def get_group_activity_summary(self, group_id: str, days: int = 30) -> List[dict]:
        """按日聚合某群最近 days 天的活跃度，返回 [{date, users, msgs}]。"""
        return self._query_cached(
            f"activity_summary:{group_id}:{days}", self._QUERY_CACHE_TTL_ACTIVITY,
            self._query_group_activity_summary, group_id, days,
        )

    def _query_group_activity_summary(self, group_id: str, days: int) -> List[dict]:
        import datetime as _dt

        since = int(time.time()) - days * 86400
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, user_id FROM group_activity "
                "WHERE group_id=? AND ts>=? ORDER BY ts ASC",
                (str(group_id), since),
            ).fetchall()
        daily = {}
        for r in rows:
            day = _dt.date.fromtimestamp(r["ts"]).isoformat()
            entry = daily.setdefault(day, {"date": day, "users": set(), "msgs": 0})
            entry["users"].add(r["user_id"])
            entry["msgs"] += 1
        out = []
        for day in sorted(daily.keys()):
            e = daily[day]
            out.append({"date": day, "users": len(e["users"]), "msgs": e["msgs"]})
        return out

    def get_group_activity_top_users(self, group_id: str, days: int = 30, top_n: int = 10) -> List[dict]:
        """某群最近 days 天的活跃用户排行（按发言条数）。"""
        return self._query_cached(
            f"activity_top:{group_id}:{days}:{top_n}", self._QUERY_CACHE_TTL_ACTIVITY,
            self._query_group_activity_top_users, group_id, days, top_n,
        )

    def _query_group_activity_top_users(self, group_id: str, days: int, top_n: int) -> List[dict]:
        since = int(time.time()) - days * 86400
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, user_name, COUNT(*) as cnt FROM group_activity "
                "WHERE group_id=? AND ts>=? GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
                (str(group_id), since, top_n),
            ).fetchall()
        return [{"user_id": r["user_id"], "user_name": r["user_name"], "count": r["cnt"] or 0} for r in rows]

    def get_user_violation_count(self, group_id: str, user_id: str, days: int = 30) -> int:
        """某用户在指定群最近 days 天内的违规次数（违规积分累进制数据源）。

        统计审核处罚记录（撤回/禁言/踢出/警告等处置均写入 moderation_logs）。
        带 5s TTL 缓存；add_log 写日志时主动失效，保证积分升级判断相对实时。
        """
        if not group_id or not user_id:
            return 0
        try:
            return self._query_cached(
                f"violation:{group_id}:{user_id}:{days}",
                self._QUERY_CACHE_TTL_VIOLATION,
                self._query_user_violation_count, group_id, user_id, days,
            )
        except Exception:
            return 0

    def _query_user_violation_count(self, group_id: str, user_id: str, days: int) -> int:
        since = int(time.time()) - max(1, int(days)) * 86400
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM moderation_logs "
                "WHERE group_id=? AND user_id=? AND ts>=? AND action != ''",
                (str(group_id), str(user_id), since),
            ).fetchone()
        return int(row["cnt"] or 0) if row else 0

    def record_web_audit(self, operator_name: str = "", operator_qq: str = "",
                         group_id: str = "", action: str = "", target_user: str = "",
                         params: str = "", result: str = "", message: str = "",
                         operator_ip: str = "", before_value: str = "",
                         after_value: str = "") -> None:
        """记录一条 WebUI 远程操作审计日志（失败静默）。

        v2.19.0 增加 operator_ip（操作人IP）、before_value/after_value（修改前后值）。
        """
        try:
            now = int(time.time())
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO web_audit_logs(ts, time, operator_name, operator_qq, group_id, "
                    "action, target_user, params, result, message, operator_ip, before_value, after_value) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                     str(operator_name or ""), str(operator_qq or ""), str(group_id or ""),
                     str(action or ""), str(target_user or ""), str(params or ""),
                     str(result or ""), str(message or ""),
                     str(operator_ip or ""), str(before_value or ""), str(after_value or "")),
                )
                # _connect() 退出时不隐式 commit，必须显式提交否则写入在连接关闭时回滚
                conn.commit()
        except Exception as e:
            logger.debug(f"[GroupMgr] 记录 Web 审计日志失败: {e}")

    def list_web_audit_logs(self, limit: int = 100, group_id: str = "") -> List[dict]:
        """查询 WebUI 远程操作审计日志（按时间倒序，可指定群过滤）。"""
        try:
            with self._connect() as conn:
                if group_id:
                    rows = conn.execute(
                        "SELECT * FROM web_audit_logs WHERE group_id=? "
                        "ORDER BY ts DESC LIMIT ?", (str(group_id), max(1, int(limit))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM web_audit_logs ORDER BY ts DESC LIMIT ?",
                        (max(1, int(limit)),),
                    ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ============================================================
    # v2.19.0 双管理员审批：高敏感远程操作的待审批存储
    # ============================================================
    _PENDING_OP_TTL_SECONDS = 600  # 默认 10 分钟未确认自动过期

    def create_pending_web_operation(self, operator_name: str = "", operator_qq: str = "",
                                     operator_ip: str = "", group_id: str = "",
                                     action: str = "", params: str = "",
                                     ttl_seconds: int = None) -> int:
        """创建一条待审批的高敏感远程操作，返回 id；失败返回 0。"""
        try:
            now = int(time.time())
            ttl = max(60, int(ttl_seconds or self._PENDING_OP_TTL_SECONDS))
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO pending_web_operations(ts, expire_at, status, operator_name, "
                    "operator_qq, operator_ip, group_id, action, params) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, now + ttl, "pending", str(operator_name or ""), str(operator_qq or ""),
                     str(operator_ip or ""), str(group_id or ""), str(action or ""),
                     str(params or "")),
                )
                conn.commit()
                return int(cur.lastrowid or 0)
        except Exception as e:
            logger.debug(f"[GroupMgr] 创建待审批操作失败: {e}")
            return 0

    def list_pending_web_operations(self, limit: int = 20) -> List[dict]:
        """列出未过期且待处理的高敏感操作（pending 待确认 / failed 可重试，按时间倒序）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM pending_web_operations WHERE status IN ('pending','failed') "
                    "AND expire_at>=? ORDER BY ts DESC LIMIT ?", (int(time.time()), max(1, int(limit))),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_pending_web_operation(self, op_id: int) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM pending_web_operations WHERE id=?", (int(op_id),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def approve_pending_web_operation(self, op_id: int, approver_name: str = "",
                                      approver_qq: str = "", approver_ip: str = "") -> bool:
        """确认高敏感操作：仅 pending（或执行失败后可重试的 failed）且未过期可确认成功（CAS 防并发）。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE pending_web_operations SET status='approved', approver_name=?, "
                    "approver_qq=?, approver_ip=?, result='' WHERE id=? "
                    "AND status IN ('pending','failed') AND expire_at>=?",
                    (str(approver_name or ""), str(approver_qq or ""), str(approver_ip or ""),
                     int(op_id), int(time.time())),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            return False

    def reject_pending_web_operation(self, op_id: int, rejector_name: str = "",
                                     rejector_qq: str = "") -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE pending_web_operations SET status='rejected', "
                    "approver_name=?, approver_qq=? WHERE id=? AND status IN ('pending','failed')",
                    (str(rejector_name or ""), str(rejector_qq or ""), int(op_id)),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            return False

    def expire_pending_web_operations(self) -> None:
        """把超时未审批的操作标记为 expired（幂等）。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pending_web_operations SET status='expired' "
                    "WHERE status IN ('pending','failed') AND expire_at<?", (int(time.time()),),
                )
                conn.commit()
        except Exception:
            pass

    def mark_pending_web_executed(self, op_id: int) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pending_web_operations SET executed=1 WHERE id=?",
                    (int(op_id),),
                )
                conn.commit()
        except Exception:
            pass

    def mark_pending_web_failed(self, op_id: int, result: str = "") -> None:
        """执行失败：标记为 failed（保持可见、可重试），记录失败原因。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pending_web_operations SET status='failed', executed=0, result=? WHERE id=?",
                    (str(result or "")[:500], int(op_id)),
                )
                conn.commit()
        except Exception:
            pass

    # ============================================================
    # v2.23.0 不确定视频广告管理员复核队列
    # ============================================================

    def create_video_ad_review(
        self,
        group_id: str,
        user_id: str,
        user_name: str = "",
        msg_text: str = "",
        fingerprint: str = "",
        source: str = "",
    ) -> int:
        """把一条「疑似广告视频」写入待复核队列；成功返回自增 id，失败返回 0。"""
        if not str(group_id or "") or not str(user_id or ""):
            return 0
        try:
            now = int(time.time())
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO video_ad_reviews "
                    "(ts, group_id, user_id, user_name, msg_text, msg_preview, "
                    " fingerprint, source, status, reviewed_by, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', 0)",
                    (
                        now,
                        str(group_id or ""),
                        str(user_id or ""),
                        str(user_name or "")[:64],
                        str(msg_text or ""),
                        str(msg_text or "")[:200],
                        str(fingerprint or ""),
                        str(source or "")[:512],
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            return 0

    def list_pending_video_ad_reviews(self, limit: int = 50) -> List[dict]:
        """列出待复核的视频广告（pending，按时间倒序）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_preview, fingerprint, source, status, reviewed_by, reviewed_at "
                    "FROM video_ad_reviews WHERE status='pending' "
                    "ORDER BY ts DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_video_ad_review(self, review_id: int) -> Optional[dict]:
        """按 id 查询一条视频广告复核记录（任意状态）。"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_preview, fingerprint, source, status, reviewed_by, reviewed_at "
                    "FROM video_ad_reviews WHERE id=?",
                    (int(review_id),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def resolve_video_ad_review(
        self, review_id: int, status: str, reviewer: str = ""
    ) -> bool:
        """确认违规（confirmed）或放行（cleared）；仅 pending 可成功（CAS 防并发）。"""
        if status not in ("confirmed", "cleared"):
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE video_ad_reviews SET status=?, reviewed_by=?, "
                    "reviewed_at=? WHERE id=? AND status='pending'",
                    (
                        status,
                        str(reviewer or "")[:64],
                        int(time.time()),
                        int(review_id),
                    ),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            return False

    # ============================================================
    # v2.36.0 通用疑似广告人工复核队列 + 文本指纹学习库（adguard 合并）
    # ============================================================

    def create_ad_review(
        self,
        group_id: str,
        user_id: str,
        user_name: str = "",
        msg_text: str = "",
        msg_id: str = "",
        image_urls: list = None,
        source: str = "text",
    ) -> int:
        """把一条「疑似广告」写入待复核队列；成功返回自增 id，失败返回 0。"""
        if not str(group_id or "") or not str(user_id or ""):
            return 0
        try:
            now = int(time.time())
            images = []
            for url in (image_urls or []):
                try:
                    images.append(str(url))
                except Exception:
                    pass
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO ad_reviews "
                    "(ts, group_id, user_id, user_name, msg_text, msg_id, "
                    " image_urls, source, status, reviewed_by, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', 0)",
                    (
                        now,
                        str(group_id or ""),
                        str(user_id or ""),
                        str(user_name or "")[:64],
                        str(msg_text or ""),
                        str(msg_id or "")[:64],
                        ",".join(images)[:4000],
                        str(source or "text")[:64],
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            return 0

    def list_pending_ad_reviews(self, limit: int = 50) -> List[dict]:
        """列出待复核的疑似广告（pending，按时间倒序）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_id, image_urls, source, status, reviewed_by, reviewed_at "
                    "FROM ad_reviews WHERE status='pending' "
                    "ORDER BY ts DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def list_ad_reviews_for_sync(self, limit: int = 500) -> List[dict]:
        """v2.36.8：列出已处理（confirmed/released）的广告复核记录，供云同步上传。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_id, image_urls, source, status, reviewed_by, reviewed_at "
                    "FROM ad_reviews WHERE status IN ('confirmed', 'released') "
                    "ORDER BY ts DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_ad_review(self, review_id: int) -> Optional[dict]:
        """按 id 查询一条疑似广告复核记录（任意状态）。"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_id, image_urls, source, status, reviewed_by, reviewed_at "
                    "FROM ad_reviews WHERE id=?",
                    (int(review_id),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def resolve_ad_review(
        self, review_id: int, status: str, reviewer: str = ""
    ) -> bool:
        """确认违规（confirmed）或放行（released）；仅 pending 可成功（CAS 防并发）。"""
        if status not in ("confirmed", "released"):
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE ad_reviews SET status=?, reviewed_by=?, "
                    "reviewed_at=? WHERE id=? AND status='pending'",
                    (
                        status,
                        str(reviewer or "")[:64],
                        int(time.time()),
                        int(review_id),
                    ),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            return False

    def ad_text_fingerprint_hit(self, fingerprint: str) -> Optional[str]:
        """按指纹查询学习结论；命中返回 verdict（ad/ok），未命中返回 None。"""
        if not fingerprint:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT verdict FROM ad_text_fingerprints WHERE fingerprint=?",
                    (str(fingerprint),),
                ).fetchone()
            return str(row["verdict"]) if row else None
        except Exception:
            return None

    def learn_ad_text_fingerprint(
        self, fingerprint: str, verdict: str, group_id: str = "", text: str = ""
    ) -> bool:
        """学习一条文本指纹结论（ad=确认广告 / ok=确认放行），重复覆盖为最新结论。"""
        if not fingerprint or verdict not in ("ad", "ok"):
            return False
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO ad_text_fingerprints "
                    "(fingerprint, verdict, group_id, text_preview, ts) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET "
                    "verdict=excluded.verdict, group_id=excluded.group_id, "
                    "text_preview=excluded.text_preview, ts=excluded.ts",
                    (
                        str(fingerprint),
                        verdict,
                        str(group_id or ""),
                        str(text or "")[:200],
                        int(time.time()),
                    ),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def list_ad_text_fingerprints(self, limit: int = 200) -> List[dict]:
        """列出文本指纹学习库（按时间倒序）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT fingerprint, verdict, group_id, text_preview, ts "
                    "FROM ad_text_fingerprints ORDER BY ts DESC LIMIT ?",
                    (max(1, min(int(limit), 1000)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def clear_ad_text_fingerprints(self) -> int:
        """清空文本指纹学习库，返回删除条数。"""
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM ad_text_fingerprints")
                conn.commit()
                return int(cur.rowcount)
        except Exception:
            return 0

    # ============================================================
    # v2.32.0 不确定内容（文本/图片 LLM 无法确认）管理员复核队列
    # ============================================================

    def create_uncertain_review(
        self,
        group_id: str,
        user_id: str,
        user_name: str = "",
        msg_text: str = "",
        source: str = "",
    ) -> int:
        """把一条「LLM 无法确认的内容」写入待复核队列；成功返回自增 id，失败返回 0。"""
        if not str(group_id or "") or not str(user_id or ""):
            return 0
        try:
            now = int(time.time())
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO uncertain_reviews "
                    "(ts, group_id, user_id, user_name, msg_text, msg_preview, "
                    " source, status, reviewed_by, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', 0)",
                    (
                        now,
                        str(group_id or ""),
                        str(user_id or ""),
                        str(user_name or "")[:64],
                        str(msg_text or ""),
                        str(msg_text or "")[:200],
                        str(source or "")[:64],
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            return 0

    def list_pending_uncertain_reviews(self, limit: int = 50) -> List[dict]:
        """列出待复核的不确定内容（pending，按时间倒序）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_preview, source, status, reviewed_by, reviewed_at "
                    "FROM uncertain_reviews WHERE status='pending' "
                    "ORDER BY ts DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_uncertain_review(self, review_id: int) -> Optional[dict]:
        """按 id 查询一条不确定内容复核记录（任意状态）。"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, ts, group_id, user_id, user_name, msg_text, "
                    "msg_preview, source, status, reviewed_by, reviewed_at "
                    "FROM uncertain_reviews WHERE id=?",
                    (int(review_id),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def resolve_uncertain_review(
        self, review_id: int, status: str, reviewer: str = ""
    ) -> bool:
        """确认违规（confirmed）或放行（cleared）；仅 pending 可成功（CAS 防并发）。"""
        if status not in ("confirmed", "cleared"):
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE uncertain_reviews SET status=?, reviewed_by=?, "
                    "reviewed_at=? WHERE id=? AND status='pending'",
                    (
                        status,
                        str(reviewer or "")[:64],
                        int(time.time()),
                        int(review_id),
                    ),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            return False

    # ============================================================
    # v2.4.0 新增：F1 入群审核规则
    # ============================================================
    def get_join_audit_rule(self, group_id: str) -> Optional[dict]:
        # 读取某个群的入群审核规则；group_id 传 'default' 取全局兜底规则。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled "
                "FROM join_audit_rules WHERE group_id=?",
                (str(group_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "group_id": row["group_id"],
            "accept_keywords": self._loads_list(row["accept_keywords"]),
            "reject_keywords": self._loads_list(row["reject_keywords"]),
            "default_action": row["default_action"] or "manual",
            "reject_reason": row["reject_reason"] or "",
            "enabled": bool(row["enabled"]),
        }

    def list_join_audit_rules(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled "
                "FROM join_audit_rules ORDER BY group_id"
            ).fetchall()
        return [
            {
                "group_id": r["group_id"],
                "accept_keywords": self._loads_list(r["accept_keywords"]),
                "reject_keywords": self._loads_list(r["reject_keywords"]),
                "default_action": r["default_action"] or "manual",
                "reject_reason": r["reject_reason"] or "",
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]

    def save_join_audit_rule(self, group_id: str, accept_keywords: List[str], reject_keywords: List[str],
                             default_action: str = "manual", reject_reason: str = "", enabled: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO join_audit_rules("
                "group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    str(group_id),
                    json.dumps(accept_keywords or [], ensure_ascii=False),
                    json.dumps(reject_keywords or [], ensure_ascii=False),
                    default_action or "manual",
                    reject_reason or "",
                    1 if enabled else 0,
                ),
            )
            conn.commit()

    def delete_join_audit_rule(self, group_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM join_audit_rules WHERE group_id=?", (str(group_id),))
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：F2 刷屏申诉
    # ============================================================
    def open_appeal(self, group_id: str, user_id: str, reason: str, penalty: str,
                    mute_duration: int, created_at: int, expire_at: int) -> int:
        # 登记一条 waiting 申诉；若同群同人已有 waiting，先作废旧的（标记 expired）再新建。
        with self._connect() as conn:
            conn.execute(
                "UPDATE appeals SET status='expired', decided_at=? "
                "WHERE group_id=? AND user_id=? AND status='waiting'",
                (created_at, str(group_id), str(user_id)),
            )
            cur = conn.execute(
                "INSERT INTO appeals(group_id, user_id, reason, penalty, mute_duration, status, created_at, expire_at) "
                "VALUES(?, ?, ?, ?, ?, 'waiting', ?, ?)",
                (str(group_id), str(user_id), reason or "", penalty or "", int(mute_duration or 0), created_at, expire_at),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def get_waiting_appeal(self, user_id: str) -> Optional[dict]:
        # 取某用户当前 waiting 的申诉（私聊裁决时用）。取最近一条。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM appeals WHERE user_id=? AND status='waiting' ORDER BY id DESC LIMIT 1",
                (str(user_id),),
            ).fetchone()
        return self._appeal_row_to_dict(row) if row else None

    def set_appeal_status(self, appeal_id: int, status: str, decided_at: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE appeals SET status=?, decided_at=? WHERE id=?",
                (status, int(decided_at), int(appeal_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def mark_appeal_prompted(self, appeal_id: int) -> bool:
        """Atomically mark the text prompt as sent.

        Returning rowcount from a conditional UPDATE makes this safe when the
        user sends multiple non-text private messages at almost the same time:
        only one handler gets True and sends the prompt.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE appeals SET prompt_sent=1 "
                "WHERE id=? AND status='waiting' AND prompt_sent=0",
                (int(appeal_id),),
            )
            conn.commit()
        return bool(cur.rowcount)

    def claim_appeal_attempt(self, appeal_id: int, max_attempts: int = 2) -> int:
        """抢占一次文字申诉机会，成功返回当前第几次，失败返回 0。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE appeals SET status='judging', attempts=attempts+1 "
                "WHERE id=? AND status='waiting' AND attempts < ?",
                (int(appeal_id), int(max_attempts)),
            )
            if not cur.rowcount:
                conn.commit()
                return 0
            row = conn.execute("SELECT attempts FROM appeals WHERE id=?", (int(appeal_id),)).fetchone()
            conn.commit()
        return int(row["attempts"]) if row else 0

    def reopen_appeal_waiting(self, appeal_id: int, decrement_attempt: bool = False) -> bool:
        with self._connect() as conn:
            if decrement_attempt:
                cur = conn.execute(
                    "UPDATE appeals SET status='waiting', attempts=MAX(attempts-1, 0) "
                    "WHERE id=? AND status='judging'",
                    (int(appeal_id),),
                )
            else:
                cur = conn.execute(
                    "UPDATE appeals SET status='waiting' WHERE id=? AND status='judging'",
                    (int(appeal_id),),
                )
            conn.commit()
        return bool(cur.rowcount)

    def list_expired_waiting_appeals(self, now_ts: int) -> List[dict]:
        # 列出已过期且仍未裁决的申诉（waiting 或卡住的 judging），供后台任务标记 expired。
        # judging 是裁决中间态，正常会很快转为终态；若插件在裁决途中崩溃/重载会卡在此态，
        # 这里一并按超时回收，避免该用户永久无法再申诉。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appeals WHERE status IN ('waiting','judging') AND expire_at <= ?",
                (int(now_ts),),
            ).fetchall()
        return [self._appeal_row_to_dict(r) for r in rows]

    def list_appeals(self, status: str = "", limit: int = 200) -> List[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM appeals WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM appeals ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [self._appeal_row_to_dict(r) for r in rows]

    @staticmethod
    def _appeal_row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "group_id": row["group_id"] or "",
            "user_id": row["user_id"] or "",
            "reason": row["reason"] or "",
            "penalty": row["penalty"] or "",
            "mute_duration": row["mute_duration"] or 0,
            "status": row["status"] or "",
            "created_at": row["created_at"] or 0,
            "expire_at": row["expire_at"] or 0,
            "decided_at": row["decided_at"] or 0,
            "attempts": row["attempts"] or 0,
            "prompt_sent": bool(row["prompt_sent"]),
        }

    # ============================================================
    # v2.4.0 新增：F3 定时解禁
    # ============================================================
    def add_scheduled_unban(self, group_id: str, user_id: str, unban_at: int, created_at: int) -> None:
        # 登记/更新一条定时解禁计划（同群同人唯一，新计划覆盖旧的）。
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scheduled_unbans("
                "group_id, user_id, unban_at, created_at, retry_count, next_retry_at, last_error"
                ") VALUES(?, ?, ?, ?, 0, ?, '')",
                (
                    str(group_id), str(user_id), int(unban_at), int(created_at),
                    int(unban_at),
                ),
            )
            conn.commit()

    def list_due_unbans(self, now_ts: int) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at, retry_count, next_retry_at, last_error "
                "FROM scheduled_unbans "
                "WHERE CASE WHEN next_retry_at > 0 THEN next_retry_at ELSE unban_at END <= ? "
                "ORDER BY CASE WHEN next_retry_at > 0 THEN next_retry_at ELSE unban_at END",
                (int(now_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_scheduled_unbans(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at, retry_count, next_retry_at, last_error "
                "FROM scheduled_unbans ORDER BY unban_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_scheduled_unban_retry(
        self, unban_id: int, next_retry_at: int, last_error: str = ""
    ) -> bool:
        """记录一次解禁失败并安排下次重试，不丢失原任务。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_unbans SET retry_count=retry_count+1, "
                "next_retry_at=?, last_error=? WHERE id=?",
                (int(next_retry_at), str(last_error or "")[:500], int(unban_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def delete_scheduled_unban(self, unban_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM scheduled_unbans WHERE id=?", (int(unban_id),))
            conn.commit()
        return bool(cur.rowcount)

    def delete_scheduled_unban_by_target(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_unbans WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：F5 群管理员动态授权
    # ============================================================
    def get_group_admin_grant(self, group_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, grant_owner, grant_admin, enabled FROM group_admin_grant WHERE group_id=?",
                (str(group_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "group_id": row["group_id"],
            "grant_owner": bool(row["grant_owner"]),
            "grant_admin": bool(row["grant_admin"]),
            "enabled": bool(row["enabled"]),
        }

    def list_group_admin_grants(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT group_id, grant_owner, grant_admin, enabled FROM group_admin_grant ORDER BY group_id"
            ).fetchall()
        return [
            {
                "group_id": r["group_id"],
                "grant_owner": bool(r["grant_owner"]),
                "grant_admin": bool(r["grant_admin"]),
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]

    def save_group_admin_grant(self, group_id: str, grant_owner: bool, grant_admin: bool, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_admin_grant(group_id, grant_owner, grant_admin, enabled) "
                "VALUES(?, ?, ?, ?)",
                (str(group_id), 1 if grant_owner else 0, 1 if grant_admin else 0, 1 if enabled else 0),
            )
            conn.commit()

    def delete_group_admin_grant(self, group_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM group_admin_grant WHERE group_id=?", (str(group_id),))
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：单群管理类名单（managed_lists）
    # ============================================================
    _MANAGED_LIST_TYPES = ("group_white", "group_black", "user_black", "user_white", "admin")

    def load_managed_list(self, list_type: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value FROM managed_lists WHERE list_type=? ORDER BY value",
                (str(list_type),),
            ).fetchall()
        return [r["value"] for r in rows]

    def count_managed_list(self, list_type: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM managed_lists WHERE list_type=?",
                (str(list_type),),
            ).fetchone()
        return int(row["c"] or 0)

    def add_managed_list_value(self, list_type: str, value: str) -> bool:
        value = str(value).strip()
        if not value:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO managed_lists(list_type, value) VALUES(?, ?)",
                (str(list_type), value),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_managed_list_value(self, list_type: str, value: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM managed_lists WHERE list_type=? AND value=?",
                (str(list_type), str(value).strip()),
            )
            conn.commit()
        return bool(cur.rowcount)

    def seed_managed_list(self, list_type: str, values: Iterable[str]) -> int:
        # 一次性迁移：把旧 config 名单导入 DB（INSERT OR IGNORE 去重）。
        items = [(str(list_type), str(v).strip()) for v in (values or []) if str(v).strip()]
        if not items:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO managed_lists(list_type, value) VALUES(?, ?)",
                items,
            )
            conn.commit()
        return int(cur.rowcount or 0)

    @staticmethod
    def _loads_list(raw) -> List[str]:
        # 把 DB 里存的 JSON 数组字符串还原成 list[str]，异常时返回空列表。
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []

    # ============================================================
    # v2.4.0 新增：群超管 group_super_admins
    # ============================================================
    def list_group_super_admins(self, group_id: str = "") -> List[dict]:
        # 列出群超管：传 group_id 则只列该群，否则列全部。
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_super_admins WHERE group_id=? ORDER BY user_id",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_super_admins ORDER BY group_id, user_id"
                ).fetchall()
        return [{"group_id": r["group_id"], "user_id": r["user_id"]} for r in rows]

    def is_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM group_super_admins WHERE group_id=? AND user_id=? LIMIT 1",
                (str(group_id), str(user_id)),
            ).fetchone()
        return row is not None

    def add_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_super_admins(group_id, user_id) VALUES(?, ?)",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM group_super_admins WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：群级 bot 权限黑名单 group_admin_block
    # ============================================================
    def list_group_admin_blocks(self, group_id: str = "") -> List[dict]:
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_admin_block WHERE group_id=? ORDER BY user_id",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_admin_block ORDER BY group_id, user_id"
                ).fetchall()
        return [{"group_id": r["group_id"], "user_id": r["user_id"]} for r in rows]

    def is_group_admin_blocked(self, group_id: str, user_id: str) -> bool:
        # 该用户在该群是否被剥夺了 bot 管理权限（群主可设，优先级最高）。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM group_admin_block WHERE group_id=? AND user_id=? LIMIT 1",
                (str(group_id), str(user_id)),
            ).fetchone()
        return row is not None

    def add_group_admin_block(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_admin_block(group_id, user_id) VALUES(?, ?)",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_group_admin_block(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM group_admin_block WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)
