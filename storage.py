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

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _create_tables(conn) -> None:
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
        conn.commit()

    def _ensure_seed_lexicon(self) -> None:
        if self.count_lexicon_keywords() > 0:
            return
        if self.seed_lexicon_db_path.exists():
            imported = self.import_lexicon_db(self.seed_lexicon_db_path)
            if imported:
                logger.info(f"[GroupMgr] 已从 lexicon.db 导入词库: {imported} 条")

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
            conn.commit()

    def count_logs(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_logs").fetchone()
        return int(row["c"] or 0)

    def count_lexicon_keywords(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM lexicon_keywords").fetchone()
        return int(row["c"] or 0)

    def count_legacy_logs(self) -> int:
        if not self.legacy_logs_path.exists():
            return 0
        try:
            with open(self.legacy_logs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0

    def migration_status(self) -> dict:
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
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO moderation_logs("
                "id, time, ts, group_id, user_id, user_name, msg_text, msg_preview, action, reason, image_urls"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._log_to_row(log),
            )
            conn.commit()

    def import_logs(self, logs: Iterable[dict]) -> int:
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_log(r) for r in rows]

    def list_logs_asc(self, limit: int = 500) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_log(r) for r in reversed(rows)]

    def get_log(self, log_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM moderation_logs WHERE id=?", (log_id,)).fetchone()
        return self._row_to_log(row) if row else None

    def delete_logs(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM moderation_logs WHERE id IN ({placeholders})", ids)
            conn.commit()
        return cur.rowcount if cur.rowcount else 0

    def delete_all_logs(self) -> int:
        with self._connect() as conn:
            count = self.count_logs()
            conn.execute("DELETE FROM moderation_logs")
            conn.commit()
        return count

    def max_log_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) AS m FROM moderation_logs").fetchone()
        return int(row["m"] or -1)

    def migrate_legacy(self, delete_logs: bool = False) -> dict:
        imported_logs = self.import_legacy_logs(delete_file=delete_logs)
        return {
            "imported_logs": imported_logs,
            "deleted_legacy_logs": delete_logs and not self.legacy_logs_path.exists(),
            "status": self.migration_status(),
        }
