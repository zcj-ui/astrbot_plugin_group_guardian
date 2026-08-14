# -*- coding: utf-8 -*-
"""群活跃度统计（v2.13.0）：日活/周活/月活报表。

记录每个群每条普通发言到 SQLite（group_activity 表，storage.py），提供
`/群活跃度 [天数]` 命令查询：今日 / 近 7 天 / 近 30 天的发言条数与活跃人数，
以及按发言条数排行的活跃用户 Top。默认关闭（group_activity_enabled）。

数据源是审核管线的通用记录（_record_activity），与违规日志独立，正常聊天
也会被统计。仅统计开启后产生的新消息，历史不回溯。
"""
import asyncio

from astrbot.api import logger


class ActivityMixin:
    """群活跃度统计。依赖 storage（SQLiteStorage）与 OneBotMixin 通用工具。"""

    def _activity_enabled(self, group_id: str) -> bool:
        """群活跃度统计开关（可按群覆盖）。"""
        try:
            return self._cfg("group_activity_enabled", False, group_id=group_id)
        except Exception:
            return False

    async def _record_activity(self, event, group_id: str, user_id: str) -> None:
        """审核管线调用：记录一条普通群发言。失败静默。

        v2.16.0：改为 async，DB 写入通过 asyncio.to_thread 在后台线程执行，
        避免每条消息的同步 INSERT 阻塞事件循环。
        """
        if not group_id or not user_id:
            return
        if not self._activity_enabled(group_id):
            return
        try:
            user_name = str(event.get_sender_name() or "")
        except Exception:
            user_name = ""
        try:
            await asyncio.to_thread(
                self._storage.record_group_activity, group_id, user_id, user_name,
            )
        except Exception as e:
            logger.debug(f"[GroupMgr] 群活跃度记录失败: {e}")

    async def _active_user_count(self, group_id: str, days: int) -> int:
        """近 days 天独立活跃人数（近似：取超大 top_n 的去重行数）。"""
        try:
            rows = await asyncio.to_thread(
                self._storage.get_group_activity_top_users, group_id, days, 100000,
            )
            return len(rows)
        except Exception:
            return 0

    async def cmd_group_activity(self, event):
        """群活跃度统计：/群活跃度 [天数]"""
        try:
            args = event.message_str.split()
        except Exception:
            args = []
        days = 30
        if len(args) >= 2:
            days = max(1, min(self._safe_int(args[1], 30), 365))
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取当前群号")
            return
        if not self._activity_enabled(group_id):
            yield event.plain_result(
                "群活跃度统计未开启（配置项 group_activity_enabled），请联系管理员开启后生效"
            )
            return
        try:
            # v2.16.0：聚合查询在后台线程执行，避免阻塞事件循环；结果有 storage 层 TTL 缓存
            summary = await asyncio.to_thread(
                self._storage.get_group_activity_summary, group_id, days,
            )
            top = await asyncio.to_thread(
                self._storage.get_group_activity_top_users, group_id, days, 10,
            )
        except Exception as e:
            logger.warning(f"[GroupMgr] 查询群活跃度失败: {e}")
            yield event.plain_result(f"查询失败: {e}")
            return
        if not summary:
            yield event.plain_result(f"群 {group_id} 最近 {days} 天暂无活跃记录")
            return
        today = summary[-1]
        week_msgs = sum(e["msgs"] for e in summary[-7:])
        month_msgs = sum(e["msgs"] for e in summary[-30:])
        week_users = await self._active_user_count(group_id, 7)
        month_users = await self._active_user_count(group_id, 30)
        lines = [
            f"📊 群活跃度（最近 {days} 天）",
            f"今日：{today['msgs']} 条 / {today['users']} 人",
            f"近7天：{week_msgs} 条 / {week_users} 人",
            f"近30天：{month_msgs} 条 / {month_users} 人",
        ]
        if top:
            lines.append("活跃用户 Top10：")
            for i, u in enumerate(top, 1):
                name = str(u.get("user_name") or u.get("user_id") or "")
                lines.append(f"{i}. {name}({u.get('user_id', '')}) {u.get('count', 0)} 条")
        yield event.plain_result("\n".join(lines))
