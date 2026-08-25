# -*- coding: utf-8 -*-
"""内存自动回收机制（v2.26.0）。

周期性监控进程内存，超过阈值时主动回收，缓解长时间运行被 OOM 杀死的问题：
- 零依赖跨平台读内存（psutil / /proc/self/statm / Windows ctypes）；
- 强制 gc.collect() + 清理可重建缓存 + 裁剪视频指纹缓存；
- 不清理视频临时目录（即用即删，避免中断在途审核）。
"""

import asyncio
import os

from astrbot.api import logger


class MemoryGuardMixin:
    """内存自动回收能力，由 SchedulerMixin 组合使用。"""

    @staticmethod
    def _current_process_memory_mb() -> float:
        """返回当前进程 RSS 内存（MB）；无法获取返回 -1.0。"""
        try:
            import psutil  # type: ignore

            return psutil.Process().memory_info().rss / 1024.0 / 1024.0
        except Exception:
            pass
        try:
            with open("/proc/self/statm", "r", encoding="utf-8") as f:
                parts = f.read().split()
            if len(parts) >= 2:
                page_size = os.sysconf("SC_PAGE_SIZE")
                return int(parts[1]) * page_size / 1024.0 / 1024.0
        except Exception:
            pass
        try:
            import ctypes  # type: ignore
            from ctypes import wintypes  # type: ignore

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            kernel32 = ctypes.windll.kernel32
            psapi = getattr(ctypes.windll, "psapi", None)
            if psapi is None:
                return -1.0
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PMC),
                ctypes.c_size_t,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return counters.WorkingSetSize / 1024.0 / 1024.0
        except Exception:
            pass
        return -1.0

    def _memory_guard_clean_cache_entries(self) -> None:
        """清理插件内各类内存缓存（防御式遍历，均可重建）。"""
        for attr in ("_recent_media_hashes", "_recent_video_fingerprints"):
            cache = getattr(self, attr, None)
            if isinstance(cache, dict):
                try:
                    cache.clear()
                except Exception:
                    pass
        for attr in ("_query_cache", "_web_group_cache", "_admin_role_cache",
                     "_card_snapshots"):
            cache = getattr(self, attr, None)
            if isinstance(cache, dict):
                try:
                    cache.clear()
                except Exception:
                    pass
        stats = getattr(self, "_stats_cache", None)
        if isinstance(stats, dict):
            try:
                stats["group_stats"] = {}
                stats["user_stats"] = {}
            except Exception:
                pass

    def _memory_guard_trim_video_fp_cache(self) -> None:
        """视频指纹缓存裁剪：保留最近 100 条。"""
        cache = getattr(self, "_video_fp_cache", None)
        if not isinstance(cache, dict) or len(cache) <= 100:
            return
        try:
            stale_keys = sorted(cache, key=cache.get)[: len(cache) - 100]
            for key in stale_keys:
                cache.pop(key, None)
            save_fn = getattr(self, "_save_json_file", None)
            if callable(save_fn):
                save_fn("video_fingerprint_cache.json", cache)
        except Exception:
            pass

    def _run_memory_guard(self) -> None:
        """按配置执行一次内存回收：GC + 缓存清理 + 指纹裁剪。"""
        threshold_mb = self._cfg_int("memory_guard_threshold_mb", 0)
        used_mb = self._current_process_memory_mb()
        if threshold_mb > 0 and (used_mb < 0 or used_mb < threshold_mb):
            return
        freed = -1
        try:
            import gc

            freed = gc.collect()
        except Exception:
            pass
        self._memory_guard_clean_cache_entries()
        self._memory_guard_trim_video_fp_cache()
        if used_mb >= 0:
            logger.info(
                f"[GroupMgr] 内存自动回收完成: 当前 {used_mb:.0f}MB / "
                f"阈值 {threshold_mb if threshold_mb > 0 else '不限'}MB, "
                f"gc 释放 {freed} 个对象"
            )

    async def _memory_guard_loop(self) -> None:
        """周期性检查内存并在需要时回收。"""
        while not getattr(self, "_scheduler_stop", False):
            try:
                interval = max(30, self._cfg_int("memory_guard_interval_sec", 60))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(60)
                continue
            if getattr(self, "_scheduler_stop", False):
                break
            if not self._cfg("memory_guard_enabled", True):
                continue
            try:
                self._run_memory_guard()
            except Exception as exc:
                logger.debug(f"[GroupMgr] 内存回收执行失败: {exc}")
