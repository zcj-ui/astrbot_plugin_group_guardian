"""防刷屏检测模块。

按 (群号, 用户ID) 追踪消息时间戳，超限自动禁言 + 可选撤回。
"""
import re
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
        * 可选夜间独立阈值：按本机时区判断小时，夜间开启后覆盖日间三档上限
        * 所有消息类型均计入（文本/图片/转发/QQ 收藏/JSON/App）
        * 管理员完全豁免（在 moderation.py 管线中提前 return）
    """

    def _init_anti_flood(self) -> None:
        """初始化防刷屏追踪数据字典和清理时间戳。"""
        self._anti_flood_data: Dict[str, Dict[str, deque]] = {}
        self._anti_flood_last_cleanup = 0.0
        # 处罚冷却表：{group_id: {user_id: 冷却到期时间戳}}。
        # 触发处罚后进入冷却，期间该用户的消息只静默处理，不再重复禁言/记日志/开申诉，
        # 用于吸收"处罚决定做出后仍在事件队列里排队的积压消息"，避免重复处罚刷屏。
        self._anti_flood_penalty_until: Dict[str, Dict[str, float]] = {}

    def _anti_flood_in_cooldown(self, group_id: str, user_id: str) -> bool:
        """判断某用户当前是否处于防刷屏处罚冷却期内（到期自动清理标记）。"""
        users = self._anti_flood_penalty_until.get(group_id)
        if not users:
            return False
        until = users.get(user_id, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            users.pop(user_id, None)
            if not users:
                self._anti_flood_penalty_until.pop(group_id, None)
            return False
        return True

    def _mark_anti_flood_penalty(self, group_id: str, user_id: str, cooldown_seconds: int) -> None:
        """登记一次防刷屏处罚：设置冷却到期时间，并清空该用户的消息计数队列。

        清空队列是关键：否则冷却结束后窗口内残留的旧消息会让用户立刻再次命中阈值。
        """
        if cooldown_seconds <= 0:
            cooldown_seconds = 60
        self._anti_flood_penalty_until.setdefault(group_id, {})[user_id] = time.time() + cooldown_seconds
        group_q = self._anti_flood_data.get(group_id)
        if group_q and user_id in group_q:
            group_q[user_id].clear()

    def _normalize_message_text(self, text: str) -> str:
        """归一化消息文本，便于重复消息检测。"""
        s = (text or "").strip().lower()
        s = re.sub(r"\s+", " ", s)
        return s

    def _record_message(self, group_id: str, user_id: str, msg_id: str, text: str = "") -> None:
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
        normalized = self._normalize_message_text(text)
        self._anti_flood_data[group_id][user_id].append((time.time(), msg_id, normalized, len(text or "")))

    @staticmethod
    def _unpack_entry(entry) -> Tuple[float, str, str, int]:
        """兼容旧结构 (ts,msg_id) 与新结构 (ts,msg_id,text,len)。"""
        if isinstance(entry, tuple):
            if len(entry) >= 4:
                return float(entry[0]), str(entry[1]), str(entry[2] or ""), int(entry[3] or 0)
            if len(entry) >= 2:
                return float(entry[0]), str(entry[1]), "", 0
        return 0.0, "", "", 0

    def _get_rate_limit(self, key: str, default: int, group_id: str = None) -> int:
        """读取防刷屏速率配置，返回值 < = 0 表示该档位被关闭。

        Args:
            key:      配置键名，例如 ``anti_flood_rate_per_second``。
            default:  默认值。
            group_id: 群号，传入时优先用该群的独立配置。

        Returns:
            int: 速率上限，< = 0 表示不检测。
        """
        return self._cfg_int(key, default, group_id=group_id)

    @staticmethod
    def _clamp_hour(value: int) -> int:
        return max(0, min(int(value), 23))

    def _is_anti_flood_night_time(self, group_id: str = None, now_ts: float = None) -> bool:
        """判断当前是否处于夜间独立限速时段。

        start == end 表示全天使用夜间阈值；跨天区间如 23 -> 6 表示 23:00-次日 06:00。
        """
        if not self._cfg("anti_flood_night_enabled", False, group_id=group_id):
            return False
        start = self._clamp_hour(self._cfg_int("anti_flood_night_start_hour", 0, group_id=group_id))
        end = self._clamp_hour(self._cfg_int("anti_flood_night_end_hour", 6, group_id=group_id))
        hour = time.localtime(now_ts if now_ts is not None else time.time()).tm_hour
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _get_effective_rate_limits(self, group_id: str = None, now_ts: float = None) -> dict:
        """返回当前时段实际使用的秒/分/时阈值。"""
        limits = {
            "per_second": self._get_rate_limit("anti_flood_rate_per_second", 5, group_id=group_id),
            "per_minute": self._get_rate_limit("anti_flood_rate_per_minute", 20, group_id=group_id),
            "per_hour": self._get_rate_limit("anti_flood_rate_per_hour", 60, group_id=group_id),
            "night": False,
        }
        if not self._is_anti_flood_night_time(group_id=group_id, now_ts=now_ts):
            return limits
        limits.update({
            "per_second": self._get_rate_limit("anti_flood_night_rate_per_second", limits["per_second"], group_id=group_id),
            "per_minute": self._get_rate_limit("anti_flood_night_rate_per_minute", limits["per_minute"], group_id=group_id),
            "per_hour": self._get_rate_limit("anti_flood_night_rate_per_hour", limits["per_hour"], group_id=group_id),
            "night": True,
        })
        return limits

    @staticmethod
    def _rate_label(name: str, night: bool) -> str:
        return f"夜间{name}" if night else name

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

        limits = self._get_effective_rate_limits(group_id=group_id, now_ts=now)
        sec_limit = limits["per_second"]
        min_limit = limits["per_minute"]
        hour_limit = limits["per_hour"]
        night = bool(limits.get("night"))
        sec_count = 0
        min_count = 0
        hour_count = 0
        sec_ids: List[str] = []
        min_ids: List[str] = []
        hour_ids: List[str] = []

        repeat_enabled = self._cfg("repeat_detect_enabled", True, group_id=group_id)
        repeat_window = self._cfg_int("repeat_detect_window_seconds", 120, group_id=group_id)
        repeat_count_limit = self._cfg_int("repeat_detect_count", 3, group_id=group_id)
        long_text_enabled = self._cfg("long_text_detect_enabled", True, group_id=group_id)
        long_text_threshold = self._cfg_int("long_text_threshold", 500, group_id=group_id)

        current_text = ""
        current_len = 0
        repeat_count = 0
        repeat_ids: List[str] = []

        for entry in reversed(dq):
            t, mid, norm_text, msg_len = self._unpack_entry(entry)
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

            if current_text == "":
                current_text = norm_text
                current_len = msg_len
            # 重复消息：在主循环内同步统计次数并收集消息 ID，避免末尾再次遍历队列。
            if repeat_enabled and repeat_window > 0 and current_text and dt < repeat_window and norm_text == current_text:
                repeat_count += 1
                repeat_ids.append(mid)

        if sec_limit > 0 and sec_count > sec_limit:
            return True, {"rate": self._rate_label("每秒", night), "count": sec_count, "limit": sec_limit,
                          "total_msgs": total_msgs, "msg_ids": sec_ids}
        if min_limit > 0 and min_count > min_limit:
            return True, {"rate": self._rate_label("每分钟", night), "count": min_count, "limit": min_limit,
                          "total_msgs": total_msgs, "msg_ids": min_ids}
        if hour_limit > 0 and hour_count > hour_limit:
            return True, {"rate": self._rate_label("每小时", night), "count": hour_count, "limit": hour_limit,
                          "total_msgs": total_msgs, "msg_ids": hour_ids}
        if long_text_enabled and long_text_threshold > 0 and current_len > long_text_threshold:
            return True, {
                "rate": "长文本",
                "count": current_len,
                "limit": long_text_threshold,
                "total_msgs": total_msgs,
                "msg_ids": sec_ids[:1] or min_ids[:1] or hour_ids[:1],
            }
        if repeat_enabled and repeat_count_limit > 1 and repeat_count >= repeat_count_limit:
            return True, {
                "rate": "重复消息",
                "count": repeat_count,
                "limit": repeat_count_limit,
                "total_msgs": total_msgs,
                "msg_ids": repeat_ids[:repeat_count],
            }
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
                while dq:
                    t, _, _, _ = self._unpack_entry(dq[0])
                    if t >= expired:
                        break
                    dq.popleft()
                if not dq:
                    del users[uid]
            if not users:
                del self._anti_flood_data[gid]
        # 同步清理已到期的处罚冷却标记，避免冷却表长期残留
        for gid, users in list(self._anti_flood_penalty_until.items()):
            for uid in list(users.keys()):
                if now >= users[uid]:
                    del users[uid]
            if not users:
                del self._anti_flood_penalty_until[gid]

    def _get_anti_flood_status(self) -> dict:
        """返回防刷屏追踪快照，供 WebUI 仪表盘 API 使用。

        Returns:
            包含 ``enabled`` / ``tracked_groups`` / ``tracked_users``
            以及按群聚合的 ``groups`` 字典。
        """
        result = {
            "enabled": self._cfg("anti_flood_enabled", True),
            "night_enabled": self._cfg("anti_flood_night_enabled", False),
            "night_active": self._is_anti_flood_night_time(),
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
                for entry in reversed(dq):
                    t, _mid, _txt, _len = self._unpack_entry(entry)
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
