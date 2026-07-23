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
        self._card_sync_task = None
        self._scheduler_stop = False

    def _start_scheduler(self) -> None:
        """启动后台调度 loop（幂等：已在运行则不重复启动）。"""
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_stop = False
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            # 名片同步使用独立低频 loop，避免 auto_unban_scan_interval 较大时
            # 延迟名片变更检测；没有 CardMonitorMixin 时自动跳过。
            if callable(getattr(self, "_sync_group_cards", None)):
                self._card_sync_task = asyncio.create_task(self._card_sync_loop())
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
        if self._card_sync_task and not self._card_sync_task.done():
            self._card_sync_task.cancel()
            try:
                await self._card_sync_task
            except asyncio.CancelledError:
                logger.debug("[GroupMgr] 名片同步任务已取消")

    async def _scheduler_loop(self) -> None:
        consecutive_errors = 0
        while not self._scheduler_stop:
            interval = self._clamp_int(self.config.get("auto_unban_scan_interval", 60), 60, 10, 3600)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(60)
            if self._scheduler_stop:
                break
            try:
                await self._run_due_unbans()
                expire_fn = getattr(self, "_expire_appeals", None)
                if expire_fn:
                    await expire_fn()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[GroupMgr] 调度任务出错({consecutive_errors}): {e}")
                if consecutive_errors >= 10:
                    logger.error("[GroupMgr] 调度器连续错误过多，暂停 5 分钟")
                    await asyncio.sleep(300)
                    consecutive_errors = 0

    async def _card_sync_loop(self) -> None:
        """周期同步成员名片，弥补协议端不发送 group_card 的实现差异。"""
        # 启动后留出连接建立时间；之后每次间隔均从最新配置读取，支持 WebUI 热更新。
        first_run = True
        consecutive_errors = 0
        while not self._scheduler_stop:
            try:
                interval = self._clamp_int(
                    self._cfg_int("card_sync_interval", 120), 120, 30, 3600
                )
                await asyncio.sleep(5 if first_run else interval)
                first_run = False
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)
                continue
            if self._scheduler_stop:
                break
            # 没有任何全局或单群启用项时不查询群列表，避免无意义 API 请求；
            # 标准入群通知仍由 `_on_group_increase_card_check` 即时处理。
            if not self._card_sync_any_group_enabled():
                continue
            try:
                await self._sync_group_cards()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[GroupMgr] 名片周期同步出错({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    # 避免协议端持续异常时刷日志和请求；下一轮仍会自动恢复。
                    await asyncio.sleep(min(300, interval))
                    consecutive_errors = 0

    def _card_sync_any_group_enabled(self) -> bool:
        """判断是否至少有一个群同时开启名片监控与周期同步。"""
        config = getattr(self, "config", {}) or {}
        if not config.get("disclaimer_agreed", False):
            return False
        if (self._cfg("enabled", True)
                and self._cfg("card_monitor_enabled", False)
                and self._cfg("card_sync_enabled", True)):
            return True
        group_ids = set()
        for attr in ("_group_white_set", "_card_sync_known_groups"):
            group_ids.update(str(x) for x in (getattr(self, attr, set()) or set()) if x)
        try:
            group_ids.update(str(x) for x in self._storage.list_configured_groups() if x)
        except Exception:
            pass
        return any(
            self._cfg("enabled", True, group_id=gid)
            and self._cfg("card_monitor_enabled", False, group_id=gid)
            and self._cfg("card_sync_enabled", True, group_id=gid)
            for gid in group_ids
        )

    async def _run_due_unbans(self) -> None:
        """执行所有到期的定时解禁。"""
        now = int(time.time())
        due = self._storage.list_due_unbans(now)
        if not due:
            return
        for item in due:
            gid = str(item.get("group_id", ""))
            uid = item.get("user_id", "")
            if not self._cfg("auto_unban_enabled", False, group_id=gid):
                continue
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
        if not group_id or not user_id:
            return
        group_id = str(group_id)
        if not self._cfg("auto_unban_enabled", False, group_id=group_id):
            return
        now = int(time.time())
        if mute_seconds and mute_seconds > 0:
            unban_at = now + int(mute_seconds)
        else:
            # 永久禁言：按配置的兜底托管时长（默认 7 天）后解禁
            fallback = self._cfg_int("auto_unban_permanent_hours", 168, group_id=group_id)
            unban_at = now + fallback * 3600
        try:
            self._storage.add_scheduled_unban(group_id, user_id, unban_at, now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 登记定时解禁失败: {e}")
