# -*- coding: utf-8 -*-
"""误判反馈、提示词修正候选与审计历史的 SQLite repository。"""

import json
import time
from typing import Dict, Iterable, List, Optional


class ModerationReviewStorageMixin:
    _FEEDBACK_VERDICTS = ("false_positive", "confirmed_violation")
    _SUGGESTION_STATUSES = ("pending", "applied", "rejected", "rolled_back")

    @staticmethod
    def _create_moderation_review_tables(conn) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_feedback ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "log_id INTEGER NOT NULL UNIQUE, "
            "group_id TEXT, user_id TEXT, user_name TEXT, "
            "msg_text TEXT, action TEXT, original_reason TEXT, "
            "verdict TEXT NOT NULL, note TEXT, reviewer TEXT, "
            "review_status TEXT NOT NULL DEFAULT 'pending', "
            "suggestion_id INTEGER, created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_review "
            "ON moderation_feedback(verdict, review_status, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_group "
            "ON moderation_feedback(group_id, updated_at)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_prompt_suggestions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "sample_count INTEGER NOT NULL DEFAULT 0, sample_ids TEXT, "
            "summary TEXT, suggested_guidance TEXT NOT NULL, "
            "previous_guidance TEXT, status TEXT NOT NULL DEFAULT 'pending', "
            "applied_at INTEGER NOT NULL DEFAULT 0, actor TEXT, audit_note TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_suggestion_status "
            "ON moderation_prompt_suggestions(status, created_at)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_review_audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, suggestion_id INTEGER, "
            "action TEXT NOT NULL, actor TEXT, detail TEXT, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_audit_suggestion "
            "ON moderation_review_audit(suggestion_id, created_at)"
        )

    @staticmethod
    def _row_to_feedback(row) -> dict:
        return dict(row) if row else {}

    @staticmethod
    def _row_to_suggestion(row) -> dict:
        if not row:
            return {}
        item = dict(row)
        try:
            sample_ids = json.loads(item.get("sample_ids") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            sample_ids = []
        item["sample_ids"] = [int(value) for value in sample_ids if str(value).isdigit()]
        return item

    def mark_moderation_feedback(
        self,
        log_id: int,
        verdict: str,
        note: str = "",
        reviewer: str = "dashboard",
    ) -> int:
        verdict = str(verdict or "").strip()
        if verdict not in self._FEEDBACK_VERDICTS:
            raise ValueError("反馈结论无效")
        now = int(time.time())
        review_status = "pending" if verdict == "false_positive" else "excluded"
        with self._connect() as conn:
            log = conn.execute(
                "SELECT id, group_id, user_id, user_name, msg_text, action, reason "
                "FROM moderation_logs WHERE id=?",
                (int(log_id),),
            ).fetchone()
            if not log:
                return 0
            conn.execute(
                "INSERT INTO moderation_feedback("
                "log_id, group_id, user_id, user_name, msg_text, action, "
                "original_reason, verdict, note, reviewer, review_status, "
                "suggestion_id, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(log_id) DO UPDATE SET "
                "group_id=excluded.group_id, user_id=excluded.user_id, "
                "user_name=excluded.user_name, msg_text=excluded.msg_text, "
                "action=excluded.action, original_reason=excluded.original_reason, "
                "verdict=excluded.verdict, note=excluded.note, reviewer=excluded.reviewer, "
                "review_status=excluded.review_status, suggestion_id=NULL, "
                "updated_at=excluded.updated_at",
                (
                    int(log["id"]), log["group_id"] or "", log["user_id"] or "",
                    log["user_name"] or "", log["msg_text"] or "",
                    log["action"] or "", log["reason"] or "", verdict,
                    str(note or "")[:1000], str(reviewer or "dashboard")[:100],
                    review_status, now, now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM moderation_feedback WHERE log_id=?", (int(log_id),)
            ).fetchone()
            conn.commit()
        return int(row["id"] if row else 0)

    def clear_moderation_feedback(self, log_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM moderation_feedback WHERE log_id=?", (int(log_id),)
            )
            conn.commit()
        return bool(cur.rowcount)

    def feedback_for_log_ids(self, log_ids: Iterable[int]) -> Dict[int, dict]:
        ids = self._positive_ints(log_ids)
        if not ids:
            return {}
        result: Dict[int, dict] = {}
        with self._connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT * FROM moderation_feedback WHERE log_id IN "
                    f"({placeholders})", chunk,
                ).fetchall()
                result.update({int(row["log_id"]): dict(row) for row in rows})
        return result

    def list_moderation_feedback(
        self, verdict: str = "", limit: int = 100, offset: int = 0
    ) -> List[dict]:
        sql = "SELECT * FROM moderation_feedback WHERE 1=1"
        params: List[object] = []
        if verdict in self._FEEDBACK_VERDICTS:
            sql += " AND verdict=?"
            params.append(verdict)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_feedback(row) for row in rows]

    def pending_false_positive_feedback(self, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_feedback "
                "WHERE verdict='false_positive' AND review_status='pending' "
                "ORDER BY updated_at ASC, id ASC LIMIT ?",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [self._row_to_feedback(row) for row in rows]

    def recent_confirmed_feedback(self, limit: int = 10) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_feedback "
                "WHERE verdict='confirmed_violation' "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 30)),),
            ).fetchall()
        return [self._row_to_feedback(row) for row in rows]

    def create_prompt_suggestion(
        self,
        feedback_ids: Iterable[int],
        summary: str,
        suggested_guidance: str,
        previous_guidance: str,
        actor: str = "scheduler",
    ) -> int:
        ids = list(dict.fromkeys(self._positive_ints(feedback_ids)))
        if not ids or not str(suggested_guidance or "").strip():
            return 0
        now = int(time.time())
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in ids)
            current_rows = conn.execute(
                "SELECT id FROM moderation_feedback WHERE id IN "
                f"({placeholders}) AND verdict='false_positive' "
                "AND review_status='pending'",
                ids,
            ).fetchall()
            current_ids = {int(row["id"]) for row in current_rows}
            # LLM 运行期间管理员可能清除反馈或改判为确认违规。候选必须与
            # 生成时的全部有效样本一致，不能把过期结论写入审核规则。
            if current_ids != set(ids):
                return 0
            cur = conn.execute(
                "INSERT INTO moderation_prompt_suggestions("
                "created_at, updated_at, sample_count, sample_ids, summary, "
                "suggested_guidance, previous_guidance, status, actor"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    now, now, len(ids), json.dumps(ids, ensure_ascii=False),
                    str(summary or "")[:4000],
                    str(suggested_guidance or "")[:12000],
                    str(previous_guidance or "")[:12000], str(actor or "")[:100],
                ),
            )
            suggestion_id = int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE moderation_feedback SET review_status='reviewed', "
                "suggestion_id=?, updated_at=? WHERE id IN "
                f"({placeholders}) AND verdict='false_positive' "
                "AND review_status='pending'",
                [suggestion_id, now, *ids],
            )
            conn.execute(
                "INSERT INTO moderation_review_audit("
                "suggestion_id, action, actor, detail, created_at"
                ") VALUES(?, 'generated', ?, ?, ?)",
                (suggestion_id, str(actor or "")[:100], f"样本数: {len(ids)}", now),
            )
            conn.commit()
        return suggestion_id

    def get_prompt_suggestion(self, suggestion_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM moderation_prompt_suggestions WHERE id=?",
                (int(suggestion_id),),
            ).fetchone()
        return self._row_to_suggestion(row) if row else None

    def list_prompt_suggestions(self, limit: int = 50) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_prompt_suggestions "
                "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [self._row_to_suggestion(row) for row in rows]

    def has_newer_applied_prompt_suggestion(self, suggestion_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM moderation_prompt_suggestions "
                "WHERE id>? AND status='applied' LIMIT 1",
                (int(suggestion_id),),
            ).fetchone()
        return bool(row)

    def transition_prompt_suggestion(
        self,
        suggestion_id: int,
        expected_statuses: Iterable[str],
        new_status: str,
        actor: str,
        detail: str = "",
    ) -> bool:
        expected = [str(item) for item in expected_statuses if str(item)]
        if not expected or new_status not in self._SUGGESTION_STATUSES:
            return False
        now = int(time.time())
        placeholders = ",".join("?" for _ in expected)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE moderation_prompt_suggestions SET status=?, updated_at=?, "
                "applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END, "
                "actor=?, audit_note=? WHERE id=? AND status IN "
                f"({placeholders})",
                [
                    new_status, now, new_status, now, str(actor or "")[:100],
                    str(detail or "")[:2000], int(suggestion_id), *expected,
                ],
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO moderation_review_audit("
                    "suggestion_id, action, actor, detail, created_at"
                    ") VALUES(?, ?, ?, ?, ?)",
                    (
                        int(suggestion_id), new_status, str(actor or "")[:100],
                        str(detail or "")[:2000], now,
                    ),
                )
            conn.commit()
        return bool(cur.rowcount)

    def list_review_audit(self, suggestion_id: int = 0, limit: int = 100) -> List[dict]:
        sql = "SELECT * FROM moderation_review_audit"
        params: List[object] = []
        if int(suggestion_id or 0) > 0:
            sql += " WHERE suggestion_id=?"
            params.append(int(suggestion_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
