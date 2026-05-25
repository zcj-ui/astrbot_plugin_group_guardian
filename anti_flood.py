"""防刷屏检测模块。

按 (群号, 用户ID) 追踪消息时间戳，超限自动禁言 + 可选撤回。
"""
import time
from collections import deque
from typing import Dict, List, Optional, Tuple


class AntiFloodMixin:
    """为 Main 提供防刷屏检测能力。

    性能:
        *_check_anti_flood 逆向单次遍历（最新->最旧），越过 3600s 立即 break，
          O(命中范围) 而非 O(队列长 200)
        * 所有限速设为 0 时直接跳过，零运行时开销
        * 每 5 分钟回收超过 2h 的过期条目，各组/用户队列上限 200 条

    特性:
        * 三档独立速率：每秒 / 每分钟 / 每小时，单档设为 0 即关闭
        * 所有消息类型均计入（文本/图片/转发/QQ 收藏/JSON/App）
        * 管理员完全豁免（在 moderation.py 管线中提前 return）
    """

    def _init_anti_flood(self) -> None:
        """初始化防刷屏追踪数据字典和清理时间戳。"""
        self._anti_flood_data: Dict[str, Dict[str, deque]] = {}
        self._anti_flood_last_cleanup = 0.0

    def _record_message(self, group_id: str, user_id: str, msg_id: str) -> None:
        """记录一条消息到对应的群/用户时间戳队列。

        Args:
            group_id: 群号。
            user_id:   发送者的 QQ 号。
            msg_id:    消息 ID，用于后续撤回。
        """
        if group_id not in self._anti_flood_data:
            self._anti_flood_data[group_id] = {}
        if user_id not in self._anti_flood_data[group_id]:
            self._anti_flood_data[group_id][user_id] = deque(maxlen=200)
        self._anti_flood_data[group_id][user_id].append((time.time(), msg_id))

    def _get_rate_limit(self, key: str, default: int) -> int:
        """读取防刷屏速率配置，返回值 < = 0 表示该档位被关闭。

        Args:
            key:      配置键名，例如 ``anti_flood_rate_per_second``。
            default:  默认值。

        Returns:
            int: 速率上限，< = 0 表示不检测。
        """
        return self._safe_int(self.config.get(key, default), default)

    def _check_anti_flood(
        self, group_id: str, user_id: str
    ) -> Tuple[bool, Optional[dict]]:
        """检查用户是否触发刷屏阈值。

        检测顺序：每秒 -> 每分钟 -> 每小时，任一档位触发即返回。

        Args:
            group_id: 群号。
            user_id:  用户 QQ 号。

        Returns:
            (False, None) —— 未触发任何阈值。
            (True, dict)  —— 已触发，dict 包含
                ``rate``       该时间窗口的名称（"每秒"/"每分钟"/"每小时"）
                ``count``      窗口内的消息数
                ``limit``      配置的阈值
                ``total_msgs`` 队列中的总消息数
                ``msg_ids``    窗口内的消息 ID 列表
        """
        data = self._anti_flood_data
        if group_id not in data or user_id not in data[group_id]:
            return False, None

        dq = data[group_id][user_id]
        total_msgs = len(dq)
        now = time.time()

        sec_limit = self._get_rate_limit("anti_flood_rate_per_second", 5)
        min_limit = self._get_rate_limit("anti_flood_rate_per_minute", 20)
        hour_limit = self._get_rate_limit("anti_flood_rate_per_hour", 60)
        if sec_limit <= 0 and min_limit <= 0 and hour_limit <= 0:
            return False, None

        sec_count = 0
        min_count = 0
        hour_count = 0
        sec_ids: List[str] = []
        min_ids: List[str] = []
        hour_ids: List[str] = []

        for t, mid in reversed(dq):
            dt = now - t
            if dt >= 3600:
                break
            hour_count += 1
            hour_ids.append(mid)
            if dt < 60:
                min_count += 1
                min_ids.append(mid)
            if dt < 1:
                sec_count += 1
                sec_ids.append(mid)

        if sec_limit > 0 and sec_count > sec_limit:
            return True, {"rate": "每秒", "count": sec_count, "limit": sec_limit,
                          "total_msgs": total_msgs, "msg_ids": sec_ids}
        if min_limit > 0 and min_count > min_limit:
            return True, {"rate": "每分钟", "count": min_count, "limit": min_limit,
                          "total_msgs": total_msgs, "msg_ids": min_ids}
        if hour_limit > 0 and hour_count > hour_limit:
            return True, {"rate": "每小时", "count": hour_count, "limit": hour_limit,
                          "total_msgs": total_msgs, "msg_ids": hour_ids}
        return False, None

    def _anti_flood_cleanup(self) -> None:
        """清理超过 2 小时未被写入的过期条目。

        每 5 分钟执行一次，同时移除空队列和空群条目，防止内存无限增长。
        """
        now = time.time()
        if now - self._anti_flood_last_cleanup < 300:
            return
        self._anti_flood_last_cleanup = now
        expired = now - 7200
        for gid, users in list(self._anti_flood_data.items()):
            for uid in list(users.keys()):
                dq = users[uid]
                while dq and dq[0][0] < expired:
                    dq.popleft()
                if not dq:
                    del users[uid]
            if not users:
                del self._anti_flood_data[gid]

    def _get_anti_flood_status(self) -> dict:
        """返回防刷屏追踪快照，供 WebUI 仪表盘 API 使用。

        Returns:
            包含 ``enabled`` / ``tracked_groups`` / ``tracked_users``
            以及按群聚合的 ``groups`` 字典。
        """
        result = {
            "enabled": self._cfg("anti_flood_enabled", True),
            "tracked_groups": 0,
            "tracked_users": 0,
            "groups": {},
        }
        if not self._anti_flood_data:
            return result
        now = time.time()
        total_users = 0
        for gid, users in self._anti_flood_data.items():
            group_users = {}
            for uid, dq in users.items():
                total_msgs = len(dq)
                if total_msgs == 0:
                    continue
                total_users += 1
                s = m = h = 0
                for t, _mid in reversed(dq):
                    dt = now - t
                    if dt >= 3600:
                        break
                    h += 1
                    if dt < 60:
                        m += 1
                    if dt < 1:
                        s += 1
                group_users[uid] = {
                    "total_msgs": total_msgs,
                    "per_second": s,
                    "per_minute": m,
                    "per_hour": h,
                }
            if group_users:
                result["groups"][gid] = {"users": group_users}
        result["tracked_groups"] = len(result["groups"])
        result["tracked_users"] = total_users
        return result
