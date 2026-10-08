# -*- coding: utf-8 -*-
import asyncio
import inspect
import time
from typing import Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

try:
    from .single_flight import shared_task, wait_shared
except ImportError:  # 独立加载 onebot.py 的单元测试兼容路径
    from single_flight import shared_task, wait_shared

# OneBot API 调用统一超时（秒），防止协议端无响应导致协程永久挂起
ONEBOT_CALL_TIMEOUT = 20.0


class OneBotMixin:
    # 统一封装 OneBot / AIOCQHTTP 客户端获取和 API 调用。
    # _get_client 有多级回退：先从事件中取 -> 从缓存的 self._client 取 -> 从 platform_manager 中获取。
    # _call_group_api 对所有群管理 API 做统一的返回值兼容处理。
    async def _get_client(self, event: AstrMessageEvent = None):
        # 三级回退：优先从 event.bot 取，其次用缓存的 self._client，最后遍历 platform_manager 查找可用实例。
        # AstrBot 文档里的 aiocqhttp client 调用形态是 client.api.call_action()；旧版本/部分适配器
        # 也可能直接暴露 client.call_action()。这里统一归一化为"可直接 call_action 的对象"。
        if event:
            client = self._normalize_action_client(getattr(event, 'bot', None))
            if client:
                self._client = client
                return client
        cached = self._normalize_action_client(self._client)
        if cached:
            self._client = cached
            return cached
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts() or []
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if inspect.isawaitable(client):
                        client = await client
                    client = self._normalize_action_client(client)
                    if client:
                        self._client = client
                        return client
                for attr in ("client", "bot", "api"):
                    client = self._normalize_action_client(getattr(platform, attr, None))
                    if client:
                        self._client = client
                        return client
        except Exception as e:
            logger.debug(f"[GroupMgr] 从 platform_manager 获取 client 失败: {e}")
        return None

    @staticmethod
    def _normalize_action_client(candidate):
        if not candidate:
            return None
        if hasattr(candidate, 'call_action'):
            return candidate
        api = getattr(candidate, 'api', None)
        if api and hasattr(api, 'call_action'):
            return api
        return None

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, 'group_id') and event.group_id:
                return str(event.group_id)
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                gid = event.message_obj.group_id
                if gid:
                    return str(gid)
            if hasattr(event, 'raw_message') and hasattr(event.raw_message, 'group_id'):
                gid = event.raw_message.group_id
                if gid:
                    return str(gid)
            for raw in (
                getattr(event, 'raw_event', None),
                getattr(event, 'raw_message', None),
                getattr(getattr(event, 'message_obj', None), 'raw_message', None),
            ):
                if isinstance(raw, dict) and raw.get('group_id'):
                    return str(raw['group_id'])
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception as _e:
            logger.debug(f"[GroupMgr] _get_group_id fallback: {_e}")
        return ""

    def _try_get_sender_id(self, event: AstrMessageEvent) -> str:
        for getter in [
            lambda: str(event.get_sender_id()) if event.get_sender_id() else None,
            lambda: str(event.sender.user_id) if hasattr(event, 'sender') and hasattr(event.sender, 'user_id') else None,
            lambda: str(event.user_id) if hasattr(event, 'user_id') else None,
            lambda: str((getattr(event, 'raw_event', None) or {}).get('user_id') or (getattr(event, 'raw_event', None) or {}).get('sender', {}).get('user_id')) or None,
            lambda: str(event.message_obj.sender.user_id) if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id') else None,
        ]:
            try:
                result = getter()
                if result and result != 'None':
                    return result
            except Exception:
                pass
        return ""

    def _get_all_admin_ids(self) -> set:
        # 合并插件管理员名单(DB) + AstrBot 全局 admin_id
        try:
            astrbot_admin_ids = []
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            return set(self._get_admin_list()) | set(astrbot_admin_ids)
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取管理员名单失败: {e}")
            return set(self._get_admin_list())

    # 权限表查询短缓存。防刷屏对每条消息都要调用 _is_admin，每次判定原本要新开 2~3 个
    # SQLite 连接（群权限黑名单、群超管、F5 授权），同步阻塞事件循环。缓存与成员角色共用
    # _admin_role_cache（键带 "|" 与 "群号:QQ" 角色键区分）：所有权限写入路径都会 clear()
    # 它，写入后立即失效。
    PERM_CACHE_TTL = 30.0

    def _cached_perm(self, kind: str, key: str, loader):
        cache = getattr(self, "_admin_role_cache", None)
        if cache is None:
            return loader()
        cache_key = f"{kind}|{key}"
        now = time.time()
        hit = cache.get(cache_key)
        if hit and now - hit[1] < self.PERM_CACHE_TTL:
            return hit[0]
        value = loader()
        cache[cache_key] = (value, now)
        return value

    def _is_group_admin_blocked(self, group_id: str, user_id: str) -> bool:
        if not group_id:
            return False
        try:
            return self._cached_perm(
                "blocked", f"{group_id}:{user_id}",
                lambda: bool(self._storage.is_group_admin_blocked(group_id, user_id)),
            )
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群权限黑名单失败: {e}")
            return False

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        # "群操作权限"判定，判定顺序：
        #   ① 群级 bot 权限黑名单(最高优先) → ② 全局管理员名单
        #   → ③ 群超管 → ④ 群角色授权（白名单内 + F5/legacy 开关）
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        group_id = self._get_group_id(event)

        # ① 群级 bot 权限黑名单
        if self._is_group_admin_blocked(group_id, user_id):
            return False

        # ② 全局管理员名单
        if user_id in self._get_all_admin_ids():
            return True

        if not group_id:
            return False

        # ③ 群超管：在 WebUI 为该群单独设置的专属管理员
        try:
            if self._cached_perm(
                "super", f"{group_id}:{user_id}",
                lambda: bool(self._storage.is_group_super_admin(group_id, user_id)),
            ):
                return True
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群超管失败: {e}")

        # ④ 群角色判定
        role = await self._get_member_role(event, group_id, user_id)
        if role not in ("admin", "owner"):
            return False

        # 群角色授权仅在"允许管理的群"内生效：配置了白名单时必须在白名单内；
        # 未配置白名单时，黑名单群一律不授权。这样群主/群管的群操作权限被限定在
        # 其拥有管理权且被允许的群，避免任意群的群管自动获得本插件群操作能力。
        if self._group_white_set:
            if group_id not in self._group_white_set:
                return False
        elif self._group_black_set and group_id in self._group_black_set:
            return False

        # F5 动态授权：若该群在授权表且启用，按 grant_owner/grant_admin 实时判定
        if self._cfg("group_admin_grant_enabled", False):
            grant = self._cached_perm(
                "grant", str(group_id), lambda: self._storage.get_group_admin_grant(group_id),
            )
            if grant and grant.get("enabled"):
                if role == "owner" and grant.get("grant_owner"):
                    return True
                if role == "admin" and grant.get("grant_admin"):
                    return True
                return False  # 该群已显式配置授权，但当前角色不在授权范围
        # 老行为兼容：未配置 F5 时，允许范围内的 owner/admin 默认拥有群操作权限（可由开关关闭）
        return self._cfg("legacy_role_admin_enabled", True)

    async def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """"插件全局管理员"判定：仅认全局插件管理员名单 + AstrBot 全局 admin_id。

        与 _is_admin 的区别：群主/群管理员/群超管的"群角色授权"不算插件管理员。
        用于真正的插件级操作（管理插件管理员名单、改全局运行开关等）。
        """
        user_id = self._try_get_sender_id(event)
        if not user_id:
            return False
        if self._is_group_admin_blocked(self._get_group_id(event), user_id):
            return False
        return user_id in self._get_all_admin_ids()

    # 成员角色缓存过期后、在 MEMBER_ROLE_STALE_MAX 内仍先返回旧值并后台刷新：防刷屏对每条消息
    # 都要判定发送者是否管理员，同步等协议端会让协议端一慢、整群消息都跟着卡住。
    # 查询失败后 MEMBER_ROLE_FAIL_BACKOFF 内不再重查，避免协议端变慢时形成请求风暴。
    MEMBER_ROLE_STALE_MAX = 600.0
    MEMBER_ROLE_FAIL_BACKOFF = 5.0
    MEMBER_ROLE_CACHE_MAX = 1000

    async def _fetch_member_role(self, event, group_id: str, user_id: str, cache_key: str) -> str:
        """实际查询成员角色并写缓存；失败返回 '' 并记录退避时间。"""
        epoch = getattr(self, "_role_epoch", 0)
        role = None
        try:
            client = await self._get_client(event)
            if client:
                ok, info, error = await self._call_group_api_result(
                    client, 'get_group_member_info', '获取群成员信息',
                    group_id=self._safe_int(group_id, 0), user_id=self._safe_int(user_id, 0),
                    no_cache=False,
                )
                if ok and isinstance(info, dict):
                    role = info.get('role', '') or ""
                elif error:
                    logger.debug(f"[GroupMgr] 获取群成员信息失败: {error}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取群成员信息失败: {e}")
        now = time.time()
        if role is None:
            fails = getattr(self, "_member_role_fail", None)
            if fails is None or len(fails) >= self.MEMBER_ROLE_CACHE_MAX:
                fails = self._member_role_fail = {}
            fails[cache_key] = now
            return ""
        # 查询期间收到过管理员变动通知：结果可能是变动前的角色，不写缓存
        if getattr(self, "_role_epoch", 0) == epoch:
            self._admin_role_cache[cache_key] = (role, now)
        return role

    async def _get_member_role(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """获取成员在群里的角色（member/admin/owner），带缓存。

        缓存存"角色字符串"而非"是否管理员"，使 F5 授权配置变更后无需等缓存过期即可反映。
        同一成员的并发查询合并为一次；缓存过期但未太旧时先返回旧值并后台刷新。
        群管理员变动通知会调用 _invalidate_member_roles 立即失效该群缓存，"下管理"不必等刷新。
        """
        cache_key = f"{group_id}:{user_id}"
        now = time.time()
        if len(self._admin_role_cache) > self.MEMBER_ROLE_CACHE_MAX:
            self._admin_role_cache = {
                k: v for k, v in self._admin_role_cache.items()
                if now - v[1] < self.MEMBER_ROLE_STALE_MAX
            }
        cached = self._admin_role_cache.get(cache_key)
        if cached and now - cached[1] < self._admin_role_cache_ttl:
            return cached[0]
        if not self._safe_int(group_id, 0) or not self._safe_int(user_id, 0):
            return ""
        stale = cached[0] if cached and now - cached[1] < self.MEMBER_ROLE_STALE_MAX else None
        fails = getattr(self, "_member_role_fail", None)
        if fails and now - fails.get(cache_key, 0) < self.MEMBER_ROLE_FAIL_BACKOFF:
            return stale if stale is not None else ""
        task = shared_task(
            self, "_member_role_inflight", cache_key,
            lambda: self._fetch_member_role(event, group_id, user_id, cache_key),
        )
        if stale is not None:
            return stale
        ok, role = await wait_shared(task)
        return role if ok else ""

    def _bump_role_epoch(self, inflight_attr: str, match=None) -> None:
        """角色缓存失效：推进纪元，使进行中的旧查询结果不再写缓存，并让下次调用重新发起查询。

        match 为 None 时丢弃全部进行中查询，否则只丢弃 match(key) 为真的。
        """
        self._role_epoch = getattr(self, "_role_epoch", 0) + 1
        inflight = getattr(self, inflight_attr, None)
        if inflight:
            for key in [k for k in inflight if match is None or match(str(k))]:
                inflight.pop(key, None)

    def _invalidate_member_roles(self, group_id=None) -> None:
        """群管理员变动后失效该群的成员角色缓存；group_id 为空时清空全部。"""
        prefix = f"{group_id}:" if group_id else ""
        self._bump_role_epoch("_member_role_inflight", (lambda k: k.startswith(prefix)) if prefix else None)
        cache = getattr(self, "_admin_role_cache", None)
        if not cache:
            return
        if not group_id:
            cache.clear()
            return
        for key in [k for k in cache if k.startswith(prefix)]:
            cache.pop(key, None)

    async def _get_bot_uin(self, client) -> int:
        """获取当前 bot 自身 QQ 号（带缓存）。失败返回 0。"""
        cached = getattr(self, "_bot_uin_cache", 0)
        if cached:
            return cached
        try:
            ok, info, error = await self._call_group_api_result(
                client, "get_login_info", "获取 bot QQ"
            )
            if not ok:
                if error:
                    logger.debug(f"[GroupMgr] 获取 bot QQ 失败: {error}")
                return 0
            uin = self._safe_int(info.get("user_id", 0), 0) if isinstance(info, dict) else 0
            if uin:
                self._bot_uin_cache = uin
            return uin
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取 bot QQ 失败: {e}")
            return 0

    async def _get_role_by_id(self, client, group_id, user_id) -> str:
        """直接用 client 查某成员在群里的角色（member/admin/owner），无 event 版本。失败返回 ''。"""
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid or not client:
            return ""
        try:
            ok, info, error = await self._call_group_api_result(
                client, "get_group_member_info", "查询群成员角色",
                group_id=gid, user_id=uid, no_cache=False,
            )
            if ok and isinstance(info, dict):
                return info.get("role", "") or ""
            if error:
                logger.debug(f"[GroupMgr] 查询群成员角色失败({group_id}/{user_id}): {error}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群成员角色失败({group_id}/{user_id}): {e}")
        return ""

    # 机器人自身群角色缓存（Issue #89）：角色很少变化，确认值缓存 5 分钟；
    # 查询失败只短缓存 30 秒，避免每条消息都重查两次 API，又能较快恢复。
    # 机器人被设/撤管理员时 group_admin 通知会调用 _invalidate_bot_role 立即失效。
    # 消息热路径最多等 BOT_ROLE_HOT_WAIT 秒：同群并发查询合并为一次，缓存过期时先用旧值。
    BOT_ROLE_CACHE_TTL = 300.0
    BOT_ROLE_FAIL_TTL = 30.0
    BOT_ROLE_CACHE_MAX = 2000
    BOT_ROLE_HOT_WAIT = 1.5

    async def _fetch_bot_group_role(self, event, gid: str) -> str:
        """实际查询机器人群角色并写缓存，失败返回 ''。"""
        epoch = getattr(self, "_role_epoch", 0)
        role = ""
        try:
            client = await self._get_client(event)
            if client:
                uin = await self._get_bot_uin(client)
                if uin:
                    role = await self._get_role_by_id(client, gid, uin)
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询机器人群角色失败({gid}): {e}")
            role = ""
        if getattr(self, "_role_epoch", 0) != epoch:
            return role  # 查询期间角色缓存被失效（机器人刚被设/撤管理员），结果可能过时，不写缓存
        cache = getattr(self, "_bot_role_cache", None)
        if cache is None:
            cache = self._bot_role_cache = {}
        if len(cache) >= self.BOT_ROLE_CACHE_MAX and gid not in cache:
            cache.clear()
        cache[gid] = (role, time.time(), self.BOT_ROLE_CACHE_TTL if role else self.BOT_ROLE_FAIL_TTL)
        return role

    async def _get_bot_group_role(self, event, group_id, max_wait=None, allow_stale=False) -> str:
        """返回机器人在某群的角色（owner/admin/member），查询失败或未及时返回时为 ''。

        同一群的并发查询共享一个进行中的请求。allow_stale=True 时缓存过期也先返回
        上次确认的角色并后台刷新；max_wait 限制本次最多等待的秒数，超时返回 ''，
        查询在后台继续并写入缓存。
        """
        gid = str(group_id or "")
        if not gid:
            return ""
        cache = getattr(self, "_bot_role_cache", None)
        if cache is None:
            cache = self._bot_role_cache = {}
        hit = cache.get(gid)
        if hit and time.time() - hit[1] < hit[2]:
            return hit[0]
        task = shared_task(self, "_bot_role_inflight", gid, lambda: self._fetch_bot_group_role(event, gid))
        if allow_stale and hit and hit[0]:
            return hit[0]
        ok, role = await wait_shared(task, max_wait)
        return role if ok else ""

    def _invalidate_bot_role(self, group_id=None) -> None:
        """机器人群角色变化后失效缓存；group_id 为空时清空全部。"""
        gid = str(group_id) if group_id else ""
        self._bump_role_epoch("_bot_role_inflight", (lambda k: k == gid) if gid else None)
        cache = getattr(self, "_bot_role_cache", None)
        if not cache:
            return
        if group_id:
            cache.pop(str(group_id), None)
        else:
            cache.clear()

    async def _bot_can_moderate(self, event, group_id) -> bool:
        """机器人在该群能否执行撤回/禁言（Issue #89）。

        仅在确认机器人是普通成员时返回 False；角色查询失败时返回 True，
        保持原有行为，避免协议端偶发故障导致审核被静默关闭。
        受 moderation_require_bot_admin（默认开，可按群覆盖）控制。
        """
        gid = str(group_id or "")
        if not gid or not self._cfg("moderation_require_bot_admin", True, group_id=gid):
            return True
        role = await self._get_bot_group_role(
            event, gid, max_wait=self.BOT_ROLE_HOT_WAIT, allow_stale=True,
        )
        if not role:
            return True
        if role in ("admin", "owner"):
            return True
        # 同一群最多每小时提醒一次，便于管理员知道为什么这个群不审核
        warned = getattr(self, "_bot_role_warned", None)
        if warned is None:
            warned = {}
            self._bot_role_warned = warned
        now = time.time()
        if now - warned.get(gid, 0) >= 3600:
            if len(warned) >= self.BOT_ROLE_CACHE_MAX:
                warned.clear()
            warned[gid] = now
            logger.warning(
                f"[GroupMgr] 机器人在群 {gid} 不是管理员/群主，无法撤回或禁言，已跳过该群的自动审核"
                "（可在配置 moderation_require_bot_admin 中关闭此判断）"
            )
        return False

    async def _precheck_member_action(self, client, group_id, target_uid, action: str) -> Tuple[bool, str]:
        """群成员操作前置校验：检查 bot 自身权限 + 目标角色，避免必然失败的调用。

        规则（OneBot/QQ 平台限制）：
          - bot 必须是管理员或群主，否则无法禁言/踢人/改名片等；
          - 不能对群主执行（禁言/踢/改名片）；
          - 普通管理员（bot 非群主）不能操作其他管理员。
        仅对写操作做检查；返回 (允许, 错误说明)。
        """
        # 仅这些操作需要目标角色保护
        if action not in ("ban", "kick", "set_card", "set_title", "set_admin", "unset_admin"):
            return True, ""
        bot_uin = await self._get_bot_uin(client)
        bot_role = await self._get_role_by_id(client, group_id, bot_uin) if bot_uin else ""
        if bot_role not in ("admin", "owner"):
            return False, "机器人在该群不是管理员/群主，无法执行群管操作"
        # QQ 平台任免管理员仅群主可执行，bot 为普通管理员时调用必败，提前拦截并给准确文案
        if action in ("set_admin", "unset_admin") and bot_role != "owner":
            return False, "设置/取消管理员需要机器人为群主"
        target_role = await self._get_role_by_id(client, group_id, target_uid)
        if target_role == "owner":
            return False, "目标是群主，无法操作"
        if target_role == "admin" and bot_role != "owner":
            return False, "目标是管理员，机器人需为群主才能操作"
        return True, ""

    def _shut_remain_seconds(self, item: dict) -> int:
        """统一解析 get_group_shut_list 条目的剩余禁言秒数（不同 OneBot 实现字段不同）。

        优先 shut_up_timestamp（解禁时刻戳，NapCat 等），其次 duration（剩余秒数，go-cqhttp 等），
        都取不到返回 -1（调用方显示"未知"）。
        """
        ts = self._safe_int(item.get("shut_up_timestamp", 0), 0)
        if ts > 0:
            remain = ts - int(time.time())
            return remain if remain > 0 else 0
        dur = self._safe_int(item.get("duration", -1), -1)
        return dur if dur >= 0 else -1

    async def _check_set_admin_operator(self, event: AstrMessageEvent, group_id: str) -> Tuple[bool, str]:
        """设置/取消管理员的操作者权限校验（指令与 LLM 工具两路径共用，保证一致）。

        严格模式(set_admin_require_owner)：仅本群群主，插件管理员也不例外；
        非严格模式：插件管理员任意群可用；否则要求群在白名单内且操作者为群主。
        """
        operator = self._try_get_sender_id(event)
        if self._cfg("set_admin_require_owner", False, group_id=group_id):
            role = await self._get_member_role(event, group_id, operator) if operator else ""
            if role != "owner":
                return False, "本群已开启严格模式：仅群主可以设置/取消群管理员"
            return True, ""
        if await self._is_plugin_admin(event):
            return True, ""
        if not (self._group_white_set and group_id in self._group_white_set):
            return False, "此功能仅对白名单群开放，请联系插件管理员将本群加入白名单"
        role = await self._get_member_role(event, group_id, operator) if operator else ""
        if role != "owner":
            return False, "仅本群群主或插件管理员可以设置/取消群管理员"
        return True, ""

    def _check_group_access(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        group_id = self._get_group_id(event)
        if not group_id:
            return True, ""
        if self._group_black_set and group_id in self._group_black_set:
            return False, f"群 {group_id} 在黑名单中"
        if self._group_white_set:
            if group_id not in self._group_white_set:
                return False, f"群 {group_id} 不在白名单中"
        return True, ""

    def _event_platform_name(self, event: AstrMessageEvent) -> str:
        """事件来源的平台适配器类型（aiocqhttp / qq_official 等），取不到返回空串。"""
        for getter in (
            lambda: event.get_platform_name(),
            lambda: event.platform_meta.name,
        ):
            try:
                name = getter()
            except Exception:
                continue
            if isinstance(name, str) and name.strip():
                return name.strip().lower()
        return ""

    def _onebot_unsupported_message(self, event: AstrMessageEvent) -> str:
        """Issue #92：事件来自不提供 OneBot 接口的平台（如 QQ 官方机器人）时返回说明，否则返回空串。

        群管操作全部依赖 OneBot 动作（set_group_ban / set_group_whole_ban 等），
        QQ 官方机器人接口既不提供这些动作，也拿不到群主/管理员身份，旧行为只会给出
        「仅管理员可以使用此功能」或「无法获取QQ客户端」，让人误以为是名单配置问题。
        取不到平台名、或事件自带可 call_action 的客户端时一律放行，不影响 OneBot 及自定义适配器。
        """
        platform = self._event_platform_name(event)
        if not platform or platform == "aiocqhttp":
            return ""
        if self._normalize_action_client(getattr(event, "bot", None)):
            return ""
        return (
            f"当前平台（{platform}）不支持此功能：禁言、踢人、全体禁言、群公告等群管操作依赖 OneBot 协议接口，"
            "需通过 aiocqhttp 适配器接入 NapCat / Lagrange 等协议端使用。"
            "QQ 官方机器人接口不提供这些群管理能力，也无法识别群主/管理员身份。"
        )

    def _admin_denied_message(self, event: AstrMessageEvent, base: str) -> str:
        """权限拒绝文案。发送者 ID 不是 QQ 号（如 QQ 官方机器人的 openid）时附上实际 ID，
        便于加入管理员名单——名单按发送者 ID 原样比对，填 QQ 号匹配不上（Issue #92）。"""
        user_id = self._try_get_sender_id(event)
        if not user_id or user_id.isdigit():
            return base
        return (
            f"{base}\n当前平台识别到的你的用户ID为：{user_id}（不是QQ号）。"
            "如需授权，请将此ID加入插件管理员名单（WebUI「插件管理员」或 AstrBot 管理员 ID）。"
        )

    async def _check_admin_cfg_access(self, event: AstrMessageEvent, cfg_key: str, feature_name: str, need_admin: bool = True) -> Tuple[bool, str]:
        # 复合检查：平台能力 → 管理员身份 → _cfg_check（插件/功能启用状态，按群）→ 群黑白名单，任一失败即拒绝。
        # 本方法的调用方均为依赖 OneBot 接口的群操作，平台不支持时先说明，避免误导为权限问题。
        unsupported = self._onebot_unsupported_message(event)
        if unsupported:
            return False, unsupported
        if need_admin and not await self._is_admin(event):
            return False, self._admin_denied_message(event, "仅管理员可以使用此功能")
        gid = self._get_group_id(event)
        # Issue #31：可选严格模式，群管操作指令要求操作者本群角色为群主/群管理员，
        # 即使是插件全局管理员，在其非群管的群里也不能通过聊天指令禁言/踢人（防止跨群乱操作）。
        # 仅约束写操作（need_admin=True），查询指令不受影响；WebUI 远程执行走独立路径不受此限。
        if need_admin and gid and self._cfg("member_action_require_group_role", False, group_id=gid):
            operator = self._try_get_sender_id(event)
            role = await self._get_member_role(event, gid, operator) if operator else ""
            if role not in ("owner", "admin"):
                return False, "本群已开启严格模式：仅群主或群管理员可通过指令执行群管操作"
        ok, msg = self._cfg_check(cfg_key, feature_name, group_id=gid)
        if not ok:
            return False, msg
        allowed, reason = self._check_group_access(event)
        if not allowed:
            return False, reason
        return True, ""

    async def _prepare_group_member_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        user_id,
        precheck_action: str = "",
    ) -> Tuple[bool, str, object, int, int]:
        """统一准备群成员操作所需的权限、client、群号和目标 QQ 号。

        precheck_action 非空时，额外做 bot 自身权限 + 目标角色（群主/管理员）预检，
        避免对群主/管理员执行必然失败的操作。
        """
        ok, err = await self._check_admin_cfg_access(event, cfg_key, feature_name)
        if not ok:
            return False, err, None, 0, 0
        _, client, gid, err = await self._get_group_client(event, need_gid=True)
        if not client:
            return False, err, None, 0, 0
        uid = self._safe_int(user_id, 0)
        if not uid:
            return False, "用户QQ号格式无效", None, 0, 0
        if precheck_action:
            ok_pre, pre_msg = await self._precheck_member_action(client, gid, uid, precheck_action)
            if not ok_pre:
                return False, pre_msg, None, 0, 0
        return True, "", client, gid, uid

    async def _prepare_group_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        need_admin: bool = True,
        need_gid: bool = True,
    ) -> Tuple[bool, str, object, int]:
        """统一准备群操作所需的权限、client 和可选群号。"""
        ok, err = await self._check_admin_cfg_access(event, cfg_key, feature_name, need_admin=need_admin)
        if not ok:
            return False, err, None, 0
        if need_gid:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                return False, err, None, 0
            return True, "", client, gid
        _, client, err = await self._get_group_client(event)
        if not client:
            return False, err, None, 0
        return True, "", client, 0

    async def _prepare_message_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        message_id,
    ) -> Tuple[bool, str, object, int]:
        """统一准备基于 message_id 的操作。"""
        ok, err, client, _ = await self._prepare_group_action(
            event, cfg_key, feature_name, need_gid=False
        )
        if not ok:
            return False, err, None, 0
        mid = self._safe_int(message_id, 0)
        if not mid:
            return False, "消息ID格式无效", None, 0
        return True, "", client, mid

    async def _get_group_client(self, event: AstrMessageEvent, need_gid: bool = False) -> Tuple:
        # 同时获取 group_id（字符串）和 client，并按 need_gid 决定是否返回 int 格式的 gid。
        group_id = self._get_group_id(event)
        if not group_id:
            return (None, None, None, "无法获取群号") if need_gid else (None, None, "无法获取群号")
        client = await self._get_client(event)
        if not client:
            return (None, None, None, "无法获取QQ客户端") if need_gid else (None, None, "无法获取QQ客户端")
        if need_gid:
            gid = self._safe_int(group_id, 0)
            if not gid:
                return None, None, None, "群号格式无效"
            return group_id, client, gid, ""
        return group_id, client, ""

    async def _call_group_api_result(self, client, action: str,
                                     result_name: str = "", **kwargs):
        """Call OneBot with timeout and return ``(ok, data, error)``."""
        try:
            result = await asyncio.wait_for(client.call_action(action, **kwargs), timeout=ONEBOT_CALL_TIMEOUT)
            ok, err = self._check_api_result(result, result_name or action)
            if not ok:
                return False, None, err
            return True, self._extract_data_result(result), ""
        except asyncio.TimeoutError:
            return False, None, f"{result_name or action} 超时（协议端 {ONEBOT_CALL_TIMEOUT:.0f}s 无响应）"
        except Exception as e:
            err = str(e)
            # Issue #34：QQ 权限错误的原始 OIDB 报文对用户不友好，转成可读提示
            if "ERR_NO_PERMISSION" in err or "120101007" in err:
                return False, None, "机器人在该群权限不足（需为管理员/群主，且不能对同级或更高身份操作）"
            return False, None, err

    async def _call_action_checked(self, client, action: str, result_name: str = "", **kwargs):
        """带超时与结果校验地调用 OneBot，成功时原样返回协议端结果，失败时抛 RuntimeError。

        供查询类指令/工具使用：它们需要自行解析原始结果（列表字段名各协议端不同），
        此前直接裸调 client.call_action——没有超时（协议端不响应即永久挂起），
        也不校验 status/retcode（失败包被当成「空列表」显示给用户）。
        调用方均在 try/except Exception 中，异常会转为明确的失败提示。
        """
        name = result_name or action
        try:
            result = await asyncio.wait_for(
                client.call_action(action, **kwargs), timeout=ONEBOT_CALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"{name} 超时（协议端 {ONEBOT_CALL_TIMEOUT:.0f}s 无响应）")
        ok, err = self._check_api_result(result, name)
        if not ok:
            raise RuntimeError(err or f"{name} 失败")
        return result

    async def _call_group_api(self, client, action: str, result_name: str = "", **kwargs) -> Tuple[bool, str]:
        # 调用 OneBot API 并用 _check_api_result 统一判断结果（status=failed 或 retcode!=0 视为失败）。
        # 统一加 20s 超时，防止协议端无响应导致协程永久挂起。
        ok, _data, error = await self._call_group_api_result(
            client, action, result_name, **kwargs
        )
        return ok, error

    async def _recall_msg(self, event: AiocqhttpMessageEvent, msg_id: str) -> Optional[bool]:
        """撤回一条消息。返回 True=撤回成功，False=协议端明确失败，None=未尝试（无消息ID/无 client）。

        Issue #89：此前不校验 OneBot 返回值，机器人非管理员时撤回被拒也毫无感知，
        审核路径随后照样在群里宣布「消息已被撤回」。现经 _call_group_api 统一校验
        status/retcode 与异常，调用方可据此决定是否发提示、日志如何记录。
        """
        mid = self._safe_int(msg_id)
        if not mid:
            return None
        client = await self._get_client(event)
        if not client:
            return None
        ok, err = await self._call_group_api(client, "delete_msg", "撤回消息", message_id=mid)
        if not ok:
            # 撤回失败多为业务性原因（超2分钟/无权限），不清 client 缓存
            logger.warning(f"[GroupMgr] 撤回消息失败: {err}")
        return ok

    async def _maybe_recall_on_kick(self, client, gid, user_id) -> int:
        """按 kick_recall_enabled 配置在踢人前撤回该成员近期消息，返回撤回条数。

        定义在 OneBotMixin（Main 继承）而非 CommandsMixin/LlmToolsMixin，
        因为 commands 和 llm_tools 两处踢人入口都要用 self 调用它，
        而 Main 不继承那两个 Mixin（会 AttributeError，同 #18/#19/#31 坑）。
        """
        group_id = str(gid)
        if not self._cfg("kick_recall_enabled", False, group_id=group_id):
            return 0
        recall_count = min(max(self._cfg_int("kick_recall_count", 10, group_id=group_id), 1), 50)
        return await self._recall_user_recent_msgs(client, gid, user_id, recall_count)

    async def _recall_user_recent_msgs(self, client, group_id, user_id, count: int) -> int:
        """撤回某用户在群内最近 count 条消息（供踢人自动撤回等复用）。返回实际撤回条数。

        注意：OneBot delete_msg 只能撤回约 2 分钟内的消息，超时的会静默失败。
        """
        gid = self._safe_int(group_id, 0)
        if not gid or not user_id or count <= 0:
            return 0
        recalled = 0
        try:
            result = await asyncio.wait_for(
                client.call_action('get_group_msg_history', group_id=gid, count=100),
                timeout=ONEBOT_CALL_TIMEOUT)
            result = self._extract_data_result(result)
            msgs = result.get('messages', []) if isinstance(result, dict) else []
            # 从最新往回撤，直到够 count 条
            for msg in reversed(msgs):
                if recalled >= count:
                    break
                sender = msg.get('sender') or {}
                if str(sender.get('user_id', '')) != str(user_id):
                    continue
                mid = msg.get('message_id')
                if not mid:
                    continue
                try:
                    await asyncio.wait_for(client.call_action('delete_msg', message_id=mid), timeout=ONEBOT_CALL_TIMEOUT)
                    recalled += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[GroupMgr] 撤回用户近期消息失败({group_id}/{user_id}): {e}")
        return recalled

    async def _kick_member(self, event: AiocqhttpMessageEvent) -> bool:
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        try:
            result = await asyncio.wait_for(
                client.call_action(
                    'set_group_kick', group_id=gid, user_id=uid
                ),
                timeout=ONEBOT_CALL_TIMEOUT,
            )
            ok, error = self._check_api_result(result, "踢人")
            if not ok:
                logger.warning(f"[GroupMgr] 踢人失败: {error}")
                return False
            return True
        except Exception as e:
            # 业务性失败（无权限等）不清空 client 缓存——_get_client 自带三级回退，
            # 粗暴置 None 会让后续 _fetch_context_messages 等静默丢失上下文（审查 P0-6）
            logger.warning(f"[GroupMgr] 踢人失败: {e}")
            return False

    async def _mute_member(
        self, event: AiocqhttpMessageEvent, duration: int = None
    ) -> bool:
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        ban_duration = duration if duration is not None else self._cfg_int("moderation_ban_duration", 1800, group_id=group_id)
        try:
            result = await asyncio.wait_for(
                client.call_action(
                    'set_group_ban', group_id=gid, user_id=uid,
                    duration=ban_duration,
                ),
                timeout=ONEBOT_CALL_TIMEOUT,
            )
            ok, error = self._check_api_result(result, "禁言")
            if not ok:
                logger.warning(f"[GroupMgr] 禁言失败: {error}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 禁言失败: {e}")
            return False

    async def _unban_member(self, group_id, user_id, event: AstrMessageEvent = None) -> bool:
        # 解除某群成员禁言（set_group_ban duration=0）。用于定时解禁、申诉通过等场景。
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        try:
            result = await asyncio.wait_for(
                client.call_action(
                    'set_group_ban', group_id=gid, user_id=uid, duration=0
                ),
                timeout=ONEBOT_CALL_TIMEOUT,
            )
            ok, error = self._check_api_result(result, "解禁")
            if not ok:
                logger.warning(
                    f"[GroupMgr] 解禁失败({group_id}/{user_id}): {error}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 解禁失败({group_id}/{user_id}): {e}")
            return False
