# -*- coding: utf-8 -*-
"""群级配置、名片审计与保护名单的 SQLite repository mixin。"""

from typing import Dict, List, Optional


class GroupStorageMixin:
    """依赖宿主提供 ``_connect()``，保持 ``SQLiteStorage`` 公共 API 不变。"""

    def get_group_config(self, group_id: str, key: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            ).fetchone()
        return row["value"] if row else None

    def get_group_configs(self, group_id: str) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM group_configs WHERE group_id=?",
                (str(group_id),),
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_group_config(self, group_id: str, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_configs(group_id, key, value) "
                "VALUES(?, ?, ?)",
                (str(group_id), str(key), str(value)),
            )
            conn.commit()

    def delete_group_config(self, group_id: str, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def clear_group_configs(self, group_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM group_configs WHERE group_id=?", (str(group_id),)
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def list_configured_groups(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT group_id FROM group_configs ORDER BY group_id"
            ).fetchall()
        return [row["group_id"] for row in rows]

    def add_card_change_log(
        self,
        kind: str,
        group_id: str,
        user_id: str,
        user_name: str,
        old_value: str,
        new_value: str,
        action: str,
        ts: int,
        time_str: str,
    ) -> int:
        """记录一条名片变更/管理员任免日志，返回新行 id。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO card_change_logs("
                "ts, time, kind, group_id, user_id, user_name, old_value, "
                "new_value, action) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(ts), str(time_str), str(kind), str(group_id),
                    str(user_id), str(user_name or ""), str(old_value or ""),
                    str(new_value or ""), str(action or ""),
                ),
            )
            conn.commit()
        return int(cursor.lastrowid or 0)

    def list_card_change_logs(
        self,
        limit: int = 50,
        offset: int = 0,
        group_id: str = "",
        user_id: str = "",
        kind: str = "",
    ) -> List[dict]:
        sql = "SELECT * FROM card_change_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(str(group_id))
        if user_id:
            sql += " AND user_id=?"
            params.append(str(user_id))
        if kind:
            sql += " AND kind=?"
            params.append(str(kind))
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"], "ts": row["ts"], "time": row["time"],
                "kind": row["kind"], "group_id": row["group_id"],
                "user_id": row["user_id"], "user_name": row["user_name"],
                "old_value": row["old_value"],
                "new_value": row["new_value"], "action": row["action"],
            }
            for row in rows
        ]

    def count_card_change_logs(
        self, group_id: str = "", user_id: str = "", kind: str = ""
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM card_change_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(str(group_id))
        if user_id:
            sql += " AND user_id=?"
            params.append(str(user_id))
        if kind:
            sql += " AND kind=?"
            params.append(str(kind))
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def clear_card_change_logs(self, group_id: str = "") -> int:
        with self._connect() as conn:
            if group_id:
                cursor = conn.execute(
                    "DELETE FROM card_change_logs WHERE group_id=?",
                    (str(group_id),),
                )
            else:
                cursor = conn.execute("DELETE FROM card_change_logs")
            conn.commit()
        return int(cursor.rowcount or 0)

    def prune_card_change_logs(self, keep: int = 5000) -> int:
        """仅保留最近 keep 条，删除更早的，防止无限增长。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM card_change_logs WHERE id NOT IN "
                "(SELECT id FROM card_change_logs ORDER BY id DESC LIMIT ?)",
                (int(keep),),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def list_card_protected(self, group_id: str = "") -> List[dict]:
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, user_id, protected_card, created_at "
                    "FROM card_protected_members WHERE group_id=? "
                    "ORDER BY created_at DESC",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, user_id, protected_card, created_at "
                    "FROM card_protected_members "
                    "ORDER BY group_id, created_at DESC"
                ).fetchall()
        return [
            {
                "group_id": row["group_id"], "user_id": row["user_id"],
                "protected_card": row["protected_card"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_card_protected(
        self, group_id: str, user_id: str
    ) -> Optional[str]:
        """返回被保护成员的应有名片；不在保护名单返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT protected_card FROM card_protected_members "
                "WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            ).fetchone()
        return row["protected_card"] if row else None

    def add_card_protected(
        self,
        group_id: str,
        user_id: str,
        protected_card: str,
        created_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO card_protected_members("
                "group_id, user_id, protected_card, created_at) "
                "VALUES(?, ?, ?, ?)",
                (
                    str(group_id), str(user_id), str(protected_card),
                    int(created_at),
                ),
            )
            conn.commit()

    def remove_card_protected(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM card_protected_members "
                "WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cursor.rowcount)

    # ==================== 自适应上下文学习：learned_keywords ====================

    def upsert_learned_candidate(
        self,
        group_id: str,
        keyword: str,
        category: str,
        reason: str,
        sample: str,
        confidence: float,
        now: int,
        source: str = "llm",
    ) -> dict:
        """插入或累计一条学习候选。冲突时累加 occurrences 并刷新理由/样例/置信度，
        但【不改动 status】——已审批(approved)或已拒绝(rejected)的词保持原状，
        避免被后续挖掘重新拉回 pending。返回该词当前 {status, occurrences}。"""
        cat = category if category in ("ad", "swear") else "ad"
        with self._connect() as conn:
            # 匹配器本身大小写不敏感（关键词与正文都会转小写），但 UNIQUE(group_id, keyword)
            # 区分大小写：LLM 先后返回「AI中转」「ai中转」会成为两条互不相干的候选，
            # 出现次数被拆散、审批互不影响。先按不区分大小写找到已有行，沿用其原始写法。
            existing = conn.execute(
                "SELECT keyword FROM learned_keywords "
                "WHERE group_id=? AND lower(keyword)=lower(?) LIMIT 1",
                (str(group_id), str(keyword)),
            ).fetchone()
            if existing:
                keyword = existing["keyword"]
            conn.execute(
                "INSERT INTO learned_keywords("
                "group_id, keyword, category, status, reason, sample, confidence, "
                "occurrences, source, created_at, updated_at) "
                "VALUES(?, ?, ?, 'pending', ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(group_id, keyword) DO UPDATE SET "
                "occurrences = occurrences + 1, "
                "category = excluded.category, "  # 跟随最新一次挖掘的分类，与审批门用的当次分类一致
                "reason = excluded.reason, "
                "sample = excluded.sample, "
                "confidence = MAX(confidence, excluded.confidence), "
                "updated_at = excluded.updated_at",
                (
                    str(group_id), str(keyword), cat, str(reason or ""),
                    str(sample or ""), float(confidence or 0), str(source or "llm"),
                    int(now), int(now),
                ),
            )
            row = conn.execute(
                "SELECT status, occurrences, category FROM learned_keywords "
                "WHERE group_id=? AND keyword=?",
                (str(group_id), str(keyword)),
            ).fetchone()
            conn.commit()
        return {
            "status": row["status"] if row else "pending",
            "occurrences": row["occurrences"] if row else 1,
            "category": row["category"] if row else cat,
        }

    def list_learned(
        self, group_id: str = "", status: str = "",
        limit: int = 200, offset: int = 0,
    ) -> List[dict]:
        sql = "SELECT * FROM learned_keywords WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(str(group_id))
        if status:
            sql += " AND status=?"
            params.append(str(status))
        sql += " ORDER BY occurrences DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"], "group_id": r["group_id"], "keyword": r["keyword"],
                "category": r["category"], "status": r["status"],
                "reason": r["reason"], "sample": r["sample"],
                "confidence": r["confidence"], "occurrences": r["occurrences"],
                "source": r["source"], "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def count_learned(self, group_id: str = "", status: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM learned_keywords WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(str(group_id))
        if status:
            sql += " AND status=?"
            params.append(str(status))
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def set_learned_status(self, keyword_id: int, status: str, now: int) -> Optional[dict]:
        """审批/拒绝一条候选。返回更新后的 {group_id, keyword, category, status} 供重建匹配器。"""
        if status not in ("pending", "approved", "rejected"):
            return None
        with self._connect() as conn:
            conn.execute(
                "UPDATE learned_keywords SET status=?, updated_at=? WHERE id=?",
                (str(status), int(now), int(keyword_id)),
            )
            row = conn.execute(
                "SELECT group_id, keyword, category, status FROM learned_keywords WHERE id=?",
                (int(keyword_id),),
            ).fetchone()
            conn.commit()
        if not row:
            return None
        return {
            "group_id": row["group_id"], "keyword": row["keyword"],
            "category": row["category"], "status": row["status"],
        }

    def delete_learned(self, keyword_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, keyword, category, status FROM learned_keywords WHERE id=?",
                (int(keyword_id),),
            ).fetchone()
            conn.execute("DELETE FROM learned_keywords WHERE id=?", (int(keyword_id),))
            conn.commit()
        if not row:
            return None
        return {
            "group_id": row["group_id"], "keyword": row["keyword"],
            "category": row["category"], "status": row["status"],
        }

    def list_approved_learned(self, group_id: str = "") -> List[dict]:
        """返回已审批生效的学习词，用于构建按群匹配器。"""
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, keyword, category FROM learned_keywords "
                    "WHERE status='approved' AND group_id=?",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, keyword, category FROM learned_keywords "
                    "WHERE status='approved'"
                ).fetchall()
        return [
            {"group_id": r["group_id"], "keyword": r["keyword"], "category": r["category"]}
            for r in rows
        ]

    def get_learned_keyword(self, group_id: str, keyword: str) -> Optional[dict]:
        with self._connect() as conn:
            # 与 upsert_learned_candidate 一致按不区分大小写查找，否则大小写不同时自动审批找不到行
            row = conn.execute(
                "SELECT id, status, category FROM learned_keywords "
                "WHERE group_id=? AND lower(keyword)=lower(?) "
                "ORDER BY keyword=? DESC LIMIT 1",
                (str(group_id), str(keyword), str(keyword)),
            ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "status": row["status"], "category": row["category"]}

    def clear_learned(self, group_id: str = "", status: str = "") -> int:
        sql = "DELETE FROM learned_keywords WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(str(group_id))
        if status:
            sql += " AND status=?"
            params.append(str(status))
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
        return int(cursor.rowcount or 0)
