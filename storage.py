# -*- coding: utf-8 -*-
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from astrbot.api import logger


class SQLiteStorage:
    # 持久化层统一使用 SQLite。_connect() 是 contextmanager，进入时创建连接并开启 WAL，退出时自动关闭。
    # 审核日志按 message_id + group_id + user_id + time 组合键去重。
    # seed_lexicon_db 是发布时打包进插件的内置词库，只在首次初始化时复制到 data 目录。
    def __init__(self, data_dir: Path, plugin_dir: str):
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.db_path = self.data_dir / "group_guardian.db"
        self.seed_lexicon_db_path = self.plugin_dir / "lexicon.db"
        self.legacy_logs_path = self.data_dir / "moderation_logs.json"

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._create_tables(conn)
        self._ensure_seed_lexicon()
        self._ensure_seed_rules()

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

    @contextmanager
    def _connect(self):
        # 使用 contextmanager 确保连接在退出 with 块时总是通过 finally 关闭，防止泄漏。
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
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
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unban_at ON scheduled_unbans(unban_at)")
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
            seed = sqlite3.connect(str(self.seed_lexicon_db_path))
            seed.row_factory = sqlite3.Row
            rows = seed.execute(
                "SELECT category, pattern FROM moderation_rules ORDER BY id"
            ).fetchall()
            seed.close()
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
            result = {}
            for cat in cats:
                rows = conn.execute(
                    "SELECT keyword FROM lexicon_keywords WHERE category=? ORDER BY id",
                    (cat["name"],),
                ).fetchall()
                result[cat["name"]] = {
                    "description": cat["description"] or "",
                    "keywords": [r["keyword"] for r in rows],
                }
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
        # 结果按日期升序排列，日期键为 YYYY-MM-DD 格式字符串。
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
                "INSERT OR REPLACE INTO scheduled_unbans(group_id, user_id, unban_at, created_at) "
                "VALUES(?, ?, ?, ?)",
                (str(group_id), str(user_id), int(unban_at), int(created_at)),
            )
            conn.commit()

    def list_due_unbans(self, now_ts: int) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at FROM scheduled_unbans WHERE unban_at <= ? ORDER BY unban_at",
                (int(now_ts),),
            ).fetchall()
        return [{"id": r["id"], "group_id": r["group_id"], "user_id": r["user_id"], "unban_at": r["unban_at"]} for r in rows]

    def list_all_scheduled_unbans(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at FROM scheduled_unbans ORDER BY unban_at"
            ).fetchall()
        return [{"id": r["id"], "group_id": r["group_id"], "user_id": r["user_id"], "unban_at": r["unban_at"]} for r in rows]

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

    # ============================================================
    # v2.3.0 新增：多群独立配置 group_configs
    # ============================================================
    def get_group_config(self, group_id: str, key: str):
        # 读取某群对某配置项的覆盖值（字符串），不存在返回 None。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            ).fetchone()
        return row["value"] if row else None

    def get_group_configs(self, group_id: str) -> Dict[str, str]:
        # 读取某群的全部配置覆盖，返回 {key: value(str)}。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM group_configs WHERE group_id=?",
                (str(group_id),),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_group_config(self, group_id: str, key: str, value: str) -> None:
        # 设置/更新某群某配置项的覆盖值。
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_configs(group_id, key, value) VALUES(?, ?, ?)",
                (str(group_id), str(key), str(value)),
            )
            conn.commit()

    def delete_group_config(self, group_id: str, key: str) -> bool:
        # 删除某群某配置项的覆盖（恢复为继承全局）。
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def clear_group_configs(self, group_id: str) -> int:
        # 清空某群的全部配置覆盖（整群恢复继承全局）。
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM group_configs WHERE group_id=?", (str(group_id),))
            conn.commit()
        return int(cur.rowcount or 0)

    def list_configured_groups(self) -> List[str]:
        # 列出所有有自定义配置的群号。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT group_id FROM group_configs ORDER BY group_id"
            ).fetchall()
        return [r["group_id"] for r in rows]
