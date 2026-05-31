# -*- coding: utf-8 -*-
"""后台调度模块（v2.4.0）。

负责两类周期性任务：
1. F3 定时自动解禁：扫描 scheduled_unbans 表，到期则解禁并删除记录。
2. F2 申诉超时：把过期仍 waiting 的申诉标记 expired（维持原处罚）。

设计要点：
- 单个 asyncio 后台 loop，按 auto_unban_scan_interval 秒轮询；
- 后台任务无 event，OneBot client 通过 _get_client(None) 的 platform_manager 回退获取；
- 插件 terminate 时取消任务，避免热重载后残留协程。
"""
import asyncio
import time

from astrbot.api import logger


class SchedulerMixin:
    def _init_scheduler(self) -> None:
        """初始化调度器状态。在 __init__ 中调用。"""
        self._scheduler_task = None
        self._scheduler_stop = False

    def _start_scheduler(self) -> None:
        """启动后台调度 loop（幂等：已在运行则不重复启动）。"""
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_stop = False
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        except RuntimeError:
            # 无运行中的事件循环（极少见），放弃后台任务，不影响其它功能
            logger.debug("[GroupMgr] 无事件循环，跳过调度器启动")

    async def _stop_scheduler(self) -> None:
        """停止后台调度 loop。在 terminate 中调用。"""
        self._scheduler_stop = True
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                logger.debug("[GroupMgr] 调度器任务已取消")

    async def _scheduler_loop(self) -> None:
        """调度主循环：按间隔轮询定时解禁与申诉超时。"""
        while not self._scheduler_stop:
            interval = self._clamp_int(self.config.get("auto_unban_scan_interval", 60), 60, 10, 3600)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if self._scheduler_stop:
                break
            try:
                await self._run_due_unbans()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 定时解禁任务出错: {e}")
            try:
                # 申诉超时清理由 AppealMixin 提供，未启用申诉时方法可能不存在
                expire_fn = getattr(self, "_expire_appeals", None)
                if expire_fn:
                    await expire_fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 申诉超时清理出错: {e}")

    async def _run_due_unbans(self) -> None:
        """执行所有到期的定时解禁。"""
        if not self._cfg("auto_unban_enabled", False):
            return
        now = int(time.time())
        due = self._storage.list_due_unbans(now)
        if not due:
            return
        for item in due:
            gid = item.get("group_id", "")
            uid = item.get("user_id", "")
            ok = await self._unban_member(gid, uid)
            # 不论 API 成功与否都删除记录，避免失败项反复重试堆积；失败已在 _unban_member 记日志
            self._storage.delete_scheduled_unban(item.get("id"))
            if ok:
                logger.info(f"[GroupMgr] 定时解禁: 群{gid} 用户{uid}")

    def _schedule_unban(self, group_id: str, user_id: str, mute_seconds: int) -> None:
        """登记一条定时解禁计划。mute_seconds<=0（永久禁言）时按一个较大的兜底时长处理。

        仅在 auto_unban_enabled 开启时登记。OneBot 自带到期解禁，本功能用于
        永久禁言托管解禁、重启后补解禁等场景。
        """
        if not self._cfg("auto_unban_enabled", False):
            return
        if not group_id or not user_id:
            return
        now = int(time.time())
        if mute_seconds and mute_seconds > 0:
            unban_at = now + int(mute_seconds)
        else:
            # 永久禁言：按配置的兜底托管时长（默认 7 天）后解禁
            fallback = self._clamp_int(self.config.get("auto_unban_permanent_hours", 168), 168, 1, 8760)
            unban_at = now + fallback * 3600
        try:
            self._storage.add_scheduled_unban(group_id, user_id, unban_at, now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 登记定时解禁失败: {e}")
