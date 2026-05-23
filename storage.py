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

    def list_logs(self, limit: int = 200, offset: int = 0) -> List[dict]:
        # 按 id 降序分页加载日志，用于 WebUI 展示。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_log(r) for r in rows]

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
        ids = list(ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM moderation_logs WHERE id IN ({placeholders})", ids)
            conn.commit()
        return cur.rowcount if cur.rowcount else 0

    def delete_all_logs(self) -> int:
        # 清空审核日志表，返回删除的总条数。
        with self._connect() as conn:
            count = self.count_logs()
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
